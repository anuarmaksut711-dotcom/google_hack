"""Real pinned Runner.run_live + SDK function dispatcher, synthetic connection.

Only Gemini's network connection and MCP discovery/transport are faked. No
replacement Runner, live flow, SDK tool dispatcher, callbacks or output gate.
"""
import asyncio
from contextlib import asynccontextmanager
import importlib.metadata
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.base_llm_connection import BaseLlmConnection
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from campuspulse_recovery.agent import voice_agent as a, voice_policy as v
from campuspulse_recovery.agent.smoke_agent import SmokeConfig, SerializableReadMcpToolset
from campuspulse_recovery.voice import protocol as p
from campuspulse_recovery.voice.coordinator import VoiceCoordinator
from campuspulse_recovery.voice.ports import OutputAudio, OutputTranscript, ProviderMode
from campuspulse_recovery.voice.sessions import ResumeState
from tests.voice.test_coordinator import FakeClient, control, settle
from tests.voice.test_sessions import TOKEN
from tests.voice.test_voice_policy import (setup_policy, snapshot, McpSession, remote_tools, deny_network,
                                          delayed_first_check)
from tests.storage.test_plans import run


class Connection(BaseLlmConnection):
    def __init__(self):
        self.responses = asyncio.Queue()
        self.realtime, self.sent, self.history = [], [], []
        self.closed = self.cancelled = False
        self.request = None
    async def send_history(self, history): self.history.extend(history)
    async def send_content(self, content): self.sent.append(content)
    async def send_realtime(self, blob): self.realtime.append(blob)
    async def receive(self):
        try:
            while True:
                yield await self.responses.get()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
    async def close(self): self.closed = True
    def put(self, value): self.responses.put_nowait(value)


class FakeLiveModel(BaseLlm):
    _connection: object = PrivateAttr()
    _entered: object = PrivateAttr()
    def __init__(self, connection, entered=None):
        super().__init__(model=a.LIVE_MODEL)
        self._connection, self._entered = connection, entered
    @asynccontextmanager
    async def connect(self, request):
        self._connection.request = request
        if self._entered: await self._entered.wait()
        try:
            yield self._connection
        finally:
            await self._connection.close()
    async def generate_content_async(self, llm_request, stream=False):
        raise AssertionError("text model path must never run")
        yield


def config():
    return SmokeConfig("campus-test-42", "us-central1", a.LIVE_MODEL,
                       "https://synthetic-mcp-123.us-central1.run.app")


def patch_discovery(monkeypatch, session):
    calls = []
    async def get_tools(self, readonly_context=None):
        calls.append(True)
        return remote_tools(session.policy, session)
    async def close(self): pass
    monkeypatch.setattr(SerializableReadMcpToolset, "get_tools", get_tools)
    monkeypatch.setattr(SerializableReadMcpToolset, "close", close)
    return calls


def fc(*calls):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
        name=name, id=call_id, args=args)) for name, call_id, args in calls]))


def audio(data=b"\x01\x00" * 20, thought=False):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(thought=thought,
        inline_data=types.Blob(data=data, mime_type="audio/pcm;rate=24000"))]), partial=True)


def transcription(text, final=True):
    return LlmResponse(output_transcription=types.Transcription(text=text, finished=final), partial=not final)


def test_pinned_sdk_current_modules_lazy_construction_and_config():
    async def scenario():
        _, policy, _ = await setup_policy()
        bridge = v.LiveToolBridge(policy, asyncio.Queue().put)
        agent = a.build_voice_agent(config(), bridge=bridge, ready=asyncio.Event())
        assert agent.model.model == a.LIVE_MODEL
        assert agent.model._delegate.client_kwargs["vertexai"] is True
        assert len(agent.tools) == 2 and agent.before_model_callback is None and agent.after_model_callback is None
        cfg = a.live_config()
        assert cfg.response_modalities == [types.Modality.AUDIO]
        assert cfg.realtime_input_config.automatic_activity_detection.disabled
        assert not cfg.save_live_blob and not cfg.save_live_audio and not cfg.save_input_blobs_as_artifacts
        assert not cfg.telemetry.should_add_content_to_legacy_spans
        assert cfg.input_audio_transcription is not None and cfg.output_audio_transcription is not None
        with pytest.raises(ValueError):
            a.build_voice_agent(SmokeConfig("campus-test-42", "global", "gemini-2.5-flash", config().mcp_audience),
                                bridge=bridge, ready=asyncio.Event())
    print("W05_AGENT_SOURCE=", a.__file__)
    print("W05_ADK=", importlib.metadata.version("google-adk"))
    assert Path(a.__file__).resolve() == Path(__file__).resolve().parents[2] / "src/campuspulse_recovery/agent/voice_agent.py"
    assert importlib.metadata.version("google-adk") == "2.6.3"
    run(scenario())


