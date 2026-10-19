"""Managed media operation, separate from the deferred text recovery binding.

SDK mapping: vertexai 1.165.1 custom bidi_stream, not async_stream/SSE.
Platform ingress verification MUST be installed by deployment code; neither a
reference nor a forwarded principal authenticates the caller. No default ADC
identity is inferred at the server, and no deployment is performed here.
"""

import asyncio
import base64
from collections.abc import AsyncIterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import secrets
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, TypeAdapter

from ..domain.models import Contract, Identifier, Instant
from ..security.context import AccessDenied
from ..storage.plans import _decode, _encode
from ..voice import protocol as p
from ..voice.ports import (OutputAudio, OutputTranscript, ResponseBoundary,
                           ToolRequest, ToolResult)
from ..voice.sessions import StreamLease, _Claim, _path
from ..web.auth import digest

PRIVATE_LOGS = ContextVar("campuspulse_managed_voice_logs", default=False)
Reference = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$")]
Receipt = Reference
MAX_ENVELOPE = 24576
IO_SECONDS = 20
CLOSE_SECONDS = 2


def protect_logs():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_campuspulse_managed_voice", False):
        return
    prefixes = ("vertexai", "vertexai_genai", "google", "google_adk", "google_genai",
                "mcp", "httpx", "httpcore", "websockets")
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if PRIVATE_LOGS.get() and any(record.name == x or record.name.startswith(x + ".") for x in prefixes):
            record.msg, record.args = "managed_voice_sdk_event_redacted", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record
    factory._campuspulse_managed_voice = True
    logging.setLogRecordFactory(factory)


@contextmanager
def private_logs():
    protect_logs()
    token = PRIVATE_LOGS.set(True)
    try:
        yield
    finally:
        PRIVATE_LOGS.reset(token)


async def bounded(awaitable, seconds=IO_SECONDS):
    try:
        with private_logs():
            async with asyncio.timeout(seconds):
                return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValueError("managed_voice_unavailable") from None


class _Invocation(Contract):
    backend_principal: Identifier
    fence: StreamLease = Field(repr=False)
    expires_at: Instant
    consumer: Identifier | None = None


def _invocation_path(reference):
    TypeAdapter(Reference).validate_python(reference)
    return "cp_voice_invocations/" + digest(reference)


def same_fence(a, b):
    return a.model_dump(exclude={"lease_until"}) == b.model_dump(exclude={"lease_until"})


class VoiceInvocations:
    """Server-created single-use reference + fresh W04 durable claim lookup.

    Only this collection is written here. W04 alone writes stream checkpoints,
    retirement and renewal. No Calendar/model/MCP call in a transaction.
    """

    def __init__(self, sessions):
        self.sessions = sessions

    async def issue(self, context, lease):
        context.require_active()
        trusted = self.sessions.policy.require_context(context)
        b = lease.binding
        if (context.boundary != "backend" or
                (trusted.owner_id, context.session_id, trusted.run_id, trusted.config_revision) !=
                (b.owner_id, b.session_id, b.run_id, b.config_revision)):
            raise AccessDenied("voice_invocation_binding_rejected")
        reference = secrets.token_urlsafe(32)
        async def save(tx):
            await self.sessions._current(tx, lease)
            context.require_active()
            tx.set(_invocation_path(reference), _encode("voice_invocation", _Invocation(
                backend_principal=self.sessions.policy.config.identities.backend_service_account,
                fence=lease, expires_at=lease.deadline)))
        await self.sessions._run(save)
        return reference

    async def resolve(self, reference, principal, consumer, *, redeem=False, retired_call=None, new_call=None):
        async def load(tx):
            self.sessions.policy.require_principal("agent", principal)
            record = _decode(await tx.get(_invocation_path(reference)), "voice_invocation", _Invocation)
            current = _decode(await tx.get(_path(record.fence.device_id)), "voice_stream", _Claim)
            if (principal != record.backend_principal or
                    (record.consumer is not None if redeem else record.consumer != consumer) or
                    not same_fence(current.lease, record.fence)):
                raise AccessDenied("voice_invocation_rejected")
            # Latest expiry after renewal; NEVER serialized initial expiry/access.
            _, access, now = await self.sessions._current(tx, current.lease)
            if now >= record.expires_at:
                raise AccessDenied("voice_invocation_expired")
            if retired_call is not None and retired_call not in current.resume.seen_call_ids:
                raise AccessDenied("voice_call_not_retired")
            if new_call is not None and new_call in current.resume.seen_call_ids:
                raise AccessDenied("voice_call_already_retired")
            if redeem:
                tx.set(_invocation_path(reference), _encode("voice_invocation",
                    _Invocation.model_validate(record.model_dump() | {"consumer": consumer})))
            return current.lease, access
        return await self.sessions._run(load)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    v: Literal[1]
    seq: Annotated[StrictInt, Field(ge=0, le=100000)]
    kind: Literal["open", "input", "dispatch", "result_ack", "cancel", "ready", "event", "receipt", "closed", "error"]
    body: dict = Field(repr=False)


