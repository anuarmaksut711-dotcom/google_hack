"""W12 joined ASGI + managed SDK + real ADK callbacks; no network."""
import asyncio
import importlib
import importlib.metadata
import json
from pathlib import Path

import pytest
from google.adk.models.llm_response import LlmResponse

from campuspulse_recovery.observability.voice_events import VoiceEvent
from campuspulse_recovery.observability.events import reference
from campuspulse_recovery.storage.plans import _decode
from campuspulse_recovery.voice import protocol as p
from campuspulse_recovery.agent.voice_policy import TOOLS
from tests.storage.test_plans import run
from tests.voice.test_voice_policy import deny_network
from tests.voice.test_live_agent import audio
from evals.voice.joined import joined, call, turn, finish, until, pcm


def test_current_origins_and_exact_installed_versions():
    root = Path(__file__).resolve().parents[2] / "src/campuspulse_recovery"
    for suffix in ("web.voice", "voice.coordinator", "cloud.voice_client", "cloud.voice_runtime",
                   "agent.voice_agent", "agent.voice_policy", "domain.solver"):
        module = importlib.import_module("campuspulse_recovery." + suffix)
        assert Path(module.__file__).resolve() == root.joinpath(*suffix.split(".")).with_suffix(".py")
    assert {n: importlib.metadata.version(n) for n in (
        "google-adk", "google-genai", "google-cloud-aiplatform", "mcp")} == {
        "google-adk": "2.6.3", "google-genai": "2.31.0",
        "google-cloud-aiplatform": "1.165.1", "mcp": "1.30.0"}


def test_joined_duplex_callbacks_three_tools_receipts_and_safe_evidence(monkeypatch, caplog):
    async def scenario():
        x = await joined(monkeypatch, mutation="injection")
        try:
            assert TOOLS == ("read_assignments", "read_calendar", "solve_repair")
            # Input reaches real Runner while model output/read tools are active.
            x.socket.put(p.encode_audio(p.AudioFrame("input", x.lease.connection_generation, 0, 0, b"\0\0" * 320)))
            await until(lambda: len(x.connection.realtime) >= 2)
            for name, cid in (("read_assignments", "joined_a"), ("read_calendar", "joined_c")):
                result = await call(x, name, cid)
                assert result["status"] == "read" and result["saved"] is False
                assert "W12_PRIVATE_SOURCE_SECRET" not in json.dumps(result)
            result = await call(x, "solve_repair", "joined_s", {"request": {}})
            assert result["saved"] is False and result["status"] in ("feasible", "no_change")
            assert [n for n, _ in x.attempts] == ["read_assignments", "read_calendar"]
            await turn(x)
            await until(lambda: bool(pcm(x)))
            # Full managed SDK framing, opaque receipts, never raw source results.
            wire = x.sdk.wires[0].sent
            acks = [v["bidi_stream_input"] for v in wire if v["bidi_stream_input"]["kind"] == "result_ack"]
            assert len(acks) == 3
            assert all(set(a["body"]) == {"call_id", "status", "receipt"} for a in acks)
        finally:
            await finish(x)
        events = [_decode(v, "voice_event", VoiceEvent) for k, v in x.h.documents.data.items()
                  if k.startswith("cp_voice_events/")]
        assert {e.call_id for e in events if e.kind == "tool_finished"} == {reference(v) for v in ("joined_a", "joined_c", "joined_s")}
        assert all(e.run_id == x.lease.run_id and e.evidence_class == "windows_local" for e in events)
        assert all(e.duration_ms is not None for e in events if e.kind == "tool_finished")
        assert events[-1].status == "stopped" and not events[-1].cleanup_failed
        safe = json.dumps([e.model_dump(mode="json") for e in events]) + caplog.text
        for secret in ("W12_PRIVATE_SOURCE_SECRET", "synthetic-platform-oauth", "s" * 43):
            assert secret not in safe
        assert not any(k.startswith(("cp_plans/", "cp_approvals/", "cp_operations/")) for k in x.h.documents.data)
    run(scenario())


def test_joined_interrupted_buffer_and_next_generation(monkeypatch):
    async def scenario():
        x = await joined(monkeypatch)
        try:
            x.connection.put(audio(b"\x66\x00" * 480))
            x.connection.put(LlmResponse(interrupted=True))
            await turn(x, b"\x22\x00" * 480)
            await until(lambda: bool(pcm(x)))
            assert b"".join(f.pcm for f in pcm(x)) == b"\x22\x00" * 480
            await turn(x, b"\x33\x00" * 480)
            await until(lambda: len(pcm(x)) >= 2)
            assert [f.generation for f in pcm(x)] == [1, 2]
        finally:
            await finish(x)
    run(scenario())


def test_joined_slow_consumer_bounded_close(monkeypatch):
    async def scenario():
        x = await joined(monkeypatch, stall_audio=True)
        await turn(x)
        await asyncio.wait_for(x.socket.audio_waiting.wait(), 2)
        await until(lambda: x.socket.task.done())
        await finish(x)
        assert x.socket.audio_cancelled
        assert x.socket.controls()[-1]["reason"] == "backpressure"
    run(scenario())
