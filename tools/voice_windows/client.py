"""Native W03/W02 client. Secrets are deliberately absent from CLI/config/logs."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import getpass
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import ssl
import time
from urllib.parse import urlsplit
import webbrowser

from campuspulse_recovery.voice import protocol as p
from .audio import OutputBuffer, Pacer


class ClientError(ValueError):
    pass


def origin_checked(value, *, local=False):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.path in ("", "/")
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.port != 0 and not any(c.isspace() for c in value))
        if not valid or (not local and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError()
        return "https://" + parsed.netloc
    except ValueError:
        raise ClientError("invalid_backend_origin") from None


def proof():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass(repr=False)
class PairAttempt:
    verifier: str = field(repr=False)
    grant: str = field(repr=False)
    code: str = field(repr=False)


class NativeHTTP:
    """TLS checked; no cookie jar, redirects, proxies or request/body diagnostics."""
    def __init__(self, origin, context=None, *, local=False):
        self.origin = origin_checked(origin, local=local)
        self.context = context or ssl.create_default_context()
        if self.context.verify_mode != ssl.CERT_REQUIRED or not self.context.check_hostname:
            raise ClientError("verified_tls_required")
        self.timeout = 7

    async def post(self, path, body):
        writer = None
        try:
            parsed = urlsplit(self.origin)
            data = json.dumps(body).encode("utf-8")
            if path not in ("/api/voice/pairing/start", "/api/voice/pairing/exchange") or len(data) > 2048:
                raise ClientError("invalid_pairing_request")


            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port or 443,
                    ssl=self.context, server_hostname=parsed.hostname, limit=8192,
                    ssl_handshake_timeout=self.timeout, ssl_shutdown_timeout=1)
                writer.write((f"POST {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                    "Connection: close\r\n\r\n").encode("ascii") + data)
                await writer.drain()
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("ascii").split("\r\n")
                if not lines[0].startswith("HTTP/1.1 200 "):
                    raise ClientError("pairing_denied_or_unavailable")
                headers = [line.split(":", 1) for line in lines[1:] if line]
                lengths = [v.strip() for k, v in headers if k.lower() == "content-length"]
                if len(lengths) != 1 or any(k.lower() == "transfer-encoding" for k, v in headers):
                    raise ClientError("invalid_pairing_response")
                length = int(lengths[0])
                if not 0 < length <= 4096:
                    raise ClientError("invalid_pairing_response")
                return json.loads(await reader.readexactly(length))
        except Exception:
            raise ClientError("pairing_denied_or_unavailable") from None
        finally:
            if writer is not None:
                writer.close()
                with suppress(Exception):
                    async with asyncio.timeout(1):
                        await writer.wait_closed()


class PairingClient:
    def __init__(self, http):
        self.http = http

    async def start(self, device_name="CampusPulse Windows"):
        verifier, challenge = proof()
        result = await self.http.post("/api/voice/pairing/start", {
            "challenge": challenge, "client_kind": "windows", "device_name": device_name})
        if (set(result) != {"pairing_code", "grant", "expires_at", "approval_path"}
                or result["approval_path"] != "/voice/pair"
                or not isinstance(result["pairing_code"], str)
                or not re.fullmatch(r"[A-Z0-9]{12}", result["pairing_code"])
                or not isinstance(result["grant"], str) or len(result["grant"]) > 256):
            raise ClientError("invalid_pairing_response")
        return PairAttempt(verifier, result["grant"], result["pairing_code"])

    async def exchange(self, attempt):
        try:
            result = await self.http.post("/api/voice/pairing/exchange", {
                "grant": attempt.grant, "verifier": attempt.verifier})
            if set(result) != {"credential", "expires_at", "scope"} or result["scope"] != "voice:read":
                raise ClientError("invalid_pairing_response")
            if not isinstance(result["credential"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", result["credential"]):
                raise ClientError("invalid_pairing_response")
            return result["credential"]
        finally:
            attempt.verifier = attempt.grant = attempt.code = ""


async def browser_pair(origin):
    client = PairingClient(NativeHTTP(origin))
    attempt = await client.start()

    webbrowser.open(origin + "/voice/pair")
    answer = getpass.getpass("Pairing code: " + attempt.code + "\nReview this device in the signed-in browser. "
                            "After clicking Connect, type CONTINUE here (hidden): ")
    if answer != "CONTINUE":
        raise ClientError("pairing_cancelled")
    return await client.exchange(attempt)


def silent_logger():
    logger = logging.Logger("campuspulse.voice.client.silent")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.disabled = True
    return logger


def wss_connection(origin, credential, context=None):
    from websockets.asyncio.client import connect
    origin = origin_checked(origin, local=True)
    context = context or ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ClientError("verified_tls_required")

    return connect("wss://" + urlsplit(origin).netloc + "/api/voice/ws",
        ssl=context, additional_headers={"Authorization": "Bearer " + credential},
        compression=None, proxy=None, open_timeout=5, close_timeout=2,
        max_size=p.MAX_EVENT_BYTES, max_queue=4, write_limit=4096,
        ping_interval=10, ping_timeout=10, logger=silent_logger())


def safe_event(event):

    names = ("type", "run_id", "session_id", "connection_generation", "generation",
             "phase", "call_id", "tool", "status", "duration_ms", "elapsed_ms", "reason", "code")
    return {name: getattr(event, name) for name in names if hasattr(event, name)}


class VoiceClient:
    def __init__(self, socket, *, sink=None, flush=lambda: None, trace=lambda value: None,
                 heartbeat=10, sink_timeout=2):
        self.socket, self.trace = socket, trace
        self.state = None
        self.ready, self.closed, self.response_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.input_sequence = 0
        self.sent_bytes = self.received_bytes = self.stale_drops = self.acks = 0
        self.started = time.monotonic()
        self.first_output_ms = None
        self.heartbeat, self.sink_timeout = heartbeat, sink_timeout
        self.reason = None
        self.last_inbound = self.started
        self.deadline = self.started + 540
        async def discard(pcm):
            pass
        async def bounded_sink(pcm):
            async with asyncio.timeout(self.sink_timeout):
                await (sink or discard)(pcm)
        self.output = OutputBuffer(bounded_sink, flush)
        self.request_counter = 0
        self.tasks = []
        self.uncertain = False
        self.pending_heartbeats = deque()
        self._receive_drain = None
        self._stop_deadline = None
        self.shutdown_packets = self.shutdown_discarded_packets = 0
        self.start_event = p.Start(version="1", type="start", request_id="start_1",
            input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT)

    async def control(self, kind, **values):
        async with self.send_lock:
            if self.uncertain:
                raise ClientError("connection_uncertain")
            self.request_counter += 1
            event = p.parse_json(json.dumps(dict(version="1", type=kind,
                request_id=f"request_{self.request_counter}",
                connection_generation=self.state.connection_generation, **values)), client=True)
            if isinstance(event, p.Heartbeat) and len(self.pending_heartbeats) >= p.MAX_QUEUED_EVENTS:
                raise p.ProtocolError("limit_exceeded")



            receive_drain = deepcopy(self.state) if isinstance(event, p.Stop) else None
            self.state.accept_client(event, now=time.monotonic())
            if receive_drain is not None:
                self._receive_drain = receive_drain
                self._stop_deadline = time.monotonic() + 2
                self.output.flush()
            if isinstance(event, p.Heartbeat):

                self.pending_heartbeats.append(event.request_id)
            await self.admitted_send(p.encode_json(event, client=True))

    async def admitted_send(self, packet):
        async def send():
            async with asyncio.timeout(2):
                await self.socket.send(packet)
        task = asyncio.create_task(send())
        try:
            await asyncio.shield(task)
        except BaseException:


            self.uncertain = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await self.socket.close()
            raise

    async def send_pcm(self, pcm):
        async with self.send_lock:
            if self.uncertain:
                raise ClientError("connection_uncertain")
            frame = p.AudioFrame("input", self.state.connection_generation, 0, self.input_sequence, pcm)
            self.state.accept_audio(frame, now=time.monotonic())
            await self.admitted_send(p.encode_audio(frame))
            self.input_sequence += 1
            self.sent_bytes += len(pcm)

    async def wav_turn(self, pcm):
        self.response_done.clear()
        if not self.state.input_open:
            await self.control("input", action="begin")
        pacer = Pacer()
        for offset in range(0, len(pcm), 640):
            chunk = pcm[offset:offset + 640]
            await pacer.wait(len(chunk) / 32000)
            await self.send_pcm(chunk)

        await pacer.wait(0)
        await self.control("input", action="end")

    def receive_packet(self, packet):
        self.last_inbound = time.monotonic()
        if self.closed.is_set():
            raise p.ProtocolError("invalid_state")
        if self._receive_drain is not None:
            return self.receive_shutdown_packet(packet)
        if isinstance(packet, bytes):
            frame = p.decode_audio(packet, expected_kind="output")
            if self.state is None:
                raise p.ProtocolError("invalid_state")
            if (frame.connection_generation < self.state.connection_generation
                    or (frame.connection_generation == self.state.connection_generation and
                        (frame.generation < self.state.generation or
                         (frame.generation == self.state.generation and not self.state.output_open)))):
                self.stale_drops += 1
                return
            self.state.accept_audio(frame, now=time.monotonic())
            self.output.put(frame)
            self.received_bytes += len(frame.pcm)
            if self.first_output_ms is None:
                self.first_output_ms = round((time.monotonic() - self.started) * 1000, 2)
            return
        event = p.parse_json(packet, client=False)
        if self.state is None:
            self.state = p.ProtocolState(event.connection_generation)
            self.state.accept_client(self.start_event, now=time.monotonic())


        if isinstance(event, p.Transcript) and event.speaker == "assistant" and (
                event.connection_generation == self.state.connection_generation and
                (event.generation < self.state.generation or
                 (event.generation == self.state.generation and not self.state.output_open))):
            if event.event_sequence != self.state.event_sequence or event.elapsed_ms < self.state.elapsed_ms:
                raise p.ProtocolError("invalid_sequence")
            self.state.event_sequence += 1
            self.state.elapsed_ms = event.elapsed_ms
            self.stale_drops += 1
            return
        self.validate_heartbeat_ack(event)
        self.state.accept_server(event)
        if isinstance(event, p.HeartbeatAck):
            self.pending_heartbeats.popleft()
        self.trace(safe_event(event))
        if isinstance(event, p.Ready):
            self.deadline = min(self.deadline, time.monotonic() + event.expires_in_ms / 1000)
            self.ready.set()
        elif isinstance(event, p.Interrupted):
            self.output.flush()
        elif isinstance(event, p.Generation) and event.phase == "completed":
            self.response_done.set()
        elif isinstance(event, p.HeartbeatAck):
            self.acks += 1
        elif isinstance(event, p.Error) and event.fatal:
            self.output.flush()
            raise ClientError("server_fatal_error")
        elif isinstance(event, p.Closed):
            self.reason = event.reason
            self.closed.set()
            self.output.flush()

    def validate_heartbeat_ack(self, event):
        if isinstance(event, p.HeartbeatAck) and (
                not self.pending_heartbeats or event.request_id != self.pending_heartbeats[0]):
            raise p.ProtocolError("invalid_sequence")

    def receive_shutdown_packet(self, packet):
        """Validate the peer's pre-Stop ordered stream, with zero data side effects.

        This is a bounded receive view, not a W02 contract change or authority.
        We cannot infer when the peer observes Stop from local send completion.
        Its terminal event fences this view permanently. Unsent ACKs may have
        been discarded by server cleanup; Closed never requires inventing them.
        """
        if time.monotonic() >= self._stop_deadline:
            raise ClientError("stop_timeout")
        if self.shutdown_packets >= p.MAX_QUEUED_EVENTS:
            raise p.ProtocolError("limit_exceeded")
        state = self._receive_drain
        if isinstance(packet, bytes):
            frame = p.decode_audio(packet, expected_kind="output")
            state.accept_audio(frame, now=time.monotonic())
            self.shutdown_discarded_packets += 1
        else:
            event = p.parse_json(packet, client=False)
            self.validate_heartbeat_ack(event)
            state.accept_server(event)
            if isinstance(event, p.HeartbeatAck):
                self.pending_heartbeats.popleft()
                self.acks += 1
            elif isinstance(event, p.Closed):
                self.state = state
                self.reason = event.reason
                self.closed.set()
                self.pending_heartbeats.clear()
                self.trace(safe_event(event))
            elif isinstance(event, p.Error) and event.fatal:
                raise ClientError("server_fatal_error")
            else:


                self.shutdown_discarded_packets += 1
        self.shutdown_packets += 1

    async def receiver(self):
        while not self.closed.is_set():
            self.receive_packet(await self.socket.recv())

    async def heartbeats(self):
        while True:
            await asyncio.sleep(self.heartbeat)
            if time.monotonic() >= self.deadline:
                raise ClientError("session_expired")
            if time.monotonic() - self.last_inbound >= 30:
                raise ClientError("heartbeat_timeout")
            await self.control("heartbeat")

    async def wait_event(self, event, timeout):
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait([waiter, *self.tasks], timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in self.tasks:
                if task in done:
                    task.result()
                    if not event.is_set():
                        raise ClientError("connection_closed")
            if not event.is_set():
                raise ClientError("voice_timeout")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def run(self, source):
        try:
            async with asyncio.timeout(2):
                await self.socket.send(p.encode_json(self.start_event, client=True))
            self.tasks = [asyncio.create_task(self.receiver()), asyncio.create_task(self.output.run())]
            await self.wait_event(self.ready, 5)
            heartbeat_task = asyncio.create_task(self.heartbeats())
            self.tasks.append(heartbeat_task)
            producer = asyncio.create_task(source(self))
            try:
                done, _ = await asyncio.wait([producer, *self.tasks], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if not producer.done():
                    raise ClientError("connection_closed")
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

            for task in self.tasks:
                if task.done():
                    task.result()
            async with self.send_lock:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            self.tasks.remove(heartbeat_task)
            if self.uncertain:
                raise ClientError("connection_uncertain")
            if self.state.phase == p.Phase.ACTIVE:
                await self.control("stop")
            remaining = max(0, self._stop_deadline - time.monotonic()) if self._stop_deadline is not None else 2
            await self.wait_event(self.closed, remaining)
            if self.reason != "stopped":
                raise ClientError("session_not_stopped")
            return self.summary()
        finally:
            self.output.flush()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            async with asyncio.timeout(2):
                await self.socket.close()

    async def finish_wav(self, pcm, response_timeout=10):
        await self.wav_turn(pcm)
        await self.wait_event(self.response_done, response_timeout)
        deadline = time.monotonic() + 2
        while self.output.bytes:
            if time.monotonic() >= deadline:
                raise ClientError("output_drain_timeout")
            for task in self.tasks:
                if task.done():
                    task.result()
            await asyncio.sleep(.005)



    def summary(self):
        return dict(input_bytes=self.sent_bytes, output_bytes=self.received_bytes,
            output_consumed_bytes=self.output.consumed, output_dropped_bytes=self.output.dropped,
            stale_drops=self.stale_drops, output_queue_peak_bytes=self.output.peak,
            shutdown_discarded_packets=self.shutdown_discarded_packets,
            heartbeat_acks=self.acks, first_output_from_start_ms=self.first_output_ms,
            elapsed_ms=round((time.monotonic() - self.started) * 1000, 2), closed_reason=self.reason)


async def microphone_turns(client, device, command):
    """Explicit start/end/stop operator; ready's implicit open is closed first."""
    await client.control("input", action="end")
    capture = None
    end_capture = asyncio.Event()
    async def pump():
        pacer = Pacer()
        while not end_capture.is_set():
            read = asyncio.create_task(device.read())
            stop = asyncio.create_task(end_capture.wait())
            try:
                await asyncio.wait([read, stop], return_when=asyncio.FIRST_COMPLETED)
                if end_capture.is_set():
                    return
                pcm = read.result()
                await pacer.wait(len(pcm) / 32000)
                if end_capture.is_set():
                    return


                await client.send_pcm(pcm)
            finally:
                read.cancel()
                stop.cancel()
                await asyncio.gather(read, stop, return_exceptions=True)
    try:
        while True:
            operator = asyncio.create_task(command())
            try:
                if capture is None:
                    action = await operator
                else:
                    done, _ = await asyncio.wait([operator, capture], return_when=asyncio.FIRST_COMPLETED)
                    if capture in done:
                        capture.result()
                        raise ClientError("capture_stopped")
                    action = operator.result()
            finally:
                operator.cancel()
                await asyncio.gather(operator, return_exceptions=True)
            if action == "start" and capture is None:
                end_capture.clear()
                await client.control("input", action="begin")
                device.start_input()
                capture = asyncio.create_task(pump())
            elif action in ("end", "stop"):
                if capture is not None:
                    end_capture.set()
                    async with asyncio.timeout(2.2):
                        await capture
                    capture = None
                    device.end_input()
                    await client.control("input", action="end")
                if action == "stop":
                    return
            if capture is not None and capture.done():
                capture.result()
    finally:
        if capture is not None:
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
        device.end_input()


def self_check(fixtures=None):
    fixtures = fixtures or Path(__file__).resolve().parents[2] / "tests/voice/fixtures/protocol"
    golden = json.loads((fixtures / "golden.json").read_text(encoding="utf-8"))
    for item in golden["vectors"]:
        raw = (fixtures / item["file"]).read_bytes()
        assert raw.hex() == item["hex"] and hashlib.sha256(raw).hexdigest() == item["sha256"]
        frame = p.decode_audio(raw, expected_kind=item["kind"])
        assert p.encode_audio(frame) == raw
        import struct
        assert list(struct.unpack("<8h", frame.pcm)) == golden["samples"]
    controls = json.loads((fixtures / "controls.json").read_text(encoding="utf-8"))
    state = p.ProtocolState(controls["connection_generation"])
    for item in controls["messages"]:
        client = item["direction"] == "client"
        event = p.parse_json(json.dumps(item["body"]), client=client)
        if client:
            state.accept_client(event, now=1.)
        else:
            state.accept_server(event)
    assert state.phase == p.Phase.CLOSED
    return dict(mode="self-check", evidence_class="windows_local", vectors=2,
                controls=len(controls["messages"]), status="passed")
