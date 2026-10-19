"""W08 Windows local evidence; actual loopback TLS, no Google/device access."""
import asyncio
import io
import json
from pathlib import Path
import socket
import ssl
import struct
import sys
from types import SimpleNamespace
import wave

import pytest

from campuspulse_recovery.voice import protocol as p
from tools.voice_windows.audio import AudioDevice, OutputBuffer, Pacer, decode_wav, synthetic_wav
from tools.voice_windows.client import (ClientError, NativeHTTP, PairingClient, VoiceClient,
    microphone_turns, origin_checked, proof, safe_event, self_check, wss_connection)
from tools.voice_windows.local import local_e2e, local_server, synthetic_pair
from tools.voice_windows.__main__ import main, parse_args


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def only_loopback(monkeypatch):
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    def check(address):
        assert isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"), "external_network_denied"
    def connect(sock, address):
        check(address)
        return original(sock, address)
    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


def make_wav(samples, rate=16000, channels=1, width=2):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        w.writeframes(struct.pack("<" + "h" * len(samples), *samples) if width == 2 else bytes(samples))
    return out.getvalue()


def test_current_source_golden_and_no_optional_audio_import():
    import tools.voice_windows.client as module
    assert Path(module.__file__).resolve() == Path(__file__).resolve().parents[2] / "tools/voice_windows/client.py"
    assert "sounddevice" not in sys.modules
    assert self_check()["controls"] == 18
    assert len(decode_wav(synthetic_wav())) == 12800


def test_actual_conversion_sample_oracles_and_duration():
    samples = [-32768, -1, 0, 1, 32767, 4660]
    assert decode_wav(make_wav(samples)) == struct.pack("<6h", *samples)
    assert struct.unpack("<2h", decode_wav(make_wav([100, 300, -100, -300], channels=2))) == (200, -200)
    assert struct.unpack("<2h", decode_wav(make_wav([0, 3, 6, -3, -6, -9], rate=48000))) == (3, -6)
    assert struct.unpack("<2h", decode_wav(make_wav([100, 300] * 3 + [-100, -300] * 3,
                                                                 rate=48000, channels=2))) == (200, -200)
    assert len(decode_wav(make_wav([0] * 48000, rate=48000))) == 32000


@pytest.mark.parametrize("raw", [b"", b"not wave", synthetic_wav()[:-1],
    synthetic_wav() + b"extra", make_wav([1, 2], rate=44100), make_wav([1], width=1),
    make_wav([], rate=16000), make_wav([1, 2], rate=48000), make_wav([1, 2, 3], channels=3)],
    ids=["empty", "bad_magic", "truncated", "trailing", "rate", "width", "no_samples", "partial_group", "channels"])
def test_invalid_wav_rejection(raw):
    with pytest.raises(ValueError, match="^invalid_wav$"):
        decode_wav(raw)


def test_truncated_data_with_adjusted_riff_length_is_rejected():
    raw = bytearray(synthetic_wav()[:-4])
    struct.pack_into("<I", raw, 4, len(raw) - 8)
    with pytest.raises(ValueError, match="invalid_wav"):
        decode_wav(bytes(raw))


def test_wav_odd_data_length_with_valid_padding_is_rejected():
    raw = bytearray(make_wav([12]))
    raw.extend(b"\x01\x00")
    struct.pack_into("<I", raw, 4, len(raw) - 8)
    struct.pack_into("<I", raw, 40, 3)
    with pytest.raises(ValueError, match="invalid_wav"):
        decode_wav(bytes(raw))


def test_pacer_monotonic_no_catchup_bursts():
    async def scenario():
        now, times = [0.], []
        async def sleep(delay):
            now[0] += delay
        pacer = Pacer(lambda: now[0], sleep)
        for i in range(5):
            if i == 2:
                now[0] += .5
            await pacer.wait(.02)
            times.append(now[0])
        assert times == pytest.approx([0, .02, .52, .54, .56])
    run(scenario())


class NullSocket:
    def __init__(self):
        self.sent, self.closed = [], False
    async def send(self, packet):
        self.sent.append(packet)
    async def close(self):
        self.closed = True