def envelope(seq, kind, body):
    return decode_envelope({"v": 1, "seq": seq, "kind": kind, "body": body}).model_dump(mode="json")


def decode_envelope(raw):
    if not isinstance(raw, dict):
        raise ValueError("managed_voice_envelope_rejected")
    try:
        length = len(json.dumps(raw, allow_nan=False, ensure_ascii=True).encode())
        if length > MAX_ENVELOPE or type(raw.get("v")) is not int:
            raise ValueError()
        return Envelope.model_validate(raw)
    except Exception:
        raise ValueError("managed_voice_envelope_rejected") from None


def exact(body, *keys):
    if not isinstance(body, dict) or set(body) != set(keys):
        raise ValueError("managed_voice_fields_rejected")


def b64(value, limit):
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise ValueError("managed_voice_media_rejected")
    try:
        data = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError("managed_voice_media_rejected") from None
    if not data or len(data) > limit or len(data) % 2:
        raise ValueError("managed_voice_media_rejected")
    return data


def encode_input(packet):
    if isinstance(packet, p.AudioFrame):
        return {"audio": base64.b64encode(p.encode_audio(packet)).decode("ascii")}
    if isinstance(packet, (p.Input, p.Mute)):
        return {"control": packet.model_dump(mode="json")}
    raise ValueError("managed_voice_input_rejected")


def decode_input(body, lease):
    if set(body) == {"audio"}:
        # Header plus even PCM; original W02 sequence/generations preserved.
        data = b64(body["audio"], p.HEADER_SIZE + p.MAX_INPUT_PCM)
        packet = p.decode_audio(data)
        if packet.kind != "input":
            raise ValueError("managed_voice_input_rejected")
    else:
        exact(body, "control")
        packet = p.parse_json(json.dumps(body["control"]), client=True)
        if not isinstance(packet, (p.Input, p.Mute)):
            raise ValueError("managed_voice_input_rejected")
    if packet.connection_generation != lease.connection_generation:
        raise ValueError("managed_voice_generation_rejected")
    return packet


def encode_event(item):
    if isinstance(item, ResponseBoundary):
        return {"type": "boundary", "generation": item.generation, "phase": item.phase}
    if isinstance(item, OutputAudio):
        return {"type": "audio", "generation": item.generation,
                "pcm": base64.b64encode(item.pcm).decode("ascii")}
    if isinstance(item, OutputTranscript):
        return {"type": "transcript", **vars(item)}
    if isinstance(item, ToolRequest):
        return {"type": "tool", **vars(item)}
    # W05 intentionally creates fresh SDK state: opaque tokens/history ignored.
    raise ValueError("managed_voice_event_rejected")


def _decode_event(body):
    kind = body.get("type")
    if kind == "boundary":
        exact(body, "type", "generation", "phase")
        TypeAdapter(p.PositiveU32).validate_python(body["generation"])
        if body["phase"] not in ("started", "completed", "interrupted"):
            raise ValueError("managed_voice_boundary_rejected")
        return ResponseBoundary(body["generation"], body["phase"])
    if kind == "audio":
        exact(body, "type", "generation", "pcm")
        TypeAdapter(p.PositiveU32).validate_python(body["generation"])
        return OutputAudio(body["generation"], b64(body["pcm"], p.MAX_OUTPUT_PCM))
    if kind == "transcript":
        exact(body, "type", "generation", "transcript_id", "speaker", "revision", "final", "text")
        TypeAdapter(p.U32).validate_python(body["generation"])
        TypeAdapter(p.WireId).validate_python(body["transcript_id"])
        TypeAdapter(p.U32).validate_python(body["revision"])
        TypeAdapter(p.Text).validate_python(body["text"])
        if type(body["final"]) is not bool or body["speaker"] not in ("user", "assistant"):
            raise ValueError("managed_voice_transcript_rejected")
        return OutputTranscript(**{k: v for k, v in body.items() if k != "type"})
    if kind == "tool":
        exact(body, "type", "call_id", "tool", "arguments")
        TypeAdapter(p.WireId).validate_python(body["call_id"])
        if body["tool"] not in ("read_assignments", "read_calendar", "solve_repair") or not isinstance(body["arguments"], dict):
            raise ValueError("managed_voice_tool_rejected")
        return ToolRequest(body["call_id"], body["tool"], body["arguments"])
    raise ValueError("managed_voice_event_rejected")


