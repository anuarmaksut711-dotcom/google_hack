"""Real mounted FastAPI/ASGI boundary, actual W04 lifecycle, no sockets/Google."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket

import httpx
import pytest

from campuspulse_recovery.observability.voice_events import VoiceEvent
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.voice import protocol as p
from campuspulse_recovery.voice.coordinator import Limits
from campuspulse_recovery.voice.ports import OutputAudio, OutputTranscript, ResponseBoundary, ToolRequest, ToolResult
from campuspulse_recovery.voice.sessions import ResumeState, HistoryNote, _Claim, _path
from campuspulse_recovery.storage.plans import _decode
from campuspulse_recovery.web.app import create_app
from campuspulse_recovery.web.voice import (create_voice_app, VoiceConfig, ManagedBindings, WS_PATH,
    EphemeralVoiceSessions, authorize_current, voice_server_config)
from campuspulse_recovery.web.voice_pairing import PAGE_CSP, API_PATH
from tests.storage.test_plans import run
from tests.voice.test_coordinator import FakeProvider, control, settle
from tests.voice.test_device_auth import VERIFIER
from tests.voice.test_sessions import SessionSetup, TOKEN
from tests.web.test_approval import SyntheticVerifier
from campuspulse_recovery.voice.device_auth import proof_challenge

ORIGIN = "https://localhost"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network denied in W07")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


class Provider(FakeProvider):
    async def execute(self, access, request):
        access.require_tool(request.tool)
        return ToolResult("success", {"private": "synthetic-source-do-not-log"})


class Fixture:
    def __init__(self, **changes):
        self.h = SessionSetup()
        self.provider = Provider()
        self.events = []
        self.factories = 0
        def factory():
            self.factories += 1
            return self.provider
        async def journal(event):
            self.events.append(event)
        self.config = replace(VoiceConfig(enabled=True, mode=self.provider.mode,
            local_fake_factory=factory, journal=journal, clock=self.h.clock), **changes)
        self.app = create_voice_app(policy=self.h.policy, documents=self.h.documents,
            verifier=SyntheticVerifier(), origin=ORIGIN, environment="local",
            clock=self.h.clock, voice=self.config)


class ASGISocket:
    """No endpoint/transport mock: invoke the actual app with ASGI messages."""
    def __init__(self, app, *, headers=None, scheme="wss", query=b"", client="127.0.0.1",
            subprotocols=(), fail_accept=False, stall_audio=False):
        self.app = app
        self.incoming = asyncio.Queue(maxsize=32)
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.sent = []
        self.scope = dict(type="websocket", asgi={"version": "3.0", "spec_version": "2.4"},
            scheme=scheme, path=WS_PATH, raw_path=WS_PATH.encode(), query_string=query,
            root_path="", headers=headers if headers is not None else
                [(b"host", b"localhost"), (b"authorization", ("Bearer " + TOKEN).encode())],
            client=(client, 1234), server=("localhost", 443), subprotocols=list(subprotocols))
        self.fail_accept, self.stall_audio = fail_accept, stall_audio
        self.audio_waiting = asyncio.Event()
        self.audio_cancelled = False

    async def send(self, message):
        if message["type"] == "websocket.accept" and self.fail_accept:
            raise RuntimeError("synthetic secret accept failure")
        if message.get("bytes") is not None and self.stall_audio:
            self.audio_waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.audio_cancelled = True
                raise
        assert len(self.sent) < 160
        self.sent.append(message)

    def start(self):
        self.task = asyncio.create_task(self.app(self.scope, self.incoming.get, self.send))
        return self

    def put(self, value):
        if value is None:
            message = {"type": "websocket.disconnect", "code": 1000}
        else:
            message = {"type": "websocket.receive", "bytes" if isinstance(value, bytes) else "text": value}
        self.incoming.put_nowait(message)

    def controls(self):
        return [json.loads(m["text"]) for m in self.sent if m.get("text") is not None]

    async def ready(self):
        self.put(control("start"))
        await settle(lambda: any(e["type"] == "ready" for e in self.controls()))
        return self.controls()[0]

    async def finished(self):
        async with asyncio.timeout(2):
            await self.task
        assert any(m["type"] in ("websocket.close", "websocket.accept") for m in self.sent)


def test_actual_source_and_server_bounds():
    from campuspulse_recovery.web import voice
    root = Path(__file__).resolve().parents[2]
    assert Path(voice.__file__).resolve() == root / "src/campuspulse_recovery/web/voice.py"
    cfg = voice_server_config(Fixture().app)
    cfg.load()
    assert cfg.ws == "websockets"
    assert cfg.ws_max_size == 2048 and cfg.ws_max_queue == 4
    assert cfg.ws_per_message_deflate is False and cfg.proxy_headers is False
    assert cfg.access_log is False and cfg.log_level == "critical"


def test_actual_uvicorn_protocol_limits_before_payload_allocation():
    async def scenario():
        from uvicorn.server import ServerState
        from websockets.exceptions import PayloadTooBig
        from websockets.legacy.framing import Frame
        cfg = voice_server_config(Fixture().app)
        cfg.load()
        server = cfg.ws_protocol_class(cfg, ServerState(), {})
        assert server.max_size == 2048 and server.max_queue == 4
        assert not server.available_extensions


        wire = bytearray(b"\x82\xfe" + (2049).to_bytes(2, "big"))
        read_sizes = []
        async def read_exact(size):
            read_sizes.append(size)
            assert size <= len(wire)
            result = bytes(wire[:size]); del wire[:size]
            return result
        with pytest.raises(PayloadTooBig):
            await Frame.read(read_exact, mask=True, max_size=server.max_size)
        assert read_sizes == [2, 2]
    run(scenario())


@pytest.mark.parametrize("changes", [
    {"headers": [(b"host", b"localhost")]},
    {"query": b"token=do-not-log"},
    {"headers": [(b"host", b"localhost"), (b"authorization", b"Bearer invalid")]},
    {"headers": [(b"host", b"localhost"), (b"authorization", ("Bearer "+TOKEN).encode()),
        (b"authorization", ("Bearer "+TOKEN).encode())]},
    {"headers": [(b"host", b"localhost"), (b"host", b"localhost"),
        (b"authorization", ("Bearer "+TOKEN).encode())]},
    {"headers": [(b"host", b"localhost"), (b"authorization", ("Bearer "+TOKEN).encode()),
        (b"cookie", b"__Host-cp-session=synthetic-parent")]},
    {"headers": [(b"host", b"localhost"), (b"cookie", b"__Host-cp-session=synthetic-parent"),
        (b"origin", ORIGIN.encode()), (b"x-csrf-token", b"synthetic-csrf")]},
    {"headers": [(b"host", b"localhost"), (b"authorization", ("Bearer "+TOKEN).encode()),
        (b"origin", ORIGIN.encode())]},
    {"headers": [(b"host", b"evil.invalid"), (b"authorization", ("Bearer "+TOKEN).encode())]},
    {"scheme": "ws", "headers": [(b"host", b"localhost"),
        (b"authorization", ("Bearer "+TOKEN).encode()), (b"x-forwarded-proto", b"https")]},
    {"client": "10.1.2.3"}, {"subprotocols": ["unsupported-v2"]},
])
def test_denied_before_accept_data_or_provider(changes, caplog):
    async def scenario():
        f = Fixture()
        s = ASGISocket(f.app, **changes).start()
        await s.finished()
        assert s.sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
        assert f.factories == 0 and not f.events
    run(scenario())
    assert TOKEN not in caplog.text and "do-not-log" not in caplog.text


def test_duplex_tool_trace_transcript_ephemeral_stop(caplog):
    async def scenario():
        f = Fixture()
        s = ASGISocket(f.app).start()
        ready = await s.ready()
        gen = ready["connection_generation"]
        s.put(p.encode_audio(p.AudioFrame("input", gen, 0, 0, b"\0\0" * 320)))
        f.provider.stream.emit(ResponseBoundary(1, "started"))
        f.provider.stream.emit(OutputAudio(1, b"\0\0" * 480))
        f.provider.stream.emit(OutputTranscript(1, "transcript_synthetic", "assistant", 0, True,
            "synthetic-private-transcript"))
        f.provider.stream.emit(ToolRequest("synthetic_call", "read_assignments", {}))
        await settle(lambda: bool(f.provider.stream.received) and
            any(e["type"] == "tool_finished" for e in s.controls()))
        assert any(m.get("bytes") for m in s.sent)
        assert any(e["type"] == "transcript" for e in s.controls())
        s.put(control("stop", generation=gen))
        await s.finished()
        assert f.provider.stream.closed and f.provider.stream.cancelled
        assert s.controls()[-1]["reason"] == "stopped"
        assert all(type(e) is VoiceEvent for e in f.events)
        assert {e.kind for e in f.events} == {"ready", "tool_started", "tool_finished", "closed"}
        finished = next(e for e in f.events if e.kind == "tool_finished")
        assert finished.status == "success" and finished.duration_ms >= 0
        assert finished.run_id == ready["run_id"] and finished.call_id.startswith("ref_")
        raw = repr(f.h.documents.data)
        assert "synthetic-private-transcript" not in raw
        assert 'synthetic_call' in raw
        traces = json.dumps([e.model_dump(mode="json") for e in f.events])
        for secret in (TOKEN, "synthetic-private-transcript", "synthetic-source-do-not-log", "receipt"):
            assert secret not in traces
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())
    assert "synthetic-private-transcript" not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize("packet", [b"x" * 1305, "x" * 2049, "я" * 1025,
    '{"version":"2","type":"start"}', '{"version":"1","type":"unknown"}'],
    ids=["large_binary", "large_text", "large_utf8", "version", "unknown"])
def test_large_or_invalid_message_closes_upstream(packet):
    async def scenario():
        f = Fixture()
        s = ASGISocket(f.app).start()
        await s.ready()
        s.put(packet)
        await s.finished()
        assert s.controls()[-1]["reason"] == "protocol_error"
        assert f.provider.stream.closed and f.provider.stream.cancelled
    run(scenario())


@pytest.mark.parametrize("reason", ["disconnect", "revoke", "expiry", "timeout", "upstream"])
def test_terminal_cleanup(reason):
    async def scenario():
        f = Fixture()
        if reason == "expiry":
            from datetime import timedelta
            f.h.replace_credential(expires_at=f.h.clock.value + timedelta(seconds=3))
        s = ASGISocket(f.app).start()
        await s.ready()
        if reason == "disconnect":
            s.put(None)
        elif reason == "revoke":
            f.h.replace_parent(revoked=True)
            f.h.clock.advance(10)
        elif reason in ("expiry", "timeout"):
            f.h.clock.advance(4 if reason == "expiry" else 31)
        else:
            f.provider.stream.emit(RuntimeError("private provider failure do-not-log"))
        await s.finished()
        assert f.provider.stream.closed and f.provider.stream.cancelled
        assert f.events[-1].kind == "closed"
        if reason != "disconnect":
            assert s.controls()[-1]["reason"] == dict(revoke="revoked", expiry="expired",
                timeout="timeout", upstream="upstream_error")[reason]
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_initial_timeout_and_control_rate_flood(monkeypatch):
    async def scenario():
        f = Fixture()
        s = ASGISocket(f.app).start()
        await s.finished()
        assert not f.provider.opened.is_set()
        f = Fixture()
        s = ASGISocket(f.app).start()
        ready = await s.ready()
        for _ in range(21):
            s.put(control("heartbeat", generation=ready["connection_generation"]))
        await s.finished()
        assert s.controls()[-1]["reason"] == "protocol_error"
        assert f.provider.stream.closed
    monkeypatch.setattr(p, "START_TIMEOUT_MS", 30)
    run(scenario())


def test_default_durable_journal_and_ephemeral_checkpoint():
    async def scenario():
        f = Fixture(journal=None)
        s = ASGISocket(f.app).start()
        await s.ready()
        s.put(None)
        await s.finished()
        entries = [_decode(v, "voice_event", VoiceEvent) for k, v in f.h.documents.data.items()
            if k.startswith("cp_voice_events/")]
        assert {e.kind for e in entries} == {"ready", "closed"}
        assert next(e for e in entries if e.kind == "closed").status == "disconnected"
        sessions = EphemeralVoiceSessions(f.h.sessions.devices)
        lease, _ = await sessions.acquire(TOKEN)
        await sessions.checkpoint(lease, ResumeState(history=(HistoryNote(speaker="user", text="private text"),),
            seen_call_ids=("retired",), transport_token="private-handle", transport_expires_at=lease.deadline))
        claim = _decode(f.h.documents.data[_path(lease.device_id)], "voice_stream", _Claim)
        assert not claim.resume.history and claim.resume.transport_token is None
        assert claim.resume.seen_call_ids == ("retired",)
        await sessions.release(lease)
    run(scenario())


def test_managed_real_provider_runtime_and_typed_bridge_through_route(monkeypatch):
    """Managed mode != cloud evidence: real SDK frames, synthetic transports."""
    async def scenario():
        from tests.voice.test_managed_voice import components, target, credentials
        from tests.voice.test_live_agent import audio, fc, transcription
        from google.adk.models.llm_response import LlmResponse
        from campuspulse_recovery.voice.ports import ProviderMode
        import vertexai
        x = await components(monkeypatch)
        monkeypatch.setattr(vertexai, "Client", x.sdk)
        async def issue(sessions, lease):
            assert sessions.documents is x.h.documents
            return await x.issue(lease)
        async def creds(): return credentials()

        cfg = VoiceConfig(enabled=True, mode=ProviderMode.MANAGED_PRODUCTION,
            managed=ManagedBindings(target(), lambda *a: True, issue, creds), limits=Limits(check_seconds=.02))
        app = create_voice_app(policy=x.h.policy, documents=x.h.documents, verifier=SyntheticVerifier(),
            origin=ORIGIN, environment="local", clock=x.h.clock, voice=cfg)
        s = ASGISocket(app).start()
        s.put(control("start"))
        async with asyncio.timeout(3):
            while not any(e["type"] == "ready" for e in s.controls()):
                assert not s.task.done()
                await asyncio.sleep(.001)
        lease = x.sessions[0].policy.lease


        x.h.clock.advance(10)
        async with asyncio.timeout(1):
            while _decode(x.h.documents.data[_path(lease.device_id)], "voice_stream", _Claim).lease.lease_until <= lease.lease_until:
                await asyncio.sleep(.001)
        args = {"horizon": x.base.constraints.horizon.model_dump(mode="json")}
        x.connection.put(fc(("read_assignments", "route_read", args)))
        async with asyncio.timeout(2):
            while not any(e["type"] == "tool_finished" for e in s.controls()):
                assert not s.task.done()
                await asyncio.sleep(.001)
        assert len(x.mcp.calls) == 1
        await settle(lambda: bool(x.connection.sent))
        assert x.connection.sent[0].parts[0].function_response.response["status"] == "read"
        x.connection.put(audio())
        x.connection.put(transcription(x.sessions[0].policy.speech()))
        x.connection.put(LlmResponse(turn_complete=True))
        async with asyncio.timeout(2):
            while not any(m.get("bytes") for m in s.sent):
                await asyncio.sleep(.001)
        s.put(control("stop", generation=lease.connection_generation))
        await s.finished()
        assert x.connection.closed and x.sdk.wires[0].closed
        assert x.sdk.wires[0].task.done() and not x.sessions[0].bridge.pending
        events = [_decode(v, "voice_event", VoiceEvent) for k, v in x.h.documents.data.items()
            if k.startswith("cp_voice_events/")]
        assert all(e.evidence_class == "windows_local" for e in events)
        assert {e.kind for e in events} == {"ready", "tool_started", "tool_finished", "closed"}
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_slow_consumer_cancelled_and_joined():
    async def scenario():
        f = Fixture(limits=Limits(send_seconds=.03, close_seconds=.3))
        s = ASGISocket(f.app, stall_audio=True).start()
        await s.ready()
        f.provider.stream.emit(ResponseBoundary(1, "started"))
        f.provider.stream.emit(OutputAudio(1, b"\0\0" * 480))
        await s.audio_waiting.wait()
        await s.finished()
        assert s.audio_cancelled
        assert s.controls()[-1]["reason"] == "backpressure"
        assert f.provider.stream.closed
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_failed_accept_and_disconnect_during_open():
    async def scenario():
        f = Fixture()
        s = ASGISocket(f.app, fail_accept=True).start()
        await s.finished()
        assert not f.provider.opened.is_set()
        f = Fixture()
        f.provider.stall_open = True
        s = ASGISocket(f.app).start()
        s.put(control("start"))
        await f.provider.opened.wait()
        s.put(None)
        await s.finished()
        assert f.provider.open_cancelled
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_durable_current_authority_renewal_no_reentrant_lock():
    async def scenario():
        h = SessionSetup()
        initial, _ = await h.sessions.acquire(TOKEN)
        h.clock.advance(10)
        renewed = await h.sessions.renew(initial)
        h.clock.advance(6)
        assert await authorize_current(h.sessions, initial)
        await h.sessions.release(renewed)
        with pytest.raises(AccessDenied):
            await authorize_current(h.sessions, initial)
    run(scenario())


@pytest.mark.parametrize("change", ["renew", "revoke", "takeover", "expiry"])
def test_authorizer_race_uses_full_durable_check(change):
    async def scenario():
        from tests.web.test_approval import ScheduledDocuments
        docs = ScheduledDocuments()
        h = SessionSetup(documents=docs)
        lease, _ = await h.sessions.acquire(TOKEN)

        docs.arm("check", "before")
        task = asyncio.create_task(authorize_current(h.sessions, lease))
        await docs.started.wait()
        if change in ("renew", "revoke"):
            h.clock.advance(10)
            current = await h.sessions.renew(lease)
            if change == "revoke": h.replace_parent(revoked=True)
        elif change == "takeover":
            h.clock.advance(16)
            current, _ = await h.sessions.acquire(TOKEN)
        else:
            h.clock.advance(541)
        docs.resume.set()
        if change == "renew":
            assert await task
            await h.sessions.release(current)
        else:
            with pytest.raises(AccessDenied):
                await task
    run(scenario())


def test_authorizer_refences_after_delayed_success():
    async def scenario():
        from tests.storage.test_plans import FakeDocuments
        class DelayedResult(FakeDocuments):
            async def transact(self, callback):
                result = await super().transact(callback)
                if getattr(self, "armed", False) and callback.__name__ == "check":
                    self.armed = False
                    self.clock.advance(16)
                return result
        docs = DelayedResult()
        h = SessionSetup(documents=docs)
        docs.clock = h.clock
        lease, _ = await h.sessions.acquire(TOKEN)
        docs.armed = True
        with pytest.raises(AccessDenied, match="expired"):
            await authorize_current(h.sessions, lease)
    run(scenario())


def test_disabled_and_invalid_config_fail_closed(monkeypatch):
    h = SessionSetup()
    args = dict(policy=h.policy, documents=h.documents, verifier=SyntheticVerifier(),
        origin=ORIGIN, environment="local", clock=h.clock)
    for cfg in (None, VoiceConfig()):
        app = create_voice_app(**args, voice=cfg)
        assert WS_PATH not in [r.path for r in app.routes]
    with pytest.raises(ValueError, match="verified_managed"):
        create_voice_app(**args, voice=VoiceConfig(enabled=True))
    f = Fixture()
    with pytest.raises(ValueError, match="loopback"):
        create_voice_app(**(args | {"origin": "https://campus.test"}), voice=f.config)
    monkeypatch.setenv("K_SERVICE", "synthetic-cloud")
    with pytest.raises(ValueError):
        create_voice_app(**args, voice=f.config)


@pytest.mark.parametrize("missing", ["capability", "project", "credentials"])
def test_managed_startup_rejects_unverified_bindings(missing):
    from tests.voice.test_managed_voice import target
    h = SessionSetup()
    async def issue(*a): raise AssertionError("startup must not issue references")
    async def creds(): raise AssertionError("startup must not acquire credentials")
    binding = ManagedBindings(target(), lambda *a: True, issue, creds)
    if missing == "capability": binding = replace(binding, verify_deployment=lambda *a: False)
    if missing == "project": binding = replace(binding, target=target().model_copy(update={"project_id": "other-test-42"}))
    if missing == "credentials": binding = replace(binding, credentials_provider=None)
    with pytest.raises(ValueError, match="verified_managed"):
        create_voice_app(policy=h.policy, documents=h.documents, verifier=SyntheticVerifier(),
            origin=ORIGIN, environment="local", clock=h.clock,
            voice=VoiceConfig(enabled=True, managed=binding))


@pytest.mark.parametrize("termination", ["signout", "device_revoke"])
def test_pairing_actual_factory_headers_csrf_signout_and_native_revoke(termination):
    async def scenario():
        f = Fixture()
        def client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url=ORIGIN)
        async with client() as browser, client() as native:
            challenge = await browser.get("/auth/challenge")
            signed = await browser.post("/auth/signin", headers={"Origin": ORIGIN,
                "X-CSRF-Token": challenge.json()["csrf_token"]}, json={"credential": "synthetic-signed-token"})
            headers = {"Origin": ORIGIN, "X-CSRF-Token": signed.json()["csrf_token"]}


            from tests.web.test_approval import Setup, action
            setup = Setup(documents=f.h.documents)
            plan = await setup.seed_current()
            assert (await browser.post("/api/approval", json=action(plan))).status_code == 403
            consent = await browser.post("/api/approval", headers=headers, json=action(plan))
            assert consent.status_code == 200 and consent.json()["approval"]["status"] == "active"
            from starlette.requests import Request
            from campuspulse_recovery.web.auth import WebAuth, SESSION_COOKIE
            auth_a, auth_b = [WebAuth(f.h.documents, f.h.policy, verifier=SyntheticVerifier(),
                origin=ORIGIN, environment="local", clock=f.h.clock) for _ in range(2)]
            request = Request(dict(type="http", scheme="https", path="/auth/session", query_string=b"",
                server=("localhost", 443), headers=[(b"host", b"localhost"),
                (b"cookie", (SESSION_COOKIE + "=" + browser.cookies.get(SESSION_COOKIE)).encode())]))
            local_a = await auth_a.load(request)
            with pytest.raises(AccessDenied, match="server_session_credential_required"):
                with auth_b.request_scope(local_a): pass
            with auth_b.request_scope(await auth_b.load(request)) as context:
                assert context.session_id == local_a.session.binding.session_id
            page = await browser.get("/voice/pair")
            assert page.status_code == 200 and page.headers["content-security-policy"] == PAGE_CSP
            assert (await browser.get("/auth/session")).headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
            offer = (await native.post(API_PATH + "/start", json={"challenge": proof_challenge(VERIFIER),
                "client_kind": "windows", "device_name": "Synthetic W07"})).json()
            action = {"code": offer["pairing_code"]}
            assert (await native.post(API_PATH + "/decision", json=action | {"decision": "approve"})).status_code == 403
            assert (await browser.post(API_PATH + "/preview", json=action)).status_code == 403
            assert (await browser.post(API_PATH + "/preview", headers=headers | {"Origin": "https://evil.invalid"},
                json=action)).status_code == 403
            assert (await browser.post(API_PATH + "/preview", headers=headers, json=action)).status_code == 200
            assert (await browser.post(API_PATH + "/decision", headers=headers,
                json=action | {"decision": "approve"})).status_code == 200
            issued = (await native.post(API_PATH + "/exchange", json={"grant": offer["grant"], "verifier": VERIFIER})).json()
            s = ASGISocket(f.app, headers=[(b"host", b"localhost"),
                (b"authorization", ("Bearer " + issued["credential"]).encode())]).start()
            await s.ready()
            if termination == "signout":
                assert (await browser.post("/auth/signout", headers=headers, json={})).status_code == 200
            else:
                assert (await browser.post(API_PATH + "/revoke", headers=headers, json=action)).status_code == 200
            f.h.clock.advance(10)
            await s.finished()
            assert s.controls()[-1]["reason"] == "revoked"
    run(scenario())


@pytest.mark.parametrize("terminal", ["stop", "disconnect", "request_cancel"])
def test_terminal_during_tool_cancels_and_retires(terminal):
    async def scenario():
        f = Fixture()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def execute(access, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        f.provider.execute = execute
        s = ASGISocket(f.app).start()
        ready = await s.ready()
        f.provider.stream.emit(ToolRequest("retired_route_call", "read_calendar", {}))
        await entered.wait()
        if terminal == "request_cancel":
            s.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await s.task
        else:
            s.put(control("stop", generation=ready["connection_generation"]) if terminal == "stop" else None)
            await s.finished()
        assert cancelled.is_set() and f.provider.stream.closed and f.provider.stream.cancelled
        assert "retired_route_call" in repr(f.h.documents.data)
        assert f.events[-1].kind == "closed" and not f.events[-1].cleanup_failed
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_concurrent_routes_share_durable_fence():
    async def scenario():
        f = Fixture()
        first = ASGISocket(f.app).start()
        ready = await first.ready()
        second = ASGISocket(f.app).start()
        second.put(control("start"))
        await second.finished()
        assert second.sent[-1]["type"] == "websocket.close" and not second.controls()
        assert not first.task.done()
        first.put(control("stop", generation=ready["connection_generation"]))
        await first.finished()
        assert len(f.provider.resumes) == 1
    run(scenario())