def test_real_live_dispatch_through_actual_coordinator_and_concurrent_reads(monkeypatch):
    async def scenario():
        h, base, _ = await setup_policy()
        await h.sessions.release(base.lease)
        mcp = McpSession(base); discovered = patch_discovery(monkeypatch, mcp)
        connection = Connection()
        def factory(lease):
            return v.VoicePolicy(sessions=h.sessions, lease=lease, request_scope=base.request_scope,
                constraints=base.constraints, now=base.now, lease_provider=lambda: coordinator.lease)
        provider = a.LocalAdkProvider(config(), policy_factory=factory, model=FakeLiveModel(connection))
        coordinator = VoiceCoordinator(h.sessions, provider, selected_mode=ProviderMode.LOCAL_ADK_DEVELOPMENT,
            production=False, tools=provider, clock=h.clock)
        client = FakeClient()
        task = asyncio.create_task(coordinator.run(TOKEN, client))
        try:
            await client.ready.wait()
            assert discovered == [True]
            assert connection.request.live_connect_config.realtime_input_config.automatic_activity_detection.disabled
            declarations = {d.name for t in connection.request.config.tools for d in t.function_declarations or []}
            assert declarations == set(v.TOOLS)
            assert connection.realtime == []  # Ready is not activity begin.
            generation = coordinator.lease.connection_generation
            client.put(p.encode_audio(p.AudioFrame("input", generation, 0, 0, b"\x00\x00" * 320)))
            client.put(control("input", generation, action="end"))
            await settle(lambda: len(connection.realtime) == 3)
            assert isinstance(connection.realtime[0], types.ActivityStart)
            assert connection.realtime[1].mime_type == "audio/pcm;rate=16000"
            assert isinstance(connection.realtime[2], types.ActivityEnd)
            args = {"horizon": base.constraints.horizon.model_dump(mode="json")}
            connection.put(fc(("read_assignments", "live_a", args), ("read_calendar", "live_c", args)))
            await settle(lambda: len(connection.sent) == 1)
            response = connection.sent[0]
            assert {x.function_response.name for x in response.parts} == set(v.READS)
            assert len(mcp.calls) == 2
            assert set(coordinator.resume.seen_call_ids) == {"live_a", "live_c"}
            assert len(provider.session.policy.observations) == 2
            assert all(x["operation_ids"] == [] for x in provider.session.policy.observations)
            assert not provider.session.service.sessions[a.APP_NAME][base.lease.binding.owner_id][coordinator.lease.session_id].events
            connection.put(fc(("solve_repair", "live_s", {"request": {}})))
            await settle(lambda: len(connection.sent) == 2)
            assert len(mcp.calls) == 2  # Pure deterministic solver, no new MCP.
            policy = provider.session.policy
            assert policy.candidate is not None
            # Hold the actual durable check with the old DTO while the actual
            # W04 watchdog renews it. SDK/tool flow must continue after retry.
            entered, release, attempts = delayed_first_check(monkeypatch, policy)
            captured = coordinator.lease
            connection.put(fc(("read_assignments", "after_renew", args)))
            await entered.wait()
            h.clock.advance(10)
            await settle(lambda: coordinator.lease.lease_until > provider.session.policy._lease.lease_until)
            assert provider.session.policy.lease == coordinator.lease
            release.set()
            await settle(lambda: len(connection.sent) == 3)
            assert attempts[:2] == [captured, coordinator.lease]
            assert len(mcp.calls) == 3
            assert connection.sent[-1].parts[0].function_response.response["status"] == "read"
            connection.put(audio())
            await settle()
            assert not any(isinstance(x, p.AudioFrame) for x in client.sent)
            connection.put(transcription(policy.speech()))
            connection.put(LlmResponse(turn_complete=True))
            # A completed safe turn is released with pacing; no premature audio.
            for _ in range(1500):
                await asyncio.sleep(0)
                if any(isinstance(x, p.AudioFrame) for x in client.sent): break
            assert any(isinstance(x, p.AudioFrame) for x in client.sent)
            assert any(isinstance(x, p.ToolFinished) and x.status == "success" for x in client.sent)
        finally:
            client.put(control("stop", coordinator.lease.connection_generation if coordinator.lease else 2))
            outcome = await task
        assert outcome.reason == "stopped" and not outcome.cleanup_failed
        assert connection.closed and connection.cancelled
        assert provider.session.bridge.pending == {}
    run(scenario())


