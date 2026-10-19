"""Offline contract tests; literal bytes are an oracle independent of the codec."""

import hashlib
import json
from pathlib import Path
import runpy
import socket
import warnings

import pytest

from campuspulse_recovery.voice import protocol as p

FIXTURES = Path(__file__).parent / "fixtures" / "protocol"
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
EXCHANGE = json.loads((FIXTURES / "controls.json").read_text(encoding="utf-8"))
CONNECTION = EXCHANGE["connection_generation"]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("protocol tests must not use network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def client(kind="heartbeat", **fields):
    data = {"version": "1", "type": kind, "request_id": "request_1"}
    if kind == "start":
        data.update(input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT)
    else:
        data["connection_generation"] = CONNECTION
    data.update(fields)
    return p.parse_json(json.dumps(data), client=True)


def server(state, kind, **fields):
    data = dict(version="1", type=kind, event_id=f"evt_{state.event_sequence}",
                event_sequence=state.event_sequence, elapsed_ms=state.elapsed_ms,
                connection_generation=CONNECTION)
    data.update(fields)
    return p.parse_json(json.dumps(data), client=False)


def active():
    state = p.ProtocolState(CONNECTION)
    state.accept_client(client("start"), now=0)
    state.accept_server(server(state, "ready", session_id="session_1", run_id="run_1",
                               expires_in_ms=1000, input_format=p.INPUT_FORMAT,
                               output_format=p.OUTPUT_FORMAT))
    return state


def generation(state):
    state.accept_server(server(state, "generation", generation=state.generation + 1, phase="started"))


def audio(kind="input", *, sequence=0, generation=0, connection=CONNECTION, pcm=b"\x00\x00"):
    return p.AudioFrame(kind, connection, generation, sequence, pcm)


def error(code, action):
    with pytest.raises(p.ProtocolError) as caught:
        action()
    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__context__ is None


def test_current_source_import():
    expected = Path(__file__).resolve().parents[2] / "src/campuspulse_recovery/voice/protocol.py"
    assert Path(p.__file__).resolve() == expected
    print("CURRENT_SOURCE_MODULE=" + str(p.__file__).encode("unicode_escape").decode("ascii"))


@pytest.mark.parametrize("vector", GOLDEN["vectors"], ids=lambda v: v["file"])
def test_fixed_bytes_independent_decode_and_encode(vector):
    raw = (FIXTURES / vector["file"]).read_bytes()
    assert raw.hex() == vector["hex"]
    assert hashlib.sha256(raw).hexdigest() == vector["sha256"]
    assert raw[:8] == bytes.fromhex("4350564101" + ("01" if vector["kind"] == "input" else "02") + "0018")

    fields = [int.from_bytes(raw[o:o+4], "big") for o in (8, 12, 16, 20)]
    assert fields == [vector["connection_generation"], vector["generation"], vector["sequence"], 16]
    assert [int.from_bytes(raw[o:o+2], "little", signed=True) for o in range(24, 40, 2)] == GOLDEN["samples"]
    frame = p.decode_audio(raw)
    assert (frame.kind, frame.connection_generation, frame.generation, frame.sequence, frame.pcm.hex()) == (
        vector["kind"], *fields[:3], GOLDEN["pcm_hex"])

    fresh = p.AudioFrame(vector["kind"], vector["connection_generation"], vector["generation"],
                         vector["sequence"], bytes.fromhex(GOLDEN["pcm_hex"]))
    assert p.encode_audio(fresh) == raw
    independent = bytes.fromhex("4350564101") + bytes([1 if vector["kind"] == "input" else 2])
    independent += (24).to_bytes(2, "big")
    independent += b"".join(x.to_bytes(4, "big") for x in fields)
    independent += b"".join(x.to_bytes(2, "little", signed=True) for x in GOLDEN["samples"])
    assert independent == raw


def test_fixture_generator_is_reproducible():
    before = {n: (FIXTURES / n).read_bytes() for n in ("input-v1.bin", "output-v1.bin", "golden.json")}

    namespace = runpy.run_path(str(FIXTURES / "generate.py"))
    for vector in namespace["VECTORS"]:
        assert bytes.fromhex(vector["hex"]) == before[vector["file"]]


def test_binary_direction_is_enforced_at_the_peer_boundary():
    for name,kind,opposite in (("input-v1.bin","input","output"),("output-v1.bin","output","input")):
        raw=(FIXTURES/name).read_bytes()
        assert p.decode_audio(raw,expected_kind=kind).kind == kind
        error("invalid_frame",lambda: p.decode_audio(raw,expected_kind=opposite))


def test_regenerate_fixtures_in_temporary_directory(tmp_path):
    namespace = runpy.run_path(str(FIXTURES / "generate.py"))
    namespace["generate"].__globals__["ROOT"] = tmp_path
    namespace["generate"]()
    for name in ("input-v1.bin", "output-v1.bin", "golden.json"):
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes()


@pytest.mark.parametrize("offset,hex_bytes,code", [
    (0,"00505641","invalid_frame"), (4,"02","unsupported_version"),
    (5,"00","invalid_frame"), (5,"03","invalid_frame"),
    (6,"1800","invalid_frame"), (6,"0019","invalid_frame"),
    (8,"00000000","invalid_frame"), (12,"00000001","invalid_frame"),
    (20,"10000000","invalid_length"), (20,"0000000f","invalid_length"),
])
def test_malformed_header(offset, hex_bytes, code):
    raw = bytearray((FIXTURES / "input-v1.bin").read_bytes())
    change = bytes.fromhex(hex_bytes)
    raw[offset:offset+len(change)] = change
    error(code, lambda: p.decode_audio(bytes(raw)))


@pytest.mark.parametrize("length", [0, 1, 7, 23, 24, 25, 39, 41])
def test_truncated_or_trailing_binary(length):
    raw = (FIXTURES / "input-v1.bin").read_bytes()
    damaged = (raw + b"\x00")[:length]
    error("invalid_length", lambda: p.decode_audio(damaged))


@pytest.mark.parametrize("kind,maximum", [("input",1280), ("output",1920)])
def test_pcm_bounds_alignment_and_implicit_rate(kind, maximum):
    gen = 0 if kind == "input" else 1
    for size in (2, 16, maximum):
        frame = audio(kind, generation=gen, pcm=b"\0"*size)
        assert len(p.encode_audio(frame)) == 24 + size
    for size in (0, 1, 3, maximum + 1, maximum + 2):
        error("invalid_length", lambda: p.encode_audio(audio(kind, generation=gen, pcm=b"\0"*size)))
        raw = bytes.fromhex("4350564101") + bytes([1 if kind == "input" else 2]) + (24).to_bytes(2,"big")
        raw += CONNECTION.to_bytes(4,"big") + gen.to_bytes(4,"big") + (0).to_bytes(4,"big") + size.to_bytes(4,"big") + b"\0"*size
        error("invalid_length", lambda: p.decode_audio(raw))


@pytest.mark.parametrize("fields", [
    {"sequence": True}, {"sequence": -1}, {"sequence": 2**32},
    {"connection_generation":0}, {"connection_generation":1.0},
    {"generation":1}, {"pcm":bytearray(2)}, {"kind":"wav"},
])
def test_encode_rejects_invalid_values(fields):
    values = dict(kind="input", connection_generation=CONNECTION, generation=0, sequence=0, pcm=b"\0\0")
    values.update(fields)
    with pytest.raises(p.ProtocolError):
        p.encode_audio(p.AudioFrame(**values))


@pytest.mark.parametrize("body,code", [
    ('{}','unsupported_version'), ('{"version":2,"type":"stop"}','unsupported_version'),
    ('{"version":"2","type":"stop"}','unsupported_version'),
    ('{"version":"1","type":"invoke"}','unknown_event'),
    ('{"version":"1","type":[]}','unknown_event'),
    ('[]','invalid_message'), ('null','invalid_message'),
    ('{"version":"1","version":"2"}','invalid_json'),
    ('{"x":NaN}','invalid_json'), ('{"x":Infinity}','invalid_json'),
    ('{"x":"\\ud800"}','invalid_json'), ('{"x":','invalid_json'),
    (b'\xff','invalid_json'), ('"' + 'x'*2048 + '"','message_too_large'),
])
def test_bad_json(body, code):
    error(code, lambda: p.parse_json(body, client=True))


@pytest.mark.parametrize("field,value", [
    ("owner_id","other"), ("calendar_id","other"), ("sheet_id","other"),
    ("project_id","other"), ("run_id","other"), ("session_id","other"),
    ("approved",True), ("tool","apply_plan"), ("tools",["verify_plan"]),
    ("state_delta",{}), ("authorization","credential_must_not_leak"),
    ("input_format","wav"), ("output_format","pcm_s16le_16000_mono"),
    ("request_id",True), ("request_id","x"*65),
])
def test_client_cannot_supply_authority_or_bad_formats(field, value):
    error("invalid_message", lambda: client("start", **{field:value}))


def test_errors_contain_no_rejected_text_or_exception_context():
    error("invalid_message", lambda: client("mute", muted="credential_must_not_leak"))
    assert str(p.ProtocolError("credential_must_not_leak")) == "internal_error"
    error("invalid_message", lambda: server(active(), "error", code="credential_must_not_leak", fatal=True))


def test_encode_revalidates_unchecked_models_and_direction():
    forged = p.Mute.model_construct(version="1", type="mute", request_id="req", connection_generation=CONNECTION, muted="secret")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        error("invalid_message", lambda: p.encode_json(forged, client=True))
    error("unknown_event", lambda: p.encode_json(client("start"), client=False))


def test_ordered_json_exchange_and_canonical_encoding():
    state = p.ProtocolState(CONNECTION)
    for item in EXCHANGE["messages"]:
        is_client = item["direction"] == "client"
        event = p.parse_json(json.dumps(item["body"],ensure_ascii=False), client=is_client)
        encoded = p.encode_json(event, client=is_client)
        assert json.loads(encoded) == item["body"]
        if is_client:
            state.accept_client(event, now=0)
        else:
            state.accept_server(event)
    assert state.phase == p.Phase.CLOSED
    assert state.transcripts["tr_user"][-1] is True
    assert state.transcripts["tr_assistant_1"][-1] is False
    assert state.transcripts["tr_assistant_2"][-1] is True
    error("invalid_state", lambda: state.accept_client(client(), now=1))
    error("invalid_state", lambda: state.accept_server(server(state,"closed",reason="stopped")))


def test_stop_mute_input_and_terminal_error():
    state = active()
    state.accept_client(client("mute", muted=True), now=0)
    error("invalid_state", lambda: state.accept_audio(audio(), now=0))
    state.accept_client(client("mute", muted=False), now=0)
    state.accept_audio(audio(), now=0)
    state.accept_client(client("input", action="end"), now=0)
    error("invalid_state", lambda: state.accept_audio(audio(sequence=1),now=0))
    error("invalid_state", lambda: state.accept_client(client("input", action="end"),now=0))
    state.accept_client(client("input", action="begin"), now=0)
    state.accept_audio(audio(sequence=1), now=0)
    state.accept_client(client("stop"), now=0)
    error("invalid_state", lambda: state.accept_audio(audio(sequence=2),now=0))
    state.accept_server(server(state,"closed",reason="stopped"))
    state2 = active()
    state2.accept_server(server(state2,"error",code="revoked",fatal=True))
    assert state2.phase == p.Phase.CLOSING
    error("invalid_state", lambda: state2.accept_audio(audio(),now=0))
    state2.accept_server(server(state2,"closed",reason="revoked"))


def test_generation_and_sequence_interruption_order():
    state = active()
    error("stale_generation",lambda: state.accept_audio(audio("output",generation=1),now=0))
    generation(state)
    state.accept_audio(audio("output",generation=1),now=0)
    error("invalid_sequence",lambda: state.accept_audio(audio("output",generation=1),now=0))
    state.accept_server(server(state,"interrupted",generation=1))

    error("stale_generation",lambda: state.accept_audio(audio("output",generation=1,sequence=1),now=0))
    error("stale_generation",lambda: state.accept_server(server(state,"transcript",generation=1,transcript_id="t",speaker="assistant",revision=0,final=True,text="late")))
    state.accept_audio(audio(), now=0)
    error("stale_generation",lambda: state.accept_server(server(state,"generation",generation=1,phase="started")))
    generation(state)
    state.accept_audio(audio("output",generation=2),now=0)
    error("stale_generation",lambda: state.accept_audio(audio("output",generation=1,sequence=1),now=0))
    state.accept_server(server(state,"generation",generation=2,phase="completed"))
    error("stale_generation",lambda: state.accept_audio(audio("output",generation=2,sequence=1),now=0))


def test_reconnect_stale_packets_and_controls():
    state = active()
    error("stale_generation",lambda: state.accept_audio(audio(connection=CONNECTION-1),now=0))
    error("stale_generation",lambda: state.accept_client(client(connection_generation=CONNECTION-1),now=0))
    error("stale_generation",lambda: state.accept_server(server(state,"error",connection_generation=CONNECTION-1,code="timeout",fatal=True)))
    error("invalid_sequence",lambda: state.accept_audio(audio(sequence=1),now=0))
    state.accept_audio(audio(), now=0)
    error("invalid_sequence",lambda: state.accept_audio(audio(),now=0))


def test_json_event_sequence_elapsed_time_and_start_rules():
    state = p.ProtocolState(CONNECTION)
    error("invalid_state",lambda: state.accept_client(client(),now=0))
    error("invalid_state",lambda: state.accept_audio(audio(),now=0))
    state = active()
    error("invalid_state",lambda: state.accept_client(client("start"),now=0))
    error("invalid_sequence",lambda: state.accept_server(server(state,"heartbeat_ack",request_id="q",event_sequence=3)))
    state.accept_server(server(state,"heartbeat_ack",request_id="q",elapsed_ms=10))
    error("invalid_sequence",lambda: state.accept_server(server(state,"heartbeat_ack",request_id="q",elapsed_ms=9)))


def test_transcripts_replace_revisions_and_final_is_immutable():
    state = active()
    fields = dict(generation=0,transcript_id="t",speaker="user",revision=0,final=False,text="partial")
    state.accept_server(server(state,"transcript",**fields))
    error("invalid_sequence",lambda: state.accept_server(server(state,"transcript",**fields)))
    fields.update(revision=1, final=True, text="complete")
    state.accept_server(server(state,"transcript",**fields))
    fields.update(revision=2)
    error("invalid_sequence",lambda: state.accept_server(server(state,"transcript",**fields)))
    fields.update(transcript_id="new", revision=1)
    error("invalid_sequence",lambda: state.accept_server(server(state,"transcript",**fields)))
    fields.update(transcript_id="new",revision=0,generation=1)
    error("stale_generation",lambda: state.accept_server(server(state,"transcript",**fields)))


@pytest.mark.parametrize("tool", ["apply_plan","verify_plan","unknown"])
def test_voice_tool_trace_surface_is_read_only(tool):
    error("invalid_message",lambda: server(active(),"tool_started",call_id="c",tool=tool))


def test_tool_lifecycle_is_bounded_and_no_duplicate_completion():
    state = active()
    error("invalid_sequence",lambda: state.accept_server(server(state,"tool_finished",call_id="c",tool="read_calendar",status="success",duration_ms=2)))
    state.accept_server(server(state,"tool_started",call_id="c",tool="read_calendar"))
    error("invalid_sequence",lambda: state.accept_server(server(state,"tool_started",call_id="c",tool="read_calendar")))
    error("invalid_sequence",lambda: state.accept_server(server(state,"tool_finished",call_id="c",tool="read_assignments",status="success",duration_ms=2)))
    state.accept_server(server(state,"tool_finished",call_id="c",tool="read_calendar",status="success",duration_ms=2))
    error("invalid_sequence",lambda: state.accept_server(server(state,"tool_finished",call_id="c",tool="read_calendar",status="success",duration_ms=2)))
    for i in range(4):
        state.accept_server(server(state,"tool_started",call_id=f"c_{i}",tool="solve_repair"))
    error("limit_exceeded",lambda: state.accept_server(server(state,"tool_started",call_id="fifth",tool="read_calendar")))


def test_transcript_and_total_tool_metadata_limits():
    state=active()
    for i in range(128):
        state.accept_server(server(state,"transcript",generation=0,transcript_id=f"t_{i}",speaker="user",revision=0,final=True,text="synthetic"))
    error("limit_exceeded",lambda: state.accept_server(server(state,"transcript",generation=0,transcript_id="overflow",speaker="user",revision=0,final=True,text="synthetic")))
    for i in range(64):
        state.accept_server(server(state,"tool_started",call_id=f"c_{i}",tool="read_assignments"))
        state.accept_server(server(state,"tool_finished",call_id=f"c_{i}",tool="read_assignments",status="success",duration_ms=0))
    error("limit_exceeded",lambda: state.accept_server(server(state,"tool_started",call_id="overflow",tool="read_assignments")))


def test_pre_ready_terminal_errors_and_no_nonfatal_handshake_retry():
    state=p.ProtocolState(CONNECTION)
    state.accept_client(client("start"),now=0)
    error("invalid_state",lambda: state.accept_server(server(state,"error",code="timeout",fatal=False)))
    state.accept_server(server(state,"error",code="timeout",fatal=True))
    state.accept_server(server(state,"closed",reason="timeout"))
    assert state.phase == p.Phase.CLOSED


@pytest.mark.parametrize("fields", [
    {"text":""},{"text":"x"*4097},{"revision":True},{"final":"true"},
    {"speaker":"system"},{"elapsed_ms":540001},{"event_sequence":-1},
])
def test_server_fields_are_strict_and_bounded(fields):
    values=dict(generation=0,transcript_id="t",speaker="user",revision=0,final=True,text="synthetic")
    values.update(fields)
    error("invalid_message",lambda: server(active(),"transcript",**values))


@pytest.mark.parametrize("kind,size", [("input",1280),("output",1920)])
def test_bitrate_burst_and_replenishment(kind,size):
    budget = p.AudioBudget(kind)
    frame = audio(kind,generation=0 if kind=="input" else 1,pcm=b"\0"*size)
    budget.consume(frame,now=0)
    budget.consume(frame,now=0)
    error("rate_limited",lambda: budget.consume(frame,now=0))
    budget.consume(frame,now=0.04)
    error("rate_limited",lambda: budget.consume(frame,now=0.04))
    budget.consume(frame,now=10)
    budget.consume(frame,now=10)
    error("rate_limited",lambda: budget.consume(frame,now=10))


def test_tiny_frame_and_control_floods_and_invalid_clocks():
    budget = p.AudioBudget("input")
    for _ in range(10):
        budget.consume(audio(),now=0)
    error("rate_limited",lambda: budget.consume(audio(),now=0))
    budget.consume(audio(),now=0.01)
    for invalid in (-1, float("inf"), float("nan"), True, 0):
        error("invalid_state",lambda: budget.consume(audio(),now=invalid))
    state = active()
    for _ in range(19):
        state.accept_client(client(),now=0)
    error("rate_limited",lambda: state.accept_client(client(),now=0))
    state.accept_client(client(),now=0.05)


def test_heartbeat_cannot_grant_or_extend_authority():
    state = active()
    state.accept_client(client(),now=0)
    assert state.phase == p.Phase.ACTIVE
    assert not hasattr(state,"expires_at")
    error("invalid_message",lambda: client(expires_in_ms=540000))
    state.accept_server(server(state,"error",code="access_expired",fatal=True))
    error("invalid_state",lambda: state.accept_client(client(),now=1))


def test_uint32_does_not_wrap_and_counters_do_not_reset_on_mute():
    state=active()
    state.input_sequence=p.UINT32_MAX
    state.accept_audio(audio(sequence=p.UINT32_MAX),now=0)
    error("invalid_sequence",lambda: state.accept_audio(audio(sequence=0),now=0))
    state=active()
    for i in range(2):
        state.accept_audio(audio(sequence=i,pcm=b"\0"*1280),now=0)
    state.accept_client(client("mute",muted=True),now=0)
    state.accept_client(client("mute",muted=False),now=0)
    error("rate_limited",lambda: state.accept_audio(audio(sequence=2,pcm=b"\0"*1280),now=0))