class DelayedStopSocket(NullSocket):
    """A strict W02 peer produces ACK before seeing Stop, but delivers it later."""
    def __init__(self, crossing=()):
        super().__init__()
        self.incoming, self.held = asyncio.Queue(), []
        self.peer = p.ProtocolState(7)
        self.crossing = crossing
        self.client = None
        self.phase_at_ack_delivery = None

    def server_event(self, kind, **values):
        event = p.parse_json(json.dumps(dict(version="1", type=kind,
            event_id=f"e{self.peer.event_sequence}", event_sequence=self.peer.event_sequence,
            connection_generation=7, elapsed_ms=0, **values)), client=False)
        self.peer.accept_server(event)
        return p.encode_json(event, client=False)

    async def send(self, packet):
        await super().send(packet)
        control = p.parse_json(packet, client=True)
        if isinstance(control, p.Stop):


            for kind, values in self.crossing:
                if kind == "audio":
                    frame = p.AudioFrame("output", 7, 1, 0, bytes(960))
                    self.peer.accept_audio(frame, now=asyncio.get_running_loop().time())
                    self.held.append(p.encode_audio(frame))
                else:
                    self.held.append(self.server_event(kind, **values))
            self.peer.accept_client(control, now=asyncio.get_running_loop().time())
            for held in self.held:
                self.incoming.put_nowait(held)
            self.incoming.put_nowait(self.server_event("closed", reason="stopped"))
        else:
            self.peer.accept_client(control, now=asyncio.get_running_loop().time())
            if isinstance(control, p.Start):
                self.incoming.put_nowait(self.server_event("ready", session_id="session_synthetic",
                    run_id="run_synthetic", expires_in_ms=5000, input_format=p.INPUT_FORMAT,
                    output_format=p.OUTPUT_FORMAT))
            elif isinstance(control, p.Heartbeat):
                self.held.append(self.server_event("heartbeat_ack", request_id=control.request_id))

    async def recv(self):
        packet = await self.incoming.get()
        if isinstance(packet, str) and json.loads(packet)["type"] == "heartbeat_ack":
            self.phase_at_ack_delivery = self.client.state.phase
        return packet


@pytest.mark.parametrize("crossing", [(), (
    ("generation", dict(generation=1, phase="started")), ("audio", {}),
    ("transcript", dict(generation=1, transcript_id="partial", speaker="assistant", revision=0,
                        final=False, text="synthetic-private-crossing-transcript")),
    ("generation", dict(generation=1, phase="completed")),
    ("tool_started", dict(call_id="crossing_read", tool="read_assignments")),
    ("tool_finished", dict(call_id="crossing_read", tool="read_assignments", status="success", duration_ms=0)),
)], ids=["delayed_ack", "duplex_events_before_peer_stop"])
def test_ack_produced_before_stop_delivered_after_local_stop_is_validated_and_fenced(crossing):
    async def scenario():
        sock = DelayedStopSocket(crossing)
        heard, traces = [], []
        async def sink(pcm): heard.append(pcm)
        c = VoiceClient(sock, sink=sink, trace=traces.append)
        sock.client = c
        async def source(client):
            await client.control("heartbeat")
            assert len(sock.held) == 1
        result = await c.run(source)
        assert sock.phase_at_ack_delivery == p.Phase.CLOSING
        assert result["closed_reason"] == "stopped" and result["heartbeat_acks"] == 1
        assert not heard and c.output.bytes == 0 and not c.response_done.is_set()
        assert not {t["type"] for t in traces} & {"generation", "transcript", "tool_started", "tool_finished"}
        assert sock.closed and all(t.done() for t in c.tasks)
        assert result["shutdown_discarded_packets"] == len(crossing)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


@pytest.mark.parametrize("case", ["unknown_ack", "duplicate_ack", "out_of_order_ack", "event_gap",
    "wrong_connection", "elapsed_backwards", "audio_sequence", "audio_generation", "audio_direction",
    "transcript_revision", "unknown_call", "generation_gap", "duplicate_ready"], ids=str)