@pytest.mark.parametrize("calls", [
    [("apply_plan", "write_a", {"approved": True})],
    [("verify_plan", "verify_a", {})], [("arbitrary", "unknown_a", {})],
    [("read_calendar", "spoof_a", {"calendar_id": "other"})],
    [("read_assignments", "bad_a", {"horizon": []})],
])
def test_actual_live_unknown_write_malformed_dispatch_never_reaches_mcp(monkeypatch, calls):
    async def scenario():
        _, policy, _ = await setup_policy()
        mcp = McpSession(policy); patch_discovery(monkeypatch, mcp)
        connection = Connection()
        session = await a.LocalAdkSession(policy, config(), model=FakeLiveModel(connection)).start()
        try:
            connection.put(fc(*calls))
            await settle(lambda: bool(connection.sent))
            assert mcp.calls == [] and session.bridge.pending == {}
            assert all(p.function_response.response["status"] == "blocked" for p in connection.sent[0].parts)
        finally: await session.close()
    run(scenario())


def test_ready_waits_for_actual_connect_and_cancel_joins_sdk_tasks(monkeypatch):
    async def scenario():
        _, policy, _ = await setup_policy()
        mcp = McpSession(policy); patch_discovery(monkeypatch, mcp)
        connection = Connection(); gate = asyncio.Event()
        session = a.LocalAdkSession(policy, config(), model=FakeLiveModel(connection, gate))
        opening = asyncio.create_task(session.start())
        await settle(lambda: connection.request is not None)
        assert not session.ready.is_set()
        gate.set(); await opening
        assert session.ready.is_set()
        await session.close()
        assert session.task.done() and connection.closed
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


@pytest.mark.parametrize("bad", ["invented_success", "partial", "thought", "interrupted", "wrong_mime"])
def test_output_before_audio_no_later_transcript_can_retract_it(bad):
    async def scenario():
        _, policy, _ = await setup_policy()
        gate = a.LiveOutputGate(policy)
        event = audio(thought=bad == "thought")
        if bad == "wrong_mime": event.content.parts[0].inline_data.mime_type = "audio/pcm;rate=16000"
        assert gate.accept(event) == []
        if bad == "interrupted":
            assert gate.accept(LlmResponse(interrupted=True)) == []
        text = "План успешно применён и проверен в Calendar." if bad == "invented_success" else policy.speech()
        assert gate.accept(transcription(text, final=bad != "partial")) == []
        output = gate.accept(LlmResponse(turn_complete=True))
        assert not any(isinstance(x, OutputAudio) for x in output)
        assert all("успешно" not in x.text for x in output if isinstance(x, OutputTranscript))
    run(scenario())


def test_sdk_log_payload_drop_and_telemetry_override(monkeypatch, caplog):
    a.protect_sdk_logs()
    logger = logging.getLogger("google_adk.google.adk.flows.llm_flows.base_llm_flow")
    with caplog.at_level(logging.DEBUG):
        with v.voice_logs():
            logger.error("synthetic-private-PCM-token-event")
            logging.getLogger("google_adk.future.child.logger").error("synthetic-private-later")
        logger.error("unrelated-text-agent-retained")
    assert "synthetic-private" not in caplog.text
    assert "unrelated-text-agent-retained" in caplog.text
    monkeypatch.setenv("ADK_TELEMETRY_IGNORE_RUN_CONFIG", "true")
    monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
    with pytest.raises(ValueError): a.live_config()


def test_native_sdk_transcription_conversion_and_audio_gate():
    from google.adk.models.gemini_llm_connection import GeminiLlmConnection
    async def scenario():
        _, policy, _ = await setup_policy()
        class NativeSession:
            session_id = "synthetic_native"
            async def receive(self):
                yield types.LiveServerMessage(server_content=types.LiveServerContent(
                    model_turn=types.Content(role="model", parts=[types.Part(inline_data=types.Blob(
                        data=b"\x00\x00" * 20, mime_type="audio/pcm;rate=24000"))])))
                text = policy.speech()
                yield types.LiveServerMessage(server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text=text[:10])))
                yield types.LiveServerMessage(server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text=text[10:])))
                yield types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))
        native = GeminiLlmConnection(NativeSession(), model_version=a.LIVE_MODEL)
        gate = a.LiveOutputGate(policy); output = []; observed = []
        async for response in native.receive():
            observed.append(response)
            output.extend(gate.accept(response))
        assert any(x.partial and x.output_transcription for x in observed)
        assert any(not x.partial and x.output_transcription.finished for x in observed if x.output_transcription)
        assert any(isinstance(x, OutputAudio) for x in output)
        assert [x.text for x in output if isinstance(x, OutputTranscript)] == [policy.speech()]
    run(scenario())