def decode_event(body):
    try:
        return _decode_event(body)
    except Exception:
        raise ValueError("managed_voice_event_rejected") from None


@dataclass
class _Pending:
    request: ToolRequest
    state: str = "requested"
    result: ToolResult | None = None
    receipt: str | None = None


class ManagedVoiceRuntime:
    """One run_live per admitted connection, automatic tools through W04.

    ingress: async callable with NO payload arguments, supplied by platform
    middleware; returns verified backend principal for this active invocation.
    scope_factory: server callable(binding) -> RequestContextFactory agent scope.
    Neither can be provided over the media operation. No fake ingress default.
    """

    def __init__(self, *, invocations, ingress, scope_factory, config, constraints_factory,
                 session_factory=None):
        if any(x is None for x in (invocations, ingress, scope_factory, config, constraints_factory)):
            raise ValueError("managed_voice_runtime_binding_required")
        self.invocations, self.ingress, self.scope_factory = invocations, ingress, scope_factory
        self.config, self.constraints_factory = config, constraints_factory
        # Injection is only a network-denied test seam. Default is the real W05
        # ADK Runner session executing INSIDE the managed runtime, not backend.
        self.session_factory = session_factory

    def register_operations(self, **kwargs) -> dict[str, list[str]]:
        return {"bidi_stream": ["bidi_stream_query"]}

    async def bidi_stream_query(self, request_queue: asyncio.Queue[Any]) -> AsyncIterable[Any]:
        from uuid import uuid4
        from ..agent.voice_policy import VoicePolicy
        output = asyncio.Queue(maxsize=32)
        pending = {}
        tasks = []
        session = None
        out_seq = 0
        async def emit(kind, body):
            await bounded(output.put((kind, body)))
        async def cleanup():
            nonlocal session
            owned = list(tasks)
            tasks.clear()
            for task in owned:
                task.cancel()
            closing, session = session, None
            # Shared aggregate deadline, including session close. Never catch an
            # outer cancellation and start another independently budgeted close.
            close_task = None
            try:
                with private_logs():
                    if closing is not None:
                        close_task = asyncio.create_task(closing.close())
                        owned.append(close_task)
                    async with asyncio.timeout(CLOSE_SECONDS):
                        if owned:
                            await asyncio.gather(*owned, return_exceptions=True)
                    if close_task is not None and isinstance(close_task.exception(), BaseException):
                        raise ValueError("managed_voice_close_failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ValueError("managed_voice_close_failed") from None
            finally:
                for task in owned:
                    if not task.done():
                        task.cancel()
                if owned:
                    await asyncio.gather(*owned, return_exceptions=True)
                pending.clear()
        try:
            principal = await bounded(self.ingress(), 2)
            first = decode_envelope(await bounded(request_queue.get(), 5))
            if first.seq != 0 or first.kind != "open":
                raise ValueError("managed_voice_open_required")
            exact(first.body, "reference")
            reference = TypeAdapter(Reference).validate_python(first.body["reference"])
            consumer = "runtime_" + uuid4().hex
            current, _ = await self.invocations.resolve(reference, principal, consumer, redeem=True)
            initial = current
            async def fresh(*, retired_call=None, new_call=None):
                nonlocal current
                current, access = await self.invocations.resolve(reference, principal, consumer,
                    retired_call=retired_call, new_call=new_call)
                return access
            class RuntimePolicy(VoicePolicy):
                async def authorize(inner, name=None):
                    async with asyncio.timeout(2):
                        for attempt in range(2):
                            await fresh()
                            captured = current
                            try:
                                return await super().authorize(name)
                            except AccessDenied as error:
                                if attempt or str(error) != "voice_stream_stale":
                                    raise
                                await fresh()
                                if not same_fence(captured, current) or current.lease_until <= captured.lease_until:
                                    raise
            policy = RuntimePolicy(sessions=self.invocations.sessions, lease=initial,
                lease_provider=lambda: current, request_scope=lambda: self.scope_factory(current.binding),
                constraints=self.constraints_factory(current), now=self.invocations.sessions.now())
            factory = self.session_factory
            if factory is None:
                from ..agent.voice_agent import LocalAdkSession
                factory = LocalAdkSession
            session = factory(policy, self.config)
            await bounded(session.start(), 5)
            await fresh()
            yield envelope(out_seq, "ready", {"connection_generation": current.connection_generation})
            out_seq += 1

            async def upstream():
                while True:
                    item = await session.receive()
                    await fresh()
                    if item is None:
                        await emit("closed", {})
                        return
                    body = encode_event(item)
                    decode_event(body)  # Validate also the runtime -> network boundary.
                    if isinstance(item, ToolRequest):
                        await fresh(new_call=item.call_id)
                        if (item.call_id in pending or len(pending) >= 24 or
                                sum(e.state != "finished" for e in pending.values()) >= 4):
                            raise ValueError("managed_voice_call_replay")
                        pending[item.call_id] = _Pending(item)
                    await emit("event", body)

            async def dispatch(entry):
                await fresh(retired_call=entry.request.call_id)
                access = await policy.authorize(entry.request.tool)
                result = await session.bridge.execute(access, entry.request)
                await fresh(retired_call=entry.request.call_id)
                entry.result = result  # Actual result NEVER replaced by backend data.
                entry.receipt = secrets.token_urlsafe(32)
                entry.state = "executed"
                await emit("receipt", {"call_id": entry.request.call_id,
                    "status": result.status, "receipt": entry.receipt})

            async def run_dispatch(entry):
                try:
                    await dispatch(entry)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Wake the output owner even when a dynamically admitted
                    # tool task fails before producing a receipt.
                    await emit("error", {"code": "managed_voice_unavailable"})

            async def inputs():
                seq = 1
                audio_seq = 0
                while True:
                    raw = decode_envelope(await request_queue.get())
                    if raw.seq != seq:
                        raise ValueError("managed_voice_sequence_rejected")
                    seq += 1
                    await fresh()
                    if raw.kind == "input":
                        packet = decode_input(raw.body, current)
                        if isinstance(packet, p.AudioFrame):
                            if packet.sequence != audio_seq:
                                raise ValueError("managed_voice_audio_sequence_rejected")
                            audio_seq += 1
                        await bounded(session.send(packet))
                    elif raw.kind == "dispatch":
                        exact(raw.body, "call_id")
                        entry = pending.get(raw.body["call_id"])
                        if entry is None or entry.state != "requested":
                            raise AccessDenied("managed_voice_dispatch_rejected")
                        await fresh(retired_call=entry.request.call_id)
                        entry.state = "executing"
                        task = asyncio.create_task(run_dispatch(entry))
                        tasks.append(task)
                    elif raw.kind == "result_ack":
                        exact(raw.body, "call_id", "receipt", "status")
                        entry = pending.get(raw.body["call_id"])
                        if (entry is None or entry.state != "executed" or
                                raw.body["receipt"] != entry.receipt or raw.body["status"] != entry.result.status):
                            raise AccessDenied("managed_voice_result_rejected")
                        await fresh(retired_call=entry.request.call_id)
                        entry.state = "finished"
                        await session.tool_result(entry.request.call_id, entry.result)
                    elif raw.kind == "cancel":
                        exact(raw.body)
                        await emit("closed", {})
                        return
                    else:
                        raise ValueError("managed_voice_control_rejected")

            async def monitor():
                while True:
                    await asyncio.sleep(1)
                    await fresh()

            with private_logs():
                tasks.extend([asyncio.create_task(inputs()), asyncio.create_task(upstream()),
                              asyncio.create_task(monitor())])
            async with asyncio.timeout(max(.001, (initial.deadline - self.invocations.sessions.now()).total_seconds())):
                while True:
                    reader = asyncio.create_task(output.get())
                    try:
                        done, _ = await asyncio.wait([reader, *tasks], return_when=asyncio.FIRST_COMPLETED)
                        for task in tuple(tasks):
                            if task.done():
                                task.result()  # Propagate sanitized below; never orphan errors.
                        if reader not in done:
                            # A completed input/upstream puts its terminal before return.
                            if output.empty():
                                raise ValueError("managed_voice_stream_ended")
                            kind, body = output.get_nowait()
                        else:
                            kind, body = reader.result()
                        tasks[:] = [t for t in tasks if not t.done()]
                        await fresh()  # Backpressure/queue waits never grant stale output.
                        if kind in ("closed", "error"):
                            await cleanup()
                        yield envelope(out_seq, kind, body)
                        out_seq += 1
                        if kind in ("closed", "error"):
                            return
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Error output is terminal: retire all connection-owned work before
            # yielding it, even if the peer never asks for another frame.
            await cleanup()
            yield envelope(out_seq, "error", {"code": "managed_voice_unavailable"})
        finally:
            primary = sys.exception()
            try:
                await cleanup()
            except BaseException:
                if isinstance(primary, asyncio.CancelledError):
                    raise primary
                raise