def test_shutdown_rejects_invalid_crossing_responses(case):
    async def scenario():
        c = VoiceClient(NullSocket())
        boot(c)
        event(c, "generation", generation=1, phase="started")
        await c.control("heartbeat")
        await c.control("heartbeat")
        ids = list(c.pending_heartbeats)
        await c.control("stop")
        def packet(kind="heartbeat_ack", **values):
            body = dict(version="1", type=kind, event_id=f"e{c._receive_drain.event_sequence}",
                event_sequence=c._receive_drain.event_sequence, connection_generation=7, elapsed_ms=0)
            body.update(values)
            return json.dumps(body)
        bad = packet(request_id=ids[0])
        if case == "unknown_ack":
            bad = packet(request_id="unsent")
        elif case == "duplicate_ack":
            c.receive_packet(packet(request_id=ids[0]))
            bad = packet(request_id=ids[0])
        elif case == "out_of_order_ack":
            bad = packet(request_id=ids[1])
        elif case == "event_gap":
            bad = packet(request_id=ids[0], event_sequence=c._receive_drain.event_sequence + 1)
        elif case == "wrong_connection":
            bad = packet(request_id=ids[0], connection_generation=8)
        elif case == "elapsed_backwards":
            c.receive_packet(packet(request_id=ids[0], elapsed_ms=2))
            bad = packet(request_id=ids[1], elapsed_ms=1)
        elif case in ("audio_sequence", "audio_generation", "audio_direction"):
            bad = p.encode_audio(p.AudioFrame("input" if case == "audio_direction" else "output", 7,
                0 if case == "audio_direction" else 2 if case == "audio_generation" else 1,
                1 if case == "audio_sequence" else 0, bytes(640)))
        elif case == "transcript_revision":
            bad = packet("transcript", generation=1, transcript_id="new", speaker="assistant", revision=1,
                         final=False, text="synthetic-private")
        elif case == "unknown_call":
            bad = packet("tool_finished", call_id="unknown", tool="read_assignments", status="success", duration_ms=0)
        elif case == "generation_gap":
            bad = packet("generation", generation=3, phase="started")
        elif case == "duplicate_ready":
            bad = packet("ready", session_id="session_synthetic", run_id="run_synthetic", expires_in_ms=5000,
                         input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT)
        with pytest.raises(p.ProtocolError):
            c.receive_packet(bad)
        assert c.state.phase == p.Phase.CLOSING and not c.closed.is_set() and c.output.bytes == 0
    run(scenario())


def test_stop_flushes_output_immediately_and_blocks_input_controls_and_post_terminal_packets():
    async def scenario():
        flushed = []
        c = VoiceClient(NullSocket(), flush=lambda: flushed.append(True))
        boot(c)
        event(c, "generation", generation=1, phase="started")
        c.receive_packet(p.encode_audio(p.AudioFrame("output", 7, 1, 0, bytes(960))))
        assert c.output.bytes == 960
        await c.control("stop")
        assert c.output.bytes == 0 and c.output.dropped == 960 and flushed
        for kind, values in (("heartbeat", {}), ("input", dict(action="begin")), ("stop", {})):
            with pytest.raises(p.ProtocolError, match="invalid_state"):
                await c.control(kind, **values)
        with pytest.raises(p.ProtocolError, match="invalid_state"):
            await c.send_pcm(bytes(640))
        c.receive_packet(json.dumps(dict(version="1", type="closed", event_id="terminal",
            event_sequence=c._receive_drain.event_sequence, connection_generation=7, elapsed_ms=0, reason="stopped")))
        assert c.state.phase == p.Phase.CLOSED
        for packet in (p.encode_audio(p.AudioFrame("output", 7, 1, 0, bytes(960))),
                json.dumps(dict(version="1", type="heartbeat_ack", event_id="late",
                    event_sequence=c.state.event_sequence, connection_generation=7, elapsed_ms=0, request_id="unsent"))):
            with pytest.raises(p.ProtocolError, match="invalid_state"):
                c.receive_packet(packet)
    run(scenario())


