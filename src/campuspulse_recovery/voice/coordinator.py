"""Request-owned voice lifecycle. One ordered socket writer, concurrent duplex.

Only this request owns transport/model/tool tasks. Adapters must cooperate with
cancellation (Python cannot forcibly stop an arbitrary coroutine). Cleanup
re-cancels a slow child and joins it, rather than abandoning it. An adapter that
ignores cancellation forever violates the port contract; it cannot honestly be
given both a finite shutdown guarantee and an orphan-free guarantee.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
import time

from ..config import CLOUD_MARKERS
from ..domain.ports import BindingConflict, PortError
from ..security.context import AccessDenied
from . import protocol as p
from .ports import (
    BidiProvider, ClientTransport, OutputAudio, OutputTranscript, ProviderMode,
    ReadToolHandler, ResponseBoundary, ResumeUpdate, ToolRequest, ToolResult,
    require_provider_mode,
)
from .sessions import FirestoreVoiceSessions, HistoryNote, ResumeState, HISTORY_BYTES, HISTORY_ENTRIES


class Clock:
    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Limits:
    send_seconds: float = 2
    tool_seconds: float = 20
    turn_seconds: float = 60
    close_seconds: float = p.CLOSE_TIMEOUT_MS / 1000
    check_seconds: float = p.HEARTBEAT_INTERVAL_MS / 1000

    def __post_init__(self):
        if (not 0 < self.check_seconds <= 10 or not 0 < self.close_seconds <= 2
                or not 0 < self.send_seconds <= 2 or not 0 < self.tool_seconds <= 30
                or not 0 < self.turn_seconds <= 120):
            raise ValueError("invalid_voice_limits")


@dataclass(frozen=True)
class SessionOutcome:
    reason: str
    cleanup_failed: bool


class _Buffer:
    """Count bytes/events INCLUDING the one currently being sent."""

    def __init__(self, byte_limit):
        self.items = deque()
        self.changed = asyncio.Event()
        self.byte_limit, self.bytes, self.count = byte_limit, 0, 0

    def put(self, item, size=0):
        if self.count >= p.MAX_QUEUED_EVENTS or self.bytes + size > self.byte_limit:
            raise p.ProtocolError("backpressure")
        self.items.append((item, size))
        self.bytes += size
        self.count += 1
        self.changed.set()

    async def get(self):
        while not self.items:
            self.changed.clear()
            await self.changed.wait()
        return self.items.popleft()

    def done(self, size):
        self.bytes -= size
        self.count -= 1

    def discard(self, predicate):
        kept = deque()
        for item, size in self.items:
            if predicate(item):
                self.done(size)
            else:
                kept.append((item, size))
        self.items = kept


class VoiceCoordinator:
    """Single use. W07 passes an authenticated native token, never client IDs.

    Provider selection is explicit even for fakes; production only admits the
    managed mode. Actual managed configuration/IAM belongs to W06/startup.
    No SDK, default provider, credential refresh or mutation handler is created.
    """

    def __init__(self, sessions: FirestoreVoiceSessions, provider: BidiProvider, *,
                 selected_mode: ProviderMode, production: bool, tools: ReadToolHandler | None = None,
                 clock=None, limits=Limits()):
        require_provider_mode(provider, production=(production or sessions.policy.config.mode == "cloud"
            or bool(CLOUD_MARKERS.intersection(os.environ))), selected=selected_mode)
        self.sessions, self.provider, self.tools = sessions, provider, tools
        self.clock, self.limits = clock or Clock(), limits
        self.lease = self.upstream = self.client = self.state = None
        self.resume = ResumeState()
        self.input = _Buffer(p.MAX_QUEUED_INPUT_PCM)
        self.output = _Buffer(p.MAX_QUEUED_OUTPUT_PCM)
        self.tasks = set()
        self.authority_lock = asyncio.Lock()
        self.interrupted = asyncio.Event()
        self.invalidated_through = 0
        self.generation, self.output_open, self.turn_started = 0, False, None
        self.calls = set()
        self.call_started = {}
        self.upstream_times = deque()
        self.active_tools = 0
        self.used = False
        self.cleanup_failed = False
        self.ready_sent = False

    def _spawn(self, coroutine):
        async def owned():
            try:
                await coroutine
            except asyncio.CancelledError:
                raise
            except AccessDenied:
                self._end("revoked")
            except (BindingConflict, PortError):
                self._end("upstream_error")
            except p.ProtocolError as error:
                self._end("backpressure" if error.code == "backpressure" else "protocol_error")
            except Exception:
                self._end("upstream_error")
        task = asyncio.create_task(owned(), name="voice-request")
        self.tasks.add(task)
        return task

    def _end(self, reason):
        if not self.terminal.done():
            self.terminal.set_result(reason)

    async def authorize_tool(self, name):
        """W05 adapter hook for SDK-internal tools; do not cache its result."""
        self._require_local_lifetime()
        async with self.authority_lock:


            self._require_local_lifetime()
            access = await self.sessions.check(self.lease, tool=name)
            self._require_local_lifetime()
            return access

    def _elapsed(self):
        return min(p.MAX_SESSION_MS, max(0, int((self.clock.monotonic() - self.started) * 1000)))

    def _message(self, kind, **fields):
        return p.SERVER_ADAPTER.validate_python(dict(version="1", type=kind,
            event_id="evt_" + str(self.state.event_sequence), event_sequence=self.state.event_sequence,
            connection_generation=self.lease.connection_generation, elapsed_ms=self._elapsed(), **fields))

    def _control(self, kind, **fields):
        self.output.put((kind, fields))

    async def run(self, token: str, client: ClientTransport) -> SessionOutcome:
        if self.used:
            raise RuntimeError("voice_coordinator_already_used")
        self.used = True
        self.client = client
        self.started = self.last_packet = self.last_activity = self.clock.monotonic()
        self.last_checkpoint = self.started
        self.terminal = asyncio.get_running_loop().create_future()
        reason, cancelled = "upstream_error", False
        try:
            self.lease, self.resume = await self.sessions.acquire(token)
            self.calls = set(self.resume.seen_call_ids)
            self.state = p.ProtocolState(self.lease.connection_generation)
            async with asyncio.timeout(p.START_TIMEOUT_MS / 1000):
                packet = await client.receive()
            if packet is None:
                self._end("disconnected")
            else:
                if not isinstance(packet, str):
                    raise p.ProtocolError("invalid_state")
                start = p.parse_json(packet, client=True)
                if not isinstance(start, p.Start):
                    raise p.ProtocolError("invalid_state")
                self.state.accept_client(start, now=self.clock.monotonic())

                self._spawn(self._receive_client())
                self._spawn(self._watch())
                self._spawn(self._open())
            reason = await asyncio.shield(self.terminal)
        except asyncio.CancelledError:
            reason, cancelled = "disconnected", True
        except AccessDenied:
            reason = "revoked"
        except p.ProtocolError as error:
            reason = "backpressure" if error.code == "backpressure" else "protocol_error"
        except TimeoutError:
            reason = "timeout"
        except Exception:
            reason = "upstream_error"
        finally:
            self._end(reason)

            cleanup = asyncio.create_task(self._cleanup(reason), name="voice-cleanup")
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            await cleanup
        if cancelled:
            raise asyncio.CancelledError
        return SessionOutcome(reason, self.cleanup_failed)

    async def _open(self):
        async with self.authority_lock:
            self._require_local_lifetime()
            await self.sessions.check(self.lease)
            self._require_local_lifetime()
        async with asyncio.timeout(p.START_TIMEOUT_MS / 1000):
            self.upstream = await self.provider.open(self.lease, self.resume)
        if self.terminal.done():
            return

        ready = self._message("ready", session_id=self.lease.session_id, run_id=self.lease.run_id,
            expires_in_ms=max(1, min(p.MAX_SESSION_MS, int((self.lease.deadline - self.sessions.now()).total_seconds() * 1000))),
            input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT)
        await self._send(ready)
        self.ready_sent = True
        self._spawn(self._send_input())
        self._spawn(self._receive_upstream())
        self._spawn(self._write())

    async def _receive_client(self):
        while True:
            raw = await self.client.receive()
            now = self.clock.monotonic()
            if raw is None:
                self._end("disconnected")
                return
            self.last_packet = now
            self._require_local_lifetime()
            if not self.ready_sent:
                if isinstance(raw, str):
                    event = p.parse_json(raw, client=True)
                    if (isinstance(event, p.Stop)
                            and event.connection_generation == self.lease.connection_generation):
                        self._end("stopped")
                        return
                raise p.ProtocolError("invalid_state")
            if isinstance(raw, bytes):
                frame = p.decode_audio(raw, expected_kind="input")
                self.state.accept_audio(frame, now=now)
                self.last_activity = now
                self.input.put(frame, len(frame.pcm))
            else:
                event = p.parse_json(raw, client=True)
                self.state.accept_client(event, now=now)
                if isinstance(event, p.Stop):
                    self._end("stopped")
                    return
                if isinstance(event, p.Heartbeat):
                    self._control("heartbeat_ack", request_id=event.request_id)
                else:
                    self.last_activity = now
                    self.input.put(event)

    async def _send_input(self):
        while True:
            packet, size = await self.input.get()
            try:
                async with asyncio.timeout(self.limits.send_seconds):

                    self._require_local_lifetime()
                    await self.upstream.send(packet)
            except TimeoutError:
                self._end("backpressure")
                return
            finally:
                self.input.done(size)

    def _stale_output(self, item):
        return (isinstance(item, (OutputAudio, OutputTranscript))
                and item.generation != 0 and item.generation <= self.invalidated_through)

    async def _receive_upstream(self):
        while True:
            event = await self.upstream.receive()
            if event is None:
                self._end("upstream_error")
                return
            now = self.clock.monotonic()
            while self.upstream_times and self.upstream_times[0] <= now - 1:
                self.upstream_times.popleft()
            if len(self.upstream_times) >= 200:
                raise p.ProtocolError("rate_limited")
            self.upstream_times.append(now)
            if isinstance(event, ResponseBoundary):
                if event.phase == "started":
                    if self.output_open or event.generation != self.generation + 1:
                        raise p.ProtocolError("stale_generation")
                    self.generation, self.output_open = event.generation, True
                    self.turn_started = self.clock.monotonic()
                elif event.phase in ("completed", "interrupted"):
                    if event.generation != self.generation or not self.output_open:
                        if event.generation <= self.invalidated_through:
                            continue
                        raise p.ProtocolError("stale_generation")
                    self.output_open, self.turn_started = False, None
                    if event.phase == "interrupted":
                        self.invalidated_through = event.generation
                        self.output.discard(self._stale_output)
                        self.interrupted.set()
                else:
                    raise p.ProtocolError("invalid_state")
                self.output.put(event)
            elif isinstance(event, (OutputAudio, OutputTranscript)):
                if self._stale_output(event):
                    continue
                if event.generation and (event.generation != self.generation or not self.output_open):
                    raise p.ProtocolError("stale_generation")
                if isinstance(event, OutputAudio):
                    p.encode_audio(p.AudioFrame("output", self.lease.connection_generation,
                                               event.generation, 0, event.pcm))
                else:

                    self._message("transcript", **vars(event))
                self.output.put(event, len(event.pcm) if isinstance(event, OutputAudio) else 0)
            elif isinstance(event, ResumeUpdate):
                self.resume = self.resume.changed(transport_token=event.token, transport_expires_at=event.expires_at)
            elif isinstance(event, ToolRequest):
                if (event.call_id in self.calls or len(self.calls) >= 64 or self.active_tools >= 4
                        or len(json.dumps(event.arguments, allow_nan=False).encode("utf-8")) > 16384):
                    raise p.ProtocolError("limit_exceeded")

                self._message("tool_started", call_id=event.call_id, tool=event.tool)
                await self.authorize_tool(event.tool)
                self.calls.add(event.call_id)
                self.call_started[event.call_id] = self.clock.monotonic()
                self.resume = self.resume.changed(seen_call_ids=tuple(sorted(self.calls)))


                await self._checkpoint()
                self._require_local_lifetime()
                self.active_tools += 1
                self._control("tool_started", call_id=event.call_id, tool=event.tool)
                self._spawn(self._tool(event))
            else:
                raise p.ProtocolError("unknown_event")

    async def _tool(self, request):
        began = self.clock.monotonic()
        try:
            access = await self.authorize_tool(request.tool)
            async with asyncio.timeout(self.limits.tool_seconds):
                self._require_local_lifetime()
                result = (ToolResult("unavailable") if self.tools is None
                          else await self.tools.execute(access, request))

            await self.authorize_tool(request.tool)
            async with asyncio.timeout(self.limits.send_seconds):
                self._require_local_lifetime()
                await self.upstream.tool_result(request.call_id, result)
            self._control("tool_finished", call_id=request.call_id, tool=request.tool,
                status=result.status, duration_ms=min(p.MAX_SESSION_MS, max(0, int((self.clock.monotonic() - began) * 1000))))
        finally:
            self.active_tools -= 1

    async def _send(self, packet):
        if not isinstance(packet, (p.Closed, p.ToolFinished)):
            self._require_local_lifetime()
        if isinstance(packet, p.AudioFrame):
            self.state.accept_audio(packet, now=self.clock.monotonic())
        else:
            self.state.accept_server(packet)
        async with asyncio.timeout(self.limits.send_seconds):
            await self.client.send(packet)

    def _require_local_lifetime(self):


        if self.terminal.done():
            raise AccessDenied("voice_stream_stopped")
        now = self.sessions.now()
        if now >= self.lease.deadline or self.clock.monotonic() - self.started >= p.MAX_SESSION_MS / 1000:
            self._end("expired")
            raise AccessDenied("voice_stream_expired")
        if now >= self.lease.lease_until:
            self._end("revoked")
            raise AccessDenied("voice_stream_stale")

    async def _send_audio(self, packet):


        sending = asyncio.create_task(self._send(packet), name="voice-audio-send")
        wake = asyncio.create_task(self.interrupted.wait(), name="voice-interruption")
        try:
            await asyncio.wait((sending, wake), return_when=asyncio.FIRST_COMPLETED)
            if wake.done() and packet.generation <= self.invalidated_through and not sending.done():
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
            else:
                await sending
        finally:
            sending.cancel()
            wake.cancel()
            await asyncio.gather(sending, wake, return_exceptions=True)

    def _history(self, event):
        if not event.final:
            return

        text = event.text[:512]
        notes = list(self.resume.history) + [HistoryNote(speaker=event.speaker, text=text)]
        notes = notes[-HISTORY_ENTRIES:]
        while sum(len(n.text.encode("utf-8")) for n in notes) > HISTORY_BYTES:
            notes.pop(0)
        self.resume = self.resume.changed(history=tuple(notes))

    async def _checkpoint(self):
        async with self.authority_lock:
            await self.sessions.checkpoint(self.lease, self.resume)
        self.last_checkpoint = self.clock.monotonic()

    async def _write(self):
        while True:
            item, size = await self.output.get()
            try:
                if self._stale_output(item):
                    continue
                if isinstance(item, OutputAudio):
                    self.interrupted.clear()
                    await self._send_audio(p.AudioFrame("output", self.lease.connection_generation,
                        item.generation, self.state.output_sequence, item.pcm))
                elif isinstance(item, OutputTranscript):
                    await self._send(self._message("transcript", **vars(item)))
                    self._history(item)
                elif isinstance(item, ResponseBoundary):
                    kind = "interrupted" if item.phase == "interrupted" else "generation"
                    fields = {"generation": item.generation}
                    if kind == "generation":
                        fields["phase"] = item.phase
                    await self._send(self._message(kind, **fields))
                    if item.phase == "completed" and self.clock.monotonic() - self.last_checkpoint >= 5:
                        await self._checkpoint()
                else:
                    await self._send(self._message(item[0], **item[1]))
            except TimeoutError:
                self._end("backpressure")
                return
            finally:
                self.output.done(size)

    async def _watch(self):
        while True:
            remaining = (self.lease.deadline - self.sessions.now()).total_seconds()
            await self.clock.sleep(max(0, min(self.limits.check_seconds, remaining)))
            now = self.clock.monotonic()
            if self.sessions.now() >= self.lease.deadline or now - self.started >= p.MAX_SESSION_MS / 1000:
                self._end("expired")
                return
            if (now - self.last_packet >= p.HEARTBEAT_TIMEOUT_MS / 1000
                    or now - self.last_activity >= p.IDLE_TIMEOUT_MS / 1000
                    or (self.turn_started is not None and now - self.turn_started >= self.limits.turn_seconds)):
                self._end("timeout")
                return
            async with self.authority_lock:
                self.lease = await self.sessions.renew(self.lease)

    async def _cleanup(self, reason):
        deadline = asyncio.get_running_loop().time() + self.limits.close_seconds
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.limits.close_seconds / 2)
            if pending:
                self.cleanup_failed = True
                for task in pending:
                    task.cancel()


            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.input.discard(lambda _: True)
        self.output.discard(lambda _: True)

        async def attempt(action):
            try:

                remaining = max(0, deadline - asyncio.get_running_loop().time())
                async with asyncio.timeout(min(self.limits.close_seconds / 8, remaining)):
                    await action()
            except Exception:
                self.cleanup_failed = True

        if self.upstream is not None:
            await attempt(self.upstream.cancel)
            await attempt(self.upstream.close)
        if self.lease is not None:

            if reason in ("stopped", "disconnected"):
                await attempt(self._checkpoint)
            await attempt(lambda: self.sessions.release(self.lease))
        if self.state is not None and self.state.phase not in (p.Phase.NEW, p.Phase.CLOSED):



            if self.state.phase == p.Phase.ACTIVE:
                for call_id, (tool, done) in self.state.tools.items():
                    if not done:
                        await attempt(lambda: self._send(self._message("tool_finished", call_id=call_id,
                            tool=tool, status="cancelled", duration_ms=min(p.MAX_SESSION_MS,
                            max(0, int((self.clock.monotonic() - self.call_started[call_id]) * 1000))))))
            await attempt(lambda: self._send(self._message("closed", reason=reason)))
        await attempt(self.client.close)
