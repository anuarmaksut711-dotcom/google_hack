"""Windows-local real pinned SDK serialization + synthetic network transport.

No ADC, Gemini, Google resources, cloud IAM or microphone. The main integration
uses the actual W04 coordinator, W05 Runner/tool bridge and official McpTool;
only Gemini transport and MCP transport/discovery are synthetic.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
import importlib.metadata
import inspect
import json
import logging
from pathlib import Path
import socket
from types import SimpleNamespace

import cloudpickle
from google.oauth2.credentials import Credentials
import pytest

from campuspulse_recovery.cloud import voice_client as client, voice_runtime as runtime
from campuspulse_recovery.security.context import (AccessDenied, AuthenticatedCaller,
                                                   RequestContextFactory)
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.voice import protocol as p
from campuspulse_recovery.voice.coordinator import Limits, VoiceCoordinator
from campuspulse_recovery.voice.ports import (OutputAudio, OutputTranscript, ProviderMode,
    ResponseBoundary, ToolRequest, ToolResult)
from campuspulse_recovery.voice.sessions import HistoryNote, ResumeState, _Claim, _path
from campuspulse_recovery.web.agent_client import AgentEngineTarget
from tests.storage.test_plans import run
from tests.voice.test_coordinator import FakeClient, control, settle
from tests.voice.test_live_agent import (Connection, FakeLiveModel, audio, config, fc,
                                        patch_discovery, transcription)
from tests.voice.test_sessions import SessionSetup, TOKEN
from tests.voice.test_voice_policy import McpSession, setup_policy


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("W06 network denied")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def target():
    return AgentEngineTarget(project_id="campus-test-42", location="us-central1",
        resource_name="projects/123456/locations/us-central1/reasoningEngines/789")


def credentials():
    return Credentials(token="synthetic-platform-oauth", expiry=(datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None))


@contextmanager
def scope(h, binding, boundary):
    principal = (h.config.student.allowed_signin_principal if boundary == "backend"
                 else h.config.identities.backend_service_account)
    caller = AuthenticatedCaller(principal_id=principal, session_id=binding.session_id,
        run_id=binding.run_id, authenticated_at=h.clock(), expires_at=binding.expires_at)
    factory = RequestContextFactory(h.policy, boundary=boundary, clock=h.clock,
        authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
        bindings=SimpleNamespace(resolve=lambda **k: binding))
    with factory.request(object()) as context:
        yield context


class Wire:
    """Mimics ONLY network: real SDK send wrapper + one-object receive codec."""
    def __init__(self, app):
        from vertexai._genai.live_agent_engines import AsyncLiveAgentEngineSession
        self.app = app
        self.input = asyncio.Queue(maxsize=32)
        self.output = asyncio.Queue(maxsize=32)
        self.sent = []
        self.task = None
        self.closed = False
        self.sdk = AsyncLiveAgentEngineSession(self)
        self.stream = None
    async def send(self, value):
        self.sent.append(json.loads(value))
        await self.input.put(json.loads(value)["bidi_stream_input"])
    async def recv(self):
        value = await self.output.get()
        if isinstance(value, Exception): raise value
        return value
    async def pump(self):
        self.stream = self.app.bidi_stream_query(self.input)
        try:
            async for item in self.stream:
                await self.output.put(json.dumps(item))
        finally:
            await self.stream.aclose()
    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class SdkFactory:
    def __init__(self, app):
        self.app = app
        self.wires = []
        self.closed = 0
        self.entered = asyncio.Event()
        self.open_gate = self.send_gate = self.close_gate = None
    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(agent_engines=self),
            aclose=self.aclose), _api_client=SimpleNamespace(close=self.sync_close))
    @asynccontextmanager
    async def connect(self, *, agent_engine, config):
        assert agent_engine == target().resource_name
        assert config == {"class_method": "bidi_stream_query"}
        self.entered.set()
        if self.open_gate: await self.open_gate.wait()
        wire = Wire(self.app)
        self.wires.append(wire)
        wire.task = asyncio.create_task(wire.pump())
        try:
            yield wire.sdk
        finally:
            await wire.close()
    async def aclose(self): self.closed += 1
    def sync_close(self): self.closed += 1


async def components(monkeypatch, *, session_factory=None):
    h, base, _ = await setup_policy()
    await h.sessions.release(base.lease)
    invocations = runtime.VoiceInvocations(h.sessions)
    connection = Connection()
    mcp = McpSession(base)
    patch_discovery(monkeypatch, mcp)
    sessions = []
    from campuspulse_recovery.agent.voice_agent import LocalAdkSession
    def session(policy, cfg):
        mcp.policy = policy
        value = (session_factory(policy, cfg) if session_factory else
                 LocalAdkSession(policy, cfg, model=FakeLiveModel(connection)))
        sessions.append(value)
        return value
    async def ingress(): return h.config.identities.backend_service_account
    app = runtime.ManagedVoiceRuntime(invocations=invocations, ingress=ingress,
        scope_factory=lambda b: scope(h, b, "agent"), config=config(),
        constraints_factory=lambda lease: base.constraints, session_factory=session)
    sdk = SdkFactory(app)
    async def issue(lease):
        with scope(h, lease.binding, "backend") as context:
            return await invocations.issue(context, lease)
    async def authorize(lease):
        # Backend reads current durable lease too; no initial-expiry authority.
        async def current(tx):
            value = _decode(await tx.get(_path(lease.device_id)), "voice_stream", _Claim)
            if not runtime.same_fence(value.lease, lease): raise AccessDenied("successor")
            _, access, _ = await h.sessions._current(tx, value.lease)
            return access
        return await h.sessions._run(current)
    async def creds(): return credentials()
    def provider():
        return client.ManagedVoiceProvider(target(), issue_reference=issue,
            authorize=authorize, credentials_provider=creds, client_factory=sdk)
    return SimpleNamespace(h=h, base=base, invocations=invocations, app=app, sdk=sdk,
        connection=connection, mcp=mcp, sessions=sessions, provider=provider, issue=issue)


def test_real_pinned_signatures_registration_serialization_and_source():
    from vertexai._genai import live_agent_engines as sdk, _agent_engines_utils as utils
    import vertexai
    assert importlib.metadata.version("google-cloud-aiplatform") == "1.165.1"
    assert importlib.metadata.version("google-adk") == "2.6.3"
    assert importlib.metadata.version("mcp") == "1.30.0"
    assert "config" in inspect.signature(sdk.AsyncLiveAgentEngines.connect).parameters
    assert "query_input" in inspect.signature(sdk.AsyncLiveAgentEngineSession.send).parameters
    assert not inspect.isasyncgenfunction(sdk.AsyncLiveAgentEngineSession.receive)
    assert "credentials" in inspect.signature(vertexai.Client).parameters
    obj = runtime.ManagedVoiceRuntime(invocations=object(), ingress=object(), scope_factory=object(),
        config=object(), constraints_factory=object())
    specs = utils._generate_class_methods_spec_or_raise(agent=obj, operations=obj.register_operations())
    assert len(specs) == 1
    assert specs[0]["name"] == "bidi_stream_query" and specs[0]["api_mode"] == "bidi_stream"
    restored = cloudpickle.loads(cloudpickle.dumps(obj))
    assert restored.register_operations() == {"bidi_stream": ["bidi_stream_query"]}
    path = Path(runtime.__file__).resolve()
    assert path.parts[-4:] == ("src", "campuspulse_recovery", "cloud", "voice_runtime.py")
    print("SDK1.165.1 registration/client signatures + cloudpickle: actual; source=", path)


def test_real_sdk_connect_setup_oauth_and_one_object_receive(monkeypatch):
    from vertexai._genai import live_agent_engines as sdk
    import vertexai
    async def scenario():
        messages = []
        class Socket:
            async def send(self, value): messages.append(json.loads(value))
            async def recv(self): return '{"synthetic": true}'
            async def close(self): pass
        @asynccontextmanager
        async def ws(uri, **kwargs):
            assert "BidiQueryReasoningEngine" in uri
            assert kwargs["additional_headers"]["Authorization"] == "Bearer synthetic-platform-oauth"
            yield Socket()
        monkeypatch.setattr(sdk, "ws_connect", ws)
        api = vertexai.Client(project=target().project_id, location=target().location, credentials=credentials())
        try:
            async with api.aio.live.agent_engines.connect(agent_engine=target().resource_name,
                    config={"class_method": "bidi_stream_query"}) as connection:
                await connection.send(query_input={"v": 1})
                assert await connection.receive() == {"synthetic": True}
            assert messages == [{"setup": {"name": target().resource_name, "class_method": "bidi_stream_query"}},
                                {"bidi_stream_input": {"v": 1}}]
        finally:
            await api.aio.aclose()
            api._api_client.close()
    run(scenario())


def test_managed_w04_w05_duplex_tools_renewal_and_output(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        provider = x.provider()
        coordinator = VoiceCoordinator(x.h.sessions, provider, selected_mode=ProviderMode.MANAGED_PRODUCTION,
            production=True, tools=provider, clock=x.h.clock)
        downstream = FakeClient()
        task = asyncio.create_task(coordinator.run(TOKEN, downstream))
        try:
            await asyncio.wait_for(downstream.ready.wait(), 3)
            assert len(x.sessions) == 1
            lease = coordinator.lease
            downstream.put(p.encode_audio(p.AudioFrame("input", lease.connection_generation, 0, 0, b"\x00\x00" * 320)))
            await settle(lambda: len(x.connection.realtime) >= 2)
            # Renewal changes the full DTO; runtime must reload the durable claim.
            x.h.clock.advance(10)
            await settle(lambda: coordinator.lease.lease_until > lease.lease_until)
            args = {"horizon": x.base.constraints.horizon.model_dump(mode="json")}
            x.connection.put(fc(("read_assignments", "managed_a", args), ("read_calendar", "managed_c", args)))
            await settle(lambda: len(x.connection.sent) == 1)
            assert len(x.mcp.calls) == 2
            assert set(coordinator.resume.seen_call_ids) == {"managed_a", "managed_c"}
            assert x.sessions[0].policy.lease == coordinator.lease
            assert all(part.function_response.response["status"] == "read" for part in x.connection.sent[0].parts)
            # The network contains correlation receipts, never MCP source data.
            ack = [m["bidi_stream_input"] for m in x.sdk.wires[0].sent
                   if m["bidi_stream_input"]["kind"] == "result_ack"]
            assert len(ack) == 2 and all(set(a["body"]) == {"call_id", "status", "receipt"} for a in ack)
            x.connection.put(audio())
            x.connection.put(transcription(x.sessions[0].policy.speech()))
            from google.adk.models.llm_response import LlmResponse
            x.connection.put(LlmResponse(turn_complete=True))
            for _ in range(2000):
                await asyncio.sleep(0)
                if any(isinstance(v, p.AudioFrame) for v in downstream.sent): break
            assert any(isinstance(v, p.AudioFrame) for v in downstream.sent)
            downstream.put(control("stop", coordinator.lease.connection_generation))
            result = await task
            assert not result.cleanup_failed
            assert x.connection.closed and x.sdk.wires[0].closed
            assert x.sdk.wires[0].task.done()
            assert not x.sessions[0].bridge.pending
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


@pytest.mark.parametrize("change", ["principal", "unknown_reference", "expired", "revoked", "takeover", "resources", "owner", "session", "extra"])
def test_runtime_rejects_forged_or_expired_invocation(monkeypatch, change):
    async def scenario():
        x = await components(monkeypatch)
        lease, _ = await x.h.sessions.acquire(TOKEN)
        reference = await x.issue(lease)
        body = {"reference": reference}
        if change == "principal":
            async def wrong(): return "forged-backend"
            x.app.ingress = wrong
        elif change == "unknown_reference": body["reference"] = "x" * 43
        elif change == "expired": x.h.clock.advance(541)
        elif change == "revoked": x.h.replace_credential(revoked=True)
        elif change == "takeover":
            x.h.clock.advance(16)
            await x.h.sessions.acquire(TOKEN)
        elif change == "resources":
            record = _decode(x.h.documents.data[_path(lease.device_id)], "voice_stream", _Claim)
            forged = type(lease).model_validate(lease.model_dump() | {"resource_hash": "f" * 64})
            x.h.documents.data[_path(lease.device_id)] = _encode("voice_stream", _Claim(lease=forged))
        else: body[change] = "forged"
        queue = asyncio.Queue()
        await queue.put(runtime.envelope(0, "open", body))
        stream = x.app.bidi_stream_query(queue)
        assert (await anext(stream))["kind"] == "error"
        await stream.aclose()
        assert x.sessions == [] and x.mcp.calls == []
    run(scenario())


def test_reference_consumption_and_renewal_fence(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        lease, _ = await x.h.sessions.acquire(TOKEN)
        reference = await x.issue(lease)
        principal = x.h.config.identities.backend_service_account
        await x.invocations.resolve(reference, principal, "consumer_a", redeem=True)
        x.h.clock.advance(10)
        latest = await x.h.sessions.renew(lease)
        resolved, _ = await x.invocations.resolve(reference, principal, "consumer_a")
        assert resolved == latest and resolved != lease
        for who, redeem in (("consumer_b", False), ("consumer_a", True), ("consumer_b", True)):
            with pytest.raises(AccessDenied):
                await x.invocations.resolve(reference, principal, who, redeem=redeem)
    run(scenario())


@pytest.mark.parametrize("value", [None, [], {"v": True}, {"v": 1, "seq": 0, "kind": "input", "body": {}, "principal": "x"},
    {"v": 1, "seq": 0, "kind": "input", "body": {"x": "x" * 25000}}])
def test_envelope_bounds(value):
    with pytest.raises(ValueError, match="managed_voice_envelope_rejected"):
        runtime.decode_envelope(value)


@pytest.mark.parametrize("event", [ResponseBoundary(1, "interrupted"), OutputAudio(1, b"\x00\x00"),
    OutputTranscript(1, "text_1", "assistant", 0, True, "Синтетический ответ"),
    ToolRequest("call_1", "read_calendar", {"horizon": {}})])
def test_normalized_event_roundtrip(event):
    assert runtime.decode_event(runtime.encode_event(event)) == event


def test_safe_sdk_logs_and_bounded_errors(caplog):
    async def scenario():
        caplog.set_level(logging.WARNING)
        with runtime.private_logs():
            try:
                raise ValueError("synthetic-private-audio-and-token")
            except Exception:
                logging.getLogger("vertexai_genai.future.nested").exception("synthetic-private-audio-and-token")
        assert "synthetic-private" not in caplog.text
        assert "managed_voice_sdk_event_redacted" in caplog.text
        async def bad(): raise RuntimeError("synthetic-private-upstream")
        with pytest.raises(ValueError, match="^managed_voice_unavailable$"):
            await runtime.bounded(bad())
        async def stall(): await asyncio.Event().wait()
        with pytest.raises(ValueError, match="^managed_voice_unavailable$"):
            await runtime.bounded(stall(), .01)
        logging.getLogger("vertexai_genai.future.nested").warning("ordinary-unrelated-record")
        assert "ordinary-unrelated-record" in caplog.text
    run(scenario())


def test_missing_production_binding_fails_closed():
    with pytest.raises(ValueError):
        client.ManagedVoiceProvider(target(), issue_reference=None, authorize=None, credentials_provider=None)
    with pytest.raises(ValueError):
        runtime.ManagedVoiceRuntime(invocations=None, ingress=None, scope_factory=None, config=None, constraints_factory=None)


@pytest.mark.parametrize("fault", ["unretired", "wrong_call", "forged_result", "duplicate_dispatch", "wrong_receipt"])
def test_runtime_tool_correlation_cannot_be_forged(monkeypatch, fault):
    async def scenario():
        x = await components(monkeypatch)
        lease, resume = await x.h.sessions.acquire(TOKEN)
        provider = x.provider()
        session = await provider.open(lease, resume)
        try:
            args = {"horizon": x.base.constraints.horizon.model_dump(mode="json")}
            x.connection.put(fc(("read_assignments", "protected_call", args)))
            request = await asyncio.wait_for(session.receive(), 2)
            assert isinstance(request, ToolRequest)
            if fault == "forged_result":
                with pytest.raises(AccessDenied):
                    await session.tool_result(request.call_id, ToolResult("success", {"speech": "forged proof"}))
                assert not x.connection.sent and not x.mcp.calls
                return
            if fault == "unretired":
                await session._send("dispatch", {"call_id": request.call_id})
            elif fault == "wrong_call":
                await session._send("dispatch", {"call_id": "some_other_call"})
            else:
                # Contract test acting as W04: actual durable retirement before
                # real bridge execution; never a second product checkpoint writer.
                await x.h.sessions.checkpoint(lease, resume.changed(seen_call_ids=(request.call_id,)))
                access = await x.h.sessions.check(lease)
                result = await provider.execute(access, request)
                assert result.status == "success" and set(result.value) == {"receipt"}
                with pytest.raises(AccessDenied):
                    await session.tool_result(request.call_id, ToolResult("success", {"speech": "forged proof"}))
                if fault == "duplicate_dispatch":
                    await session._send("dispatch", {"call_id": request.call_id})
                else:
                    await session._send("result_ack", {"call_id": request.call_id,
                        "receipt": "x" * 43, "status": "success"})
            with pytest.raises(ValueError, match="managed_voice"):
                await asyncio.wait_for(session.receive(), 2)
            assert len(x.mcp.calls) == (1 if fault in ("duplicate_dispatch", "wrong_receipt") else 0)
            assert not x.connection.sent  # Forged/duplicated results never reach ADK.
        finally:
            await session.close()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


@pytest.mark.parametrize("fault", ["stop", "revoke", "takeover", "expiry"])
def test_managed_stalled_tool_cancels_joins_and_never_replays(monkeypatch, fault):
    async def scenario():
        x = await components(monkeypatch)
        provider = x.provider()
        coordinator = VoiceCoordinator(x.h.sessions, provider, selected_mode=ProviderMode.MANAGED_PRODUCTION,
            production=True, tools=provider, clock=x.h.clock)
        downstream = FakeClient()
        x.mcp.gate = asyncio.Event()
        task = asyncio.create_task(coordinator.run(TOKEN, downstream))
        try:
            await asyncio.wait_for(downstream.ready.wait(), 2)
            args = {"horizon": x.base.constraints.horizon.model_dump(mode="json")}
            x.connection.put(fc(("read_assignments", "retired_managed", args)))
            await asyncio.wait_for(x.mcp.entered.wait(), 2)
            assert "retired_managed" in coordinator.resume.seen_call_ids
            if fault == "stop": downstream.put(control("stop", coordinator.lease.connection_generation))
            elif fault == "revoke":
                x.h.replace_credential(revoked=True)
                downstream.put(control("heartbeat", coordinator.lease.connection_generation))
            elif fault == "takeover":
                x.h.clock.advance(16)
                successor, _ = await x.h.sessions.acquire(TOKEN)
            else: x.h.clock.advance(541)
            result = await asyncio.wait_for(task, 3)
            if fault == "stop": assert not result.cleanup_failed
            assert x.mcp.cancelled and x.connection.closed and x.sdk.wires[0].closed
            assert not x.sessions[0].bridge.pending
            assert not x.connection.sent
            if fault == "stop":
                # A new managed stream gets a fresh reference, while the actual
                # W04 durable retired set rejects the same SDK function-call ID.
                x.mcp.gate = None
                next_coordinator = VoiceCoordinator(x.h.sessions, x.provider(),
                    selected_mode=ProviderMode.MANAGED_PRODUCTION, production=True, tools=None, clock=x.h.clock)
                next_provider = next_coordinator.provider
                next_coordinator.tools = next_provider
                second_client = FakeClient()
                second = asyncio.create_task(next_coordinator.run(TOKEN, second_client))
                try:
                    await asyncio.wait_for(second_client.ready.wait(), 2)
                    x.connection.put(fc(("read_assignments", "retired_managed", args)))
                    await asyncio.wait_for(second, 2)
                    assert len(x.mcp.calls) == 1
                finally:
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_cancel_stalled_open_and_credential_expiry_fail_closed(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        lease, resume = await x.h.sessions.acquire(TOKEN)
        x.sdk.open_gate = asyncio.Event()
        provider = x.provider()
        opening = asyncio.create_task(provider.open(lease, resume))
        await x.sdk.entered.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError): await opening
        assert x.sdk.closed == 2 and not x.sdk.wires
        provider = x.provider()
        async def stale():
            value = credentials()
            value.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=30)
            return value
        provider.credentials_provider = stale
        with pytest.raises(AccessDenied, match="fresh_platform_credentials"):
            await provider.open(lease, resume)
        assert x.sdk.closed == 2
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_cancel_stalled_send_receive_and_close_attempts_all_resources(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        lease, resume = await x.h.sessions.acquire(TOKEN)
        session = await x.provider().open(lease, resume)
        sender_entered = asyncio.Event()
        async def send(*args, **kwargs):
            sender_entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(session.connection, "send", send)
        sending = asyncio.create_task(session.send(p.AudioFrame("input", lease.connection_generation, 0, 0, b"\x00\x00")))
        receiving = asyncio.create_task(session.receive())
        await sender_entered.wait()
        sending.cancel(); receiving.cancel()
        await asyncio.gather(sending, receiving, return_exceptions=True)
        # A close timeout still attempts context exit and both HTTP pools.
        monkeypatch.setattr(client, "CLOSE_SECONDS", .01)
        async def close(): await asyncio.Event().wait()
        monkeypatch.setattr(session.connection, "close", close)
        with pytest.raises(ValueError, match="managed_voice_unavailable"):
            await session.close()
        assert x.sdk.closed == 2 and x.sdk.wires[0].closed
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_runtime_private_mcp_id_token_is_separate_from_backend_oauth(monkeypatch):
    from campuspulse_recovery.agent.mcp_auth import RefreshingIdTokenHeaders, IssuedIdToken
    from campuspulse_recovery.agent.voice_agent import LocalAdkSession
    async def scenario():
        h, policy, _ = await setup_policy()
        policy.lease_provider = lambda: policy._lease
        value = LocalAdkSession(policy, config(), model=FakeLiveModel(Connection()))
        toolset = value.agent.tools[0]
        headers = toolset._header_provider
        assert isinstance(headers, RefreshingIdTokenHeaders)
        assert headers.audience == config().mcp_audience
        issued = []
        async def issuer(audience, seconds):
            issued.append(audience)
            return IssuedIdToken("synthetic-mcp-id-token", audience, 1000)
        headers.bind_local_runtime(issuer=issuer, clock=lambda: 100)
        # Actual pinned header provider, not the backend OAuth bearer.
        result = await headers(SimpleNamespace())
        assert result == {"Authorization": "Bearer synthetic-mcp-id-token"}
        assert issued == [config().mcp_audience]
        assert result["Authorization"] != "Bearer " + credentials().token
        await value.close()
    run(scenario())


def test_runtime_renewal_during_actual_durable_check_uses_latest_lease(monkeypatch):
    from tests.voice.test_voice_policy import delayed_first_check
    async def scenario():
        x = await components(monkeypatch)
        lease, resume = await x.h.sessions.acquire(TOKEN)
        session = await x.provider().open(lease, resume)
        policy = x.sessions[0].policy
        entered, release, attempts = delayed_first_check(monkeypatch, policy)
        work = asyncio.create_task(policy.authorize("read_assignments"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            x.h.clock.advance(10)
            fresh = await x.h.sessions.renew(lease)
            release.set()
            access = await asyncio.wait_for(work, 2)
            assert attempts[:2] == [lease, fresh]
            assert policy.lease == fresh and access.binding == fresh.binding
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await session.close()
    run(scenario())


def test_concurrent_reference_redemption_has_one_durable_consumer(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        lease, _ = await x.h.sessions.acquire(TOKEN)
        reference = await x.issue(lease)
        before = x.h.documents.data[_path(lease.device_id)]
        values = await asyncio.gather(*(x.invocations.resolve(reference,
            x.h.config.identities.backend_service_account, consumer, redeem=True)
            for consumer in ("runtime_a", "runtime_b")), return_exceptions=True)
        assert sum(isinstance(value, tuple) for value in values) == 1
        assert sum(isinstance(value, AccessDenied) for value in values) == 1
        # Invocation operations never write a stream checkpoint/retired set.
        assert x.h.documents.data[_path(lease.device_id)] == before
    run(scenario())


@pytest.mark.parametrize("tool", ["apply_plan", "verify_plan", "unknown_tool"])
def test_network_event_rejects_mutation_and_unknown_tools(tool):
    with pytest.raises(ValueError, match="^managed_voice_event_rejected$"):
        runtime.decode_event({"type": "tool", "call_id": "synthetic_call", "tool": tool, "arguments": {}})


@pytest.mark.parametrize("audio", ["!", "AAA=", "eA==", "AAAA" * 1000])
def test_private_input_rejects_malformed_or_oversized_media(monkeypatch, audio):
    async def scenario():
        x = await components(monkeypatch)
        lease, _ = await x.h.sessions.acquire(TOKEN)
        with pytest.raises(ValueError):
            runtime.decode_input({"audio": audio}, lease)
    run(scenario())


def test_resume_creates_fresh_connection_without_token_history_or_audio_replay(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        lease, _ = await x.h.sessions.acquire(TOKEN)
        state = ResumeState(history=(HistoryNote(speaker="user", text="untrusted synthetic instruction"),),
            seen_call_ids=("old_call",), transport_token="synthetic-provider-resumption-token",
            transport_expires_at=x.h.clock() + timedelta(seconds=10))
        # Only actual durable retired IDs matter. Provider ignores resumption
        # token/notes, even when an injected state claims a valid transport token.
        await x.h.sessions.checkpoint(lease, state)
        session = await x.provider().open(lease, state)
        try:
            assert x.connection.history == [] and x.connection.realtime == []
            sent = x.sdk.wires[0].sent
            assert len(sent) == 1 and set(sent[0]["bidi_stream_input"]["body"]) == {"reference"}
            assert "resumption" not in json.dumps(sent) and "instruction" not in json.dumps(sent)
            args = {"horizon": x.base.constraints.horizon.model_dump(mode="json")}
            x.connection.put(fc(("read_assignments", "old_call", args)))
            with pytest.raises(ValueError, match="managed_voice"):
                await asyncio.wait_for(session.receive(), 2)
            assert x.mcp.calls == []
        finally:
            await session.close()
    run(scenario())


def test_existing_text_client_operation_preserved_with_explicit_protocol(monkeypatch):
    from campuspulse_recovery.web.agent_client import (AgentIntent, ManagedQuery, PlatformAgentClient,
                                                      RemoteFrame)
    async def scenario():
        x = await components(monkeypatch)
        lease, _ = await x.h.sessions.acquire(TOKEN)
        intent = AgentIntent(intent_id="text_intent", run_id=lease.binding.run_id, action="propose")
        called = []
        async def checkpoint(*args): pass
        async def prepare(context, supplied):
            assert supplied == intent
            return ManagedQuery(user_id=lease.binding.owner_id, session_id=lease.binding.session_id,
                                message="synthetic text query")
        async def async_stream_query(**kwargs):
            called.append(kwargs)
            yield {"synthetic": "terminal"}
        async def aclose(): pass
        fake_sdk = SimpleNamespace(agent_engines=SimpleNamespace(get=lambda **k:
            SimpleNamespace(async_stream_query=async_stream_query)), aio=SimpleNamespace(aclose=aclose),
            _api_client=SimpleNamespace(close=lambda: None))
        protocol = SimpleNamespace(prepare=prepare, decode=lambda *a: RemoteFrame(run_id=intent.run_id,
            intent_id=intent.intent_id, kind="terminal", status="no_change"))
        text = PlatformAgentClient(target(), protocol=protocol, checkpoint=checkpoint,
                                   client_factory=lambda **k: fake_sdk)
        with scope(x.h, lease.binding, "backend") as context:
            results = [item async for item in text.stream(context, intent)]
        assert len(results) == 1 and results[0].status == "no_change"
        assert called[0]["message"] == "synthetic text query"
        # There is no existing cloud recovery runtime source/registration to
        # preserve or fabricate: this new media class registers ONLY bidi.
        assert x.app.register_operations() == {"bidi_stream": ["bidi_stream_query"]}
    run(scenario())


async def stall_all_closers(session):
    entered, cancelled = set(), set()
    original_connection = session.connection.close
    original_manager = session.manager
    original_http = session.client.aio.aclose
    async def stall(name, action):
        entered.add(name)
        try:
            await action()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(name)
            raise
    async def connection_close(): await stall("connection", original_connection)
    async def manager_exit(*args):
        await stall("manager", lambda: original_manager.__aexit__(*args))
    async def http_close(): await stall("http", original_http)
    session.connection.close = connection_close
    session.manager = SimpleNamespace(__aexit__=manager_exit)
    session.client.aio.aclose = http_close
    return entered, cancelled


def test_close_preserves_outer_deadline_with_all_sdk_closers_stalled(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        lease, resume = await x.h.sessions.acquire(TOKEN)
        session = await x.provider().open(lease, resume)
        entered, cancelled = await stall_all_closers(session)
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.01):
                await session.close()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < .25, elapsed
        assert entered == cancelled == {"connection", "manager", "http"}
        assert x.sdk.wires[0].closed and x.sdk.closed == 2
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        print("W06 outer close deadline elapsed_seconds=", round(elapsed, 4))
    run(scenario())


def test_actual_w04_cleanup_budget_with_all_sdk_closers_stalled(monkeypatch):
    async def scenario():
        x = await components(monkeypatch)
        provider = x.provider()
        limits = Limits(close_seconds=.16)
        coordinator = VoiceCoordinator(x.h.sessions, provider, selected_mode=ProviderMode.MANAGED_PRODUCTION,
            production=True, tools=provider, clock=x.h.clock, limits=limits)
        downstream = FakeClient()
        task = asyncio.create_task(coordinator.run(TOKEN, downstream))
        try:
            await asyncio.wait_for(downstream.ready.wait(), 2)
            entered, cancelled = await stall_all_closers(provider.session)
            started = asyncio.get_running_loop().time()
            downstream.put(control("stop", coordinator.lease.connection_generation))
            result = await asyncio.wait_for(task, 1)
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < .4, elapsed  # Windows scheduling allowance, not a cloud latency claim.
            assert result.cleanup_failed is True
            assert entered == cancelled == {"connection", "manager", "http"}
            assert downstream.closed and x.sdk.wires[0].closed and x.connection.closed
            assert not x.sessions[0].bridge.pending
            print("W06 actual W04 cleanup elapsed_seconds=", round(elapsed, 4), "cleanup_failed=", result.cleanup_failed)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())