def test_shutdown_drain_has_message_and_deadline_bounds_without_validation_bypass():
    async def scenario():
        c = VoiceClient(NullSocket())
        boot(c)
        await c.control("stop")
        for index in range(p.MAX_QUEUED_EVENTS):

            c.receive_packet(json.dumps(dict(version="1", type="generation", event_id=f"g{index}",
                event_sequence=c._receive_drain.event_sequence, connection_generation=7, elapsed_ms=0,
                generation=index // 2 + 1, phase="started" if index % 2 == 0 else "completed")))
        with pytest.raises(p.ProtocolError, match="limit_exceeded"):
            c.receive_packet(json.dumps(dict(version="1", type="closed", event_id="terminal",
                event_sequence=c._receive_drain.event_sequence, connection_generation=7, elapsed_ms=0, reason="stopped")))
        other = VoiceClient(NullSocket())
        boot(other)
        await other.control("stop")
        other._stop_deadline = 0
        with pytest.raises(ClientError, match="stop_timeout"):
            other.receive_packet("{}")
        assert not other.closed.is_set() and not c.closed.is_set()
    run(scenario())


def boot(client, connection=7):
    client.receive_packet(json.dumps(dict(version="1", type="ready", event_id="e0", event_sequence=0,
        connection_generation=connection, elapsed_ms=0, session_id="session_synthetic", run_id="run_synthetic",
        expires_in_ms=5000, input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT)))


def event(client, kind, **values):
    client.receive_packet(json.dumps(dict(version="1", type=kind, event_id=f"e{client.state.event_sequence}",
        event_sequence=client.state.event_sequence, connection_generation=client.state.connection_generation,
        elapsed_ms=0, **values)))


def test_ready_first_pcm_and_next_turn_begin_independent_sequences():
    async def scenario():
        sock = NullSocket()
        c = VoiceClient(sock)
        boot(c)
        await c.wav_turn(bytes(640))
        await asyncio.sleep(.02)
        await c.wav_turn(bytes(640))
        controls = [json.loads(v) for v in sock.sent if isinstance(v, str)]
        assert [v["action"] for v in controls] == ["end", "begin", "end"]
        frames = [p.decode_audio(v, expected_kind="input") for v in sock.sent if isinstance(v, bytes)]
        assert [f.sequence for f in frames] == [0, 1]
        assert {f.generation for f in frames} == {0}
    run(scenario())


def test_interruption_cancels_inflight_flushes_and_drops_stale_audio_and_transcript():
    async def scenario():
        heard, entered = [], asyncio.Event()
        async def sink(pcm):
            if pcm[0] == 1:
                entered.set()
                await asyncio.Event().wait()
            heard.append(pcm)
        c = VoiceClient(NullSocket(), sink=sink)
        boot(c)
        event(c, "generation", generation=1, phase="started")
        old = p.AudioFrame("output", 7, 1, 0, b"\x01\x00" * 480)
        c.receive_packet(p.encode_audio(old))
        consumer = asyncio.create_task(c.output.run())
        await asyncio.wait_for(entered.wait(), 1)
        c.receive_packet(p.encode_audio(p.AudioFrame("output", 7, 1, 1, old.pcm)))
        event(c, "interrupted", generation=1)
        c.receive_packet(p.encode_audio(p.AudioFrame("output", 7, 1, 2, old.pcm)))
        event(c, "transcript", generation=1, transcript_id="old", speaker="assistant", revision=0,
              final=False, text="synthetic-private-transcript")
        event(c, "generation", generation=2, phase="started")
        fresh = b"\x02\x00" * 480
        c.receive_packet(p.encode_audio(p.AudioFrame("output", 7, 2, 0, fresh)))
        await asyncio.sleep(.06)
        assert heard == [fresh] and c.stale_drops == 2 and c.output.dropped == 1920
        assert c.output.bytes == 0 and "old" not in c.state.transcripts
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    run(scenario())


def test_wrong_connection_future_generation_sequence_and_direction_rejected():
    c = VoiceClient(NullSocket())
    boot(c)
    event(c, "generation", generation=1, phase="started")
    for frame in (p.AudioFrame("output", 8, 1, 0, bytes(960)),
                  p.AudioFrame("output", 7, 2, 0, bytes(960)),
                  p.AudioFrame("output", 7, 1, 1, bytes(960)),
                  p.AudioFrame("input", 7, 0, 0, bytes(640))):
        with pytest.raises(p.ProtocolError):
            c.receive_packet(p.encode_audio(frame))
    c.receive_packet(p.encode_audio(p.AudioFrame("output", 6, 1, 0, bytes(960))))
    assert c.stale_drops == 1 and c.output.bytes == 0


def test_bounded_queue_includes_inflight_and_slow_sink_timeout():
    async def scenario():
        entered = asyncio.Event()
        async def stalled(pcm):
            entered.set()
            await asyncio.Event().wait()
        c = VoiceClient(NullSocket(), sink=stalled, sink_timeout=.05)
        frame = p.AudioFrame("output", 7, 1, 0, bytes(1920))
        c.output.put(frame)
        task = asyncio.create_task(c.output.run())
        await entered.wait()
        for _ in range(4):
            c.output.put(frame)
        assert c.output.bytes == c.output.peak == 9600
        with pytest.raises(p.ProtocolError, match="backpressure"):
            c.output.put(frame)
        with pytest.raises(TimeoutError):
            await task
        assert c.output.bytes == 0
    run(scenario())


def test_fake_only_microphone_explicit_turns_and_device_cleanup():
    async def scenario():
        opened, ended, commands = [], [], asyncio.Queue()
        sock = NullSocket()
        c = VoiceClient(sock)
        boot(c)
        class Device:
            def start_input(self): opened.append(True)
            def end_input(self): ended.append(True)
            async def read(self):
                await asyncio.sleep(.02)
                return bytes(640)
        task = asyncio.create_task(microphone_turns(c, Device(), commands.get))
        await asyncio.sleep(.02)
        assert not opened and not [v for v in sock.sent if isinstance(v, bytes)]
        for cmd in ("start", "end", "start", "stop"):
            await commands.put(cmd)
            await asyncio.sleep(.065)
        await task
        assert len(opened) == 2 and len(ended) == 3
        controls = [json.loads(v) for v in sock.sent if isinstance(v, str)]
        assert [v["action"] for v in controls] == ["end", "begin", "end", "begin", "end"]
        frames = [p.decode_audio(v) for v in sock.sent if isinstance(v, bytes)]
        assert [f.sequence for f in frames] == list(range(len(frames))) and len(frames) >= 2
        count = len(sock.sent)
        await asyncio.sleep(.04)
        assert len(sock.sent) == count
    run(scenario())


def test_audio_adapter_callbacks_use_exact_formats_bounds_and_flush_without_hardware():
    streams = []
    class Stream:
        def __init__(self, **kwargs):
            self.kwargs, self.started, self.closed, self.aborted = kwargs, False, False, False
            streams.append(self)
        def start(self): self.started = True
        def abort(self): self.aborted = True
        def close(self): self.closed = True
    audio = AudioDevice(SimpleNamespace(RawInputStream=Stream, RawOutputStream=Stream))
    audio.start_input()
    assert streams[0].kwargs["samplerate"] == 16000 and streams[0].kwargs["blocksize"] == 320
    callback = streams[0].kwargs["callback"]
    for _ in range(11):
        callback(bytes(640), 320, None, False)
    assert audio.capture.qsize() == 10 and audio.failed
    audio.end_input()
    audio.failed = False
    audio.start_output()
    assert streams[1].kwargs["samplerate"] == 24000 and streams[1].kwargs["blocksize"] == 480
    out = bytearray(960)
    async def played():
        task = asyncio.create_task(audio.play(b"\x42\x00" * 480))
        await asyncio.sleep(.01)
        assert not task.done()
        streams[1].kwargs["callback"](out, 480, None, False)
        await task
    run(played())
    assert out == b"\x42\x00" * 480
    audio.output_pcm.extend(bytes(9600))
    with pytest.raises(p.ProtocolError, match="backpressure"):
        run(audio.play(bytes(2)))
    audio.flush()
    streams[1].kwargs["callback"](out, 480, None, False)
    assert out == bytes(960)
    audio.close()
    assert all(s.closed and s.aborted for s in streams)


def test_microphone_capture_failure_cancels_waiting_operator_without_hardware():
    async def scenario():
        c = VoiceClient(NullSocket())
        boot(c)
        commands = asyncio.Queue()
        commands.put_nowait("start")
        ended = []
        class Device:
            def start_input(self): pass
            def end_input(self): ended.append(True)
            async def read(self): raise p.ProtocolError("backpressure")
        with pytest.raises(p.ProtocolError, match="backpressure"):
            await asyncio.wait_for(microphone_turns(c, Device(), commands.get), 1)
        assert ended
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_end_during_admitted_pcm_send_joins_before_next_microphone_utterance():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Blocked(NullSocket):
            async def send(self, packet):
                if isinstance(packet, bytes) and not entered.is_set():
                    entered.set()
                    await release.wait()
                await super().send(packet)
        sock, commands, reads = Blocked(), asyncio.Queue(), asyncio.Queue()
        c = VoiceClient(sock)
        boot(c)
        class Device:
            def start_input(self): pass
            def end_input(self): pass
            async def read(self): return await reads.get()
        task = asyncio.create_task(microphone_turns(c, Device(), commands.get))
        await commands.put("start")
        await reads.put(bytes(640))
        await entered.wait()
        await commands.put("end")
        await asyncio.sleep(.03)

        assert [json.loads(v).get("action") for v in sock.sent] == ["end", "begin"]
        release.set()
        await asyncio.sleep(.03)
        await commands.put("start")
        await reads.put(bytes(640))
        await asyncio.sleep(.05)
        await commands.put("stop")
        await task
        frames = [p.decode_audio(v) for v in sock.sent if isinstance(v, bytes)]
        assert [f.sequence for f in frames] == [0, 1]
        assert not c.uncertain
        assert [json.loads(v).get("action") for v in sock.sent if isinstance(v, str)] == ["end", "begin", "end", "begin", "end"]
    run(scenario())


def test_cancel_admitted_pcm_terminates_uncertain_connection_and_never_reuses_sequence():
    async def scenario():
        entered = asyncio.Event()
        class Blocked(NullSocket):
            async def send(self, packet):
                entered.set()
                await asyncio.Event().wait()
        sock = Blocked()
        c = VoiceClient(sock)
        boot(c)
        task = asyncio.create_task(c.send_pcm(bytes(640)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sock.closed and c.uncertain
        with pytest.raises(ClientError, match="connection_uncertain"):
            await c.control("input", action="end")
        with pytest.raises(ClientError, match="connection_uncertain"):
            await c.send_pcm(bytes(640))
        assert not sock.sent
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_initial_start_send_has_aggregate_bound_and_closes_socket():
    async def scenario():
        class Blocked(NullSocket):
            async def send(self, packet): await asyncio.Event().wait()
        sock = Blocked()
        c = VoiceClient(sock)
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(c.run(lambda client: asyncio.sleep(0)), 3)
        assert asyncio.get_running_loop().time() - started < 2.8
        assert sock.closed and not c.tasks
    run(scenario())


@pytest.mark.parametrize("cancel", [True, False], ids=["cancel", "aggregate_deadline"])
def test_pairing_http_cancellation_and_deadline_close_request_owned_transport(monkeypatch, cancel):
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        class Reader:
            async def readuntil(self, separator):
                entered.set()
                await asyncio.Event().wait()
        class Writer:
            def write(self, data): pass
            async def drain(self): pass
            def close(self): closed.set()
            async def wait_closed(self): pass
        async def open_connection(*args, **kwargs):
            assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
            assert kwargs["server_hostname"] == "example.com"
            return Reader(), Writer()
        monkeypatch.setattr(asyncio, "open_connection", open_connection)
        http = NativeHTTP("https://example.com")
        http.timeout = .05
        task = asyncio.create_task(http.post("/api/voice/pairing/start", {"challenge": "synthetic"}))
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ClientError, match="pairing_denied_or_unavailable"):
                await task
        assert closed.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_heartbeat_missing_inbound_and_expiry_fail_closed():
    async def scenario():
        c = VoiceClient(NullSocket(), heartbeat=.001)
        boot(c)
        c.last_inbound -= 31
        with pytest.raises(ClientError, match="heartbeat_timeout"):
            await c.heartbeats()
        c.deadline = 0
        with pytest.raises(ClientError, match="session_expired"):
            await c.heartbeats()
    run(scenario())


@pytest.mark.parametrize("packet", [b"x" * 2049, b"x" * 1305, '{"version":"2","type":"start"}'],
                         ids=["server_frame_bound", "asgi_binary_bound", "protocol_version"])
def test_actual_socket_malformed_input_is_terminal_and_upstream_closes(packet):
    async def scenario():
        from websockets.exceptions import ConnectionClosed
        async with local_server() as server:
            token = await synthetic_pair(server)
            async with wss_connection(server.origin, token, server.context) as socket:
                c = VoiceClient(socket)
                await socket.send(p.encode_json(c.start_event, client=True))
                c.receive_packet(await socket.recv())
                await socket.send(packet)
                with pytest.raises(ConnectionClosed):
                    while True:
                        await asyncio.wait_for(socket.recv(), 2)
            async with asyncio.timeout(2):
                while not server.provider.stream.closed:
                    await asyncio.sleep(.01)
            assert server.provider.stream.cancelled
    run(scenario())


def test_real_tls_socket_route_pairing_wav_duplex_heartbeat_and_cleanup(caplog):
    async def scenario():
        result = await local_e2e(decode_wav(synthetic_wav()))
        assert result["tls"] == "ephemeral_ca_verified" and result["duplex"]
        assert result["input_bytes"] == 12800 and result["output_bytes"] == 4800
        assert result["heartbeat_acks"] >= 1 and result["closed_reason"] == "stopped"
        assert result["observed_input_span_ms"] >= 350 and result["elapsed_ms"] >= 400
        assert result["upstream_cancelled"] and result["upstream_closed"]
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())
    assert "Bearer" not in caplog.text and "synthetic-local-signin" not in caplog.text


def test_actual_route_ack_held_after_tls_read_until_local_stop_admission():
    async def scenario():
        async with local_server() as server:
            token = await synthetic_pair(server)
            async with wss_connection(server.origin, token, server.context) as socket:
                ack_held, stop_sent = asyncio.Event(), asyncio.Event()
                phases = []
                class DelayedDelivery:
                    async def send(self, packet):
                        await socket.send(packet)
                        if isinstance(packet, str) and json.loads(packet)["type"] == "stop":
                            stop_sent.set()
                    async def recv(self):
                        packet = await socket.recv()
                        if isinstance(packet, str) and json.loads(packet)["type"] == "heartbeat_ack":


                            ack_held.set()
                            await stop_sent.wait()
                            phases.append(client.state.phase)
                        return packet
                    async def close(self): await socket.close()
                client = VoiceClient(DelayedDelivery())
                async def source(c):
                    await c.control("heartbeat")
                    await asyncio.wait_for(ack_held.wait(), 2)
                result = await client.run(source)
                assert phases == [p.Phase.CLOSING]
                assert result["heartbeat_acks"] == 1 and result["closed_reason"] == "stopped"
                assert all(t.done() for t in client.tasks)
            async with asyncio.timeout(2):
                while not server.provider.stream.closed:
                    await asyncio.sleep(.01)
            assert server.provider.stream.cancelled
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_real_tls_rejects_untrusted_ca_and_native_cookie_origin_query():
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    async def scenario():
        async with local_server() as server:
            token = await synthetic_pair(server)
            with pytest.raises(ssl.SSLCertVerificationError):
                async with wss_connection(server.origin, token):
                    pass
            url = "wss://" + server.origin[8:] + "/api/voice/ws"
            for headers, suffix in (({"Cookie": "synthetic=x"}, ""), ({"Origin": server.origin}, ""),
                                    ({}, "?credential=synthetic")):
                with pytest.raises(InvalidStatus):
                    async with connect(url + suffix, ssl=server.context, proxy=None,
                        additional_headers={"Authorization": "Bearer " + token, **headers}):
                        pass
    run(scenario())


def test_real_socket_cancellation_stops_upstream_and_joins_tasks():
    async def scenario():
        async with local_server() as server:
            token = await synthetic_pair(server)
            async with wss_connection(server.origin, token, server.context) as socket:
                c = VoiceClient(socket)
                producing = asyncio.Event()
                async def source(client):
                    await client.send_pcm(bytes(640))
                    producing.set()
                    await asyncio.Event().wait()
                task = asyncio.create_task(c.run(source))
                await producing.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            async with asyncio.timeout(2):
                while not server.provider.stream.closed:
                    await asyncio.sleep(.01)
            assert server.provider.stream.cancelled
            assert all(t.done() for t in c.tasks)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())


def test_real_socket_slow_output_failure_closes_session_and_cancels_upstream():
    async def scenario():
        async with local_server() as server:
            token = await synthetic_pair(server)
            async with wss_connection(server.origin, token, server.context) as socket:
                async def stalled_sink(pcm): await asyncio.Event().wait()
                c = VoiceClient(socket, sink=stalled_sink, sink_timeout=.03)
                with pytest.raises(TimeoutError):
                    await c.run(lambda client: client.finish_wav(decode_wav(synthetic_wav())))
                assert c.output.bytes == 0 and all(t.done() for t in c.tasks)
            async with asyncio.timeout(2):
                while not server.provider.stream.closed:
                    await asyncio.sleep(.01)
            assert server.provider.stream.cancelled
    run(scenario())


def test_real_pairing_tls_cancel_closes_peer_connection_no_http_worker(tmp_path):
    from tools.voice_windows.local import certificates
    async def scenario():
        context, cert, key = certificates(tmp_path)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        entered, disconnected = asyncio.Event(), asyncio.Event()
        handlers = set()
        async def handler(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                await reader.readuntil(b"\r\n\r\n")
                entered.set()

                while await reader.read(4096):
                    pass
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.remove(asyncio.current_task())
        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_context)
        try:
            port = server.sockets[0].getsockname()[1]
            http = NativeHTTP(f"https://127.0.0.1:{port}", context, local=True)
            task = asyncio.create_task(http.post("/api/voice/pairing/start", {"challenge": "synthetic"}))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 2)
            await asyncio.gather(*handlers)
        finally:
            server.close()
            await server.wait_closed()
        assert not handlers
    run(scenario())


@pytest.mark.parametrize("origin", ["http://example.com", "https://example.com?a=b", "https://a:b@example.com",
    "https://example.com/api/voice/ws", "https://localhost", "https://example.com/#secret"])
def test_bad_backend_origins(origin):
    with pytest.raises(ClientError, match="invalid_backend_origin"):
        origin_checked(origin)


def test_no_unverified_tls_and_s256_vector():
    verifier, challenge = proof()
    from campuspulse_recovery.voice.device_auth import proof_challenge
    assert len(verifier) >= 43 and challenge == proof_challenge(verifier)
    context = ssl._create_unverified_context()
    with pytest.raises(ClientError, match="verified_tls_required"):
        NativeHTTP("https://example.com", context)
    with pytest.raises(ClientError, match="verified_tls_required"):
        wss_connection("https://example.com", "synthetic", context)


def test_pairing_secrets_in_memory_body_only_and_safe_browser_url(monkeypatch, capsys):
    from tools.voice_windows import client as module
    calls, urls, attempts = [], [], []
    class HTTP:
        def __init__(self, origin): pass
        async def post(self, path, body):
            calls.append((path, body))
            if path.endswith("/start"):
                return dict(pairing_code="ABCDEF123456", grant="private-grant", expires_at="synthetic", approval_path="/voice/pair")
            return dict(credential="s" * 43, expires_at="synthetic", scope="voice:read")
    monkeypatch.setattr(module, "NativeHTTP", HTTP)
    monkeypatch.setattr(module.webbrowser, "open", urls.append)
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "CONTINUE")
    assert run(module.browser_pair("https://example.com")) == "s" * 43
    assert urls == ["https://example.com/voice/pair"]
    assert set(calls[0][1]) == {"challenge", "client_kind", "device_name"}
    assert set(calls[1][1]) == {"grant", "verifier"}
    assert calls[1][1]["grant"] == "private-grant"
    assert "private-grant" not in capsys.readouterr().out


def test_cli_invalid_arguments_do_not_echo_secrets_or_enable_devices(capsys):
    for args in (["local-e2e", "--microphone"], ["connect", "--backend", "https://example.com", "--microphone", "--wav", "x"],
                 ["connect", "--credential", "synthetic-secret-do-not-echo"]):
        with pytest.raises(SystemExit) as error:
            parse_args(args)
        assert error.value.code == 2
    assert "synthetic-secret-do-not-echo" not in capsys.readouterr().err
    assert not parse_args([]).microphone and not parse_args([]).playback


def test_cli_output_wav_is_24k_and_explicit_only(tmp_path, monkeypatch, capsys):
    before = set(tmp_path.iterdir())
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    out = tmp_path / "explicit.wav"
    assert main(["local-e2e", "--output-wav", str(out)]) == 0
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() == 2400
        assert set(struct.unpack("<2400h", w.readframes(2400))) == {17, 34}
    assert set(tmp_path.iterdir()) - before == {out}
    assert main(["local-e2e", "--output-wav", str(out)]) == 1
    assert out.stat().st_size == 4844
    logs = capsys.readouterr().out
    assert "Bearer" not in logs and "credential" not in logs and "synthetic-private-transcript" not in logs