def test_initial_pcm_mute_unmute_and_transcript_revisions_through_w02(monkeypatch):
    async def scenario():
        _, policy, _ = await setup_policy()
        mcp = McpSession(policy); patch_discovery(monkeypatch, mcp)
        connection = Connection()
        session = await a.LocalAdkSession(policy, config(), model=FakeLiveModel(connection)).start()
        try:
            g = policy.lease.connection_generation
            await session.send(p.AudioFrame("input", g, 0, 0, b"\x00\x00" * 20))
            await session.send(p.Mute(version="1", type="mute", request_id="m1", connection_generation=g, muted=True))
            with pytest.raises(ValueError): await session.send(p.AudioFrame("input", g, 0, 1, b"\x00\x00" * 20))
            await session.send(p.Mute(version="1", type="mute", request_id="m2", connection_generation=g, muted=False))
            await session.send(p.AudioFrame("input", g, 0, 1, b"\x00\x00" * 20))
            await settle(lambda: len(connection.realtime) == 5)
            assert [type(x) for x in connection.realtime] == [types.ActivityStart, types.Blob,
                types.ActivityEnd, types.ActivityStart, types.Blob]
            state = p.ProtocolState(g)
            state.accept_client(p.parse_json(control("start"), client=True), now=0)
            state.accept_server(p.Ready(version="1", type="ready", event_id="e0", event_sequence=0,
                connection_generation=g, elapsed_ms=0, session_id="s", run_id="r", expires_in_ms=10000,
                input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT))
            for text, final in [("Осталось ", False), ("тридцать минут", False), ("Осталось тридцать минут", True)]:
                connection.put(LlmResponse(input_transcription=types.Transcription(text=text, finished=final), partial=not final))
                transcript = await session.receive()
                assert isinstance(transcript, OutputTranscript)
                state.accept_server(p.Transcript(version="1", type="transcript", event_id="e"+str(state.event_sequence),
                    event_sequence=state.event_sequence, connection_generation=g, elapsed_ms=0, **vars(transcript)))
            assert transcript.final and transcript.text == "Осталось тридцать минут"
            assert state.transcripts[transcript.transcript_id][2] == 2
        finally: await session.close()
    run(scenario())


@pytest.mark.parametrize("stop_during_tool", [True, False])
def test_actual_sdk_tool_cancellation_and_retired_call_on_resume(monkeypatch, stop_during_tool):
    async def scenario():
        h, base, _ = await setup_policy(); await h.sessions.release(base.lease)
        mcp = McpSession(base)
        if stop_during_tool: mcp.gate = asyncio.Event()
        patch_discovery(monkeypatch, mcp)
        cleanup_flags = []
        async def one(replay=False):
            connection = Connection()
            def factory(lease):
                return v.VoicePolicy(sessions=h.sessions, lease=lease, request_scope=base.request_scope,
                    constraints=base.constraints, now=base.now, lease_provider=lambda: coordinator.lease)
            provider = a.LocalAdkProvider(config(), policy_factory=factory, model=FakeLiveModel(connection))
            coordinator = VoiceCoordinator(h.sessions, provider, selected_mode=ProviderMode.LOCAL_ADK_DEVELOPMENT,
                production=False, tools=provider, clock=h.clock)
            client = FakeClient(); task = asyncio.create_task(coordinator.run(TOKEN, client))
            send_errors = []
            original_send = coordinator._send
            async def observed_send(packet):
                try: return await original_send(packet)
                except p.ProtocolError as error:
                    send_errors.append((packet.type, error.code, str(coordinator.state.phase)))
                    raise
            coordinator._send = observed_send
            await client.ready.wait()
            connection.put(fc(("read_assignments", "retired_read", {"horizon": base.constraints.horizon.model_dump(mode="json")})))
            if replay:
                outcome = await task
                assert outcome.reason == "protocol_error"
            else:
                await mcp.entered.wait()
                if not stop_during_tool: await settle(lambda: bool(connection.sent))
                client.put(control("stop", coordinator.lease.connection_generation))
                outcome = await task
                assert outcome.reason == "stopped"
                cleanup_flags.append(outcome.cleanup_failed)
                assert send_errors == []
                assert not outcome.cleanup_failed
            assert connection.closed and provider.session.bridge.pending == {}
            return provider
        await one()
        assert len(mcp.calls) == 1
        if stop_during_tool: assert mcp.cancelled
        mcp.gate = None
        await one(replay=True)
        assert len(mcp.calls) == 1
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not any(cleanup_flags)
    run(scenario())


def test_interrupt_during_audio_release_flushes_old_generation(monkeypatch):
    async def scenario():
        _, policy, _ = await setup_policy()
        mcp = McpSession(policy); patch_discovery(monkeypatch, mcp)
        connection = Connection()
        session = await a.LocalAdkSession(policy, config(), model=FakeLiveModel(connection)).start()
        try:
            connection.put(audio(b"\x00\x00" * 4800))
            connection.put(transcription(policy.speech())); connection.put(LlmResponse(turn_complete=True))
            started = await session.receive(); text = await session.receive()
            assert started.phase == "started" and text.revision == 0
            receiving = asyncio.create_task(session.receive())
            await asyncio.sleep(0)
            connection.put(LlmResponse(interrupted=True))
            item = await receiving
            assert item.phase == "interrupted" and item.generation == 1
            connection.put(audio()); connection.put(transcription(policy.speech())); connection.put(LlmResponse(turn_complete=True))
            started = await session.receive()
            assert started.generation == 2 and started.phase == "started"
            assert not session.events.qsize()
        finally: await session.close()
    run(scenario())


def test_expiry_between_sdk_turns_and_session_turn_audio_budgets(monkeypatch):
    async def scenario():
        h, policy, _ = await setup_policy()
        mcp = McpSession(policy); patch_discovery(monkeypatch, mcp)
        connection = Connection()
        session = await a.LocalAdkSession(policy, config(), model=FakeLiveModel(connection)).start()
        h.clock.advance(16)
        connection.put(audio())
        with pytest.raises(PermissionError): await session.receive()
        await session.close()
        assert not any(isinstance(x, OutputAudio) for x in session.output)
        h2, policy2, _ = await setup_policy()
        gate = a.LiveOutputGate(policy2)
        with pytest.raises(ValueError, match="output_queue_limit"):
            gate.accept(audio(b"\x00\x00" * 240001))
        gate = a.LiveOutputGate(policy2)
        gate.started = a.time.monotonic() - 61
        with pytest.raises(ValueError, match="turn_timeout"): gate.accept(audio())
        policy2.turns = v.MAX_TURNS
        with pytest.raises(ValueError, match="turn_budget"): a.LiveOutputGate(policy2).accept(LlmResponse(turn_complete=True))
        policy2.started = a.time.monotonic() - 541
        with pytest.raises(PermissionError): policy2.active()
    run(scenario())


def test_discovery_requires_fresh_independent_authority_and_exact_read_names(monkeypatch):
    async def scenario():
        h, policy, _ = await setup_policy()
        mcp = McpSession(policy); discovered = patch_discovery(monkeypatch, mcp)
        h.clock.advance(16)
        session = a.LocalAdkSession(policy, config(), model=FakeLiveModel(Connection()))
        with pytest.raises(PermissionError): await session.start()
        assert discovered == []
        await session.close()
        _, policy2, _ = await setup_policy()
        bridge = v.LiveToolBridge(policy2, asyncio.Queue().put)
        with pytest.raises(ValueError, match="audience"):
            a.build_voice_agent(config(), bridge=bridge, ready=asyncio.Event(),
                headers=SimpleNamespace(audience="https://other.run.app"))
        original = SerializableReadMcpToolset.get_tools
        async def wrong_tools(self, readonly_context=None):
            tools = await original(self, readonly_context)
            tools[1].name = "apply_plan"
            return tools
        monkeypatch.setattr(SerializableReadMcpToolset, "get_tools", wrong_tools)
        toolset = a.VoiceMcpToolset(config(), SimpleNamespace(audience=config().mcp_audience), bridge)
        with pytest.raises(ValueError, match="exact_two"):
            await toolset.get_tools()
        assert not policy2.remote
    run(scenario())
