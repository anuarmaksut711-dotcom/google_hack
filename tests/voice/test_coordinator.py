"""Explicit local duplex fakes, synthetic PCM, controlled synchronization."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
import socket

import pytest

from campuspulse_recovery.voice import coordinator as c, ports
from campuspulse_recovery.voice import protocol as p
from campuspulse_recovery.voice.ports import (
    OutputAudio, OutputTranscript, ProviderMode, ResponseBoundary, ResumeUpdate,
    ToolRequest, ToolResult,
)
from tests.storage.test_plans import FakeDocuments, run
from tests.voice.test_sessions import SessionSetup, TOKEN


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network denied in W04")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def control(kind, generation=1, **fields):
    value = dict(version="1", type=kind, request_id="synthetic_request", **fields)
    if kind == "start":
        value.update(input_format=p.INPUT_FORMAT, output_format=p.OUTPUT_FORMAT)
    else:
        value["connection_generation"] = generation
    return json.dumps(value)


async def settle(predicate=lambda: True):
    # Only schedule ready tasks. No real-time sleeps, model or I/O.
    for _ in range(100):
        await asyncio.sleep(0)
        if predicate():
            return
    raise AssertionError("controlled tasks did not reach expected boundary")


class FakeClient:
    def __init__(self):
        self.incoming = asyncio.Queue(maxsize=32)
        self.sent = []
        self.ready = asyncio.Event()
        self.audio_blocked = asyncio.Event()
        self.audio_gate = asyncio.Event()
        self.block_generation = None
        self.audio_cancelled = False
        self.closed = False
        self.incoming.put_nowait(control("start"))

    async def receive(self):
        return await self.incoming.get()

    async def send(self, packet):
        if isinstance(packet, p.AudioFrame) and packet.generation == self.block_generation:
            self.audio_blocked.set()
            try:
                await self.audio_gate.wait()
            except asyncio.CancelledError:
                self.audio_cancelled = True
                raise
        self.sent.append(packet)
        if isinstance(packet, p.Ready): self.ready.set()

    async def close(self):
        self.closed = True

    def put(self, packet):
        self.incoming.put_nowait(packet)


class FakeBidi:
    def __init__(self):
        self.events = asyncio.Queue(maxsize=32)
        self.received, self.results = [], []
        self.sending = asyncio.Event()
        self.send_gate = asyncio.Event()
        self.stall_input = False
        self.cancelled = self.closed = False

    async def send(self, packet):
        self.sending.set()
        if self.stall_input: await self.send_gate.wait()
        self.received.append(packet)

    async def receive(self):
        event = await self.events.get()
        if isinstance(event, Exception): raise event
        return event

    async def tool_result(self, call_id, result):
        self.results.append((call_id, result))

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True

    def emit(self, event):
        self.events.put_nowait(event)


class FakeProvider:
    mode = ProviderMode.LOCAL_FAKE
    def __init__(self):
        self.stream = FakeBidi()
        self.opened = asyncio.Event()
        self.open_gate = asyncio.Event()
        self.stall_open = False
        self.open_cancelled = False
        self.resumes = []

    async def open(self, lease, resume):
        self.opened.set()
        self.resumes.append(resume)
        if self.stall_open:
            try: await self.open_gate.wait()
            except asyncio.CancelledError:
                self.open_cancelled = True
                raise
        return self.stream


class StalledTool:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.gate = asyncio.Event()
        self.executions = []

    async def execute(self, access, request):
        self.executions.append(request.tool)
        access.require_tool(request.tool)
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ToolResult("success", {"synthetic": True})


def coordinator(h, provider, **changes):
    return c.VoiceCoordinator(h.sessions, provider, selected_mode=ProviderMode.LOCAL_FAKE,
        production=False, clock=h.clock, **changes)


async def launch(h=None, **changes):
    h = h or SessionSetup()
    provider, client = FakeProvider(), FakeClient()
    worker = coordinator(h, provider, **changes)
    task = asyncio.create_task(worker.run(TOKEN, client))
    await client.ready.wait()
    return h, provider, client, worker, task


async def finish(client, worker, task, reason="stopped"):
    if not task.done(): client.put(control("stop", worker.lease.connection_generation))
    result = await asyncio.wait_for(task, 2)
    assert result.reason == reason
    assert client.closed
    assert all(t.done() for t in worker.tasks)
    assert worker.input.count == worker.output.count == 0
    remaining = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert remaining == []
    return result


def test_current_sources_and_explicit_modes(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "src/campuspulse_recovery/voice"
    for mod, name in ((c, "coordinator.py"), (ports, "ports.py")):
        print("CURRENT_SOURCE_MODULE =", Path(mod.__file__).resolve())
        assert Path(mod.__file__).resolve() == root / name
    h, provider = SessionSetup(), FakeProvider()
    for selected, production in ((ProviderMode.LOCAL_FAKE, True), (ProviderMode.LOCAL_ADK_DEVELOPMENT, False),
                                 (ProviderMode.MANAGED_PRODUCTION, True)):
        with pytest.raises(ValueError):
            c.VoiceCoordinator(h.sessions, provider, selected_mode=selected, production=production)
    monkeypatch.setenv("K_SERVICE", "synthetic-cloud")
    with pytest.raises(ValueError): coordinator(h, provider)


def test_full_duplex_and_interruption_flushes_queued_and_late_old_audio():
    async def scenario():
        h, provider, client, worker, task = await launch()
        client.block_generation = 1
        provider.stream.emit(ResponseBoundary(1, "started"))
        provider.stream.emit(OutputAudio(1, b"\0"*960))
        await client.audio_blocked.wait()
        provider.stream.emit(OutputAudio(1, b"\0"*960))
        provider.stream.emit(OutputTranscript(1, "partial_old", "assistant", 0, False, "Незавершённый ответ"))
        client.put(p.encode_audio(p.AudioFrame("input", 1, 0, 0, b"\0"*640)))
        await settle(lambda: bool(provider.stream.received))
        assert not any(isinstance(x, p.AudioFrame) for x in client.sent)
        provider.stream.emit(ResponseBoundary(1, "interrupted"))
        provider.stream.emit(OutputAudio(1, b"\0"*960))  # provider late data retains old generation
        provider.stream.emit(ResponseBoundary(2, "started"))
        provider.stream.emit(OutputAudio(2, b"\0"*960))
        provider.stream.emit(OutputTranscript(2, "final_new", "assistant", 0, True, "Новый ответ"))
        provider.stream.emit(ResponseBoundary(2, "completed"))
        await settle(lambda: any(isinstance(x, p.Generation) and x.phase == "completed" for x in client.sent))
        assert client.audio_cancelled
        assert [x.generation for x in client.sent if isinstance(x, p.AudioFrame)] == [2]
        assert not any(isinstance(x, p.Transcript) and x.transcript_id == "partial_old" for x in client.sent)
        assert any(isinstance(x, p.Interrupted) and x.generation == 1 for x in client.sent)
        # Feed the actual sent stream into the independent shared protocol checker.
        check = p.ProtocolState(1)
        check.accept_client(p.parse_json(control("start"), client=True), now=0)
        for item in client.sent:
            if isinstance(item, p.AudioFrame): check.accept_audio(item, now=0)
            else: check.accept_server(item)
        assert not check.output_open
        result = await finish(client, worker, task)
        assert not result.cleanup_failed
        assert provider.stream.closed and provider.stream.cancelled
        assert len(worker.resume.history) == 1
        # New connection never replays packets/tools; counters and run are fresh.
        fresh = h.repository()
        lease, resume = await fresh.acquire(TOKEN)
        assert lease.connection_generation == 2 and resume.history == worker.resume.history
    run(scenario())


@pytest.mark.parametrize("terminal", ["stop", "disconnect", "cancel", "revoke", "expiry"])
def test_terminal_signals_cancel_tool_upstream_and_all_request_tasks(terminal):
    async def scenario():
        h = SessionSetup(credential_seconds=10 if terminal == "expiry" else 1200)
        tool = StalledTool()
        h, provider, client, worker, task = await launch(h, tools=tool)
        provider.stream.emit(ToolRequest("read_1", "read_calendar", {}))
        await tool.started.wait()
        if terminal == "stop": client.put(control("stop"))
        elif terminal == "disconnect": client.put(None)
        elif terminal == "cancel": task.cancel()
        else:
            if terminal == "revoke": h.replace_credential(revoked=True)
            h.clock.advance(10)
        if terminal == "cancel":
            with pytest.raises(asyncio.CancelledError): await task
            assert all(t.done() for t in worker.tasks)
        else:
            reason = {"stop": "stopped", "disconnect": "disconnected", "revoke": "revoked", "expiry": "expired"}[terminal]
            await settle(task.done)
            await finish(client, worker, task, reason)
        assert tool.cancelled.is_set()
        assert provider.stream.closed and provider.stream.cancelled
        assert provider.stream.results == []
        assert client.closed
    run(scenario())


def test_tool_has_fresh_guard_and_observable_success_without_real_resources():
    async def scenario():
        tool = StalledTool()
        _, provider, client, worker, task = await launch(tools=tool)
        provider.stream.emit(ToolRequest("read_1", "read_assignments", {}))
        await tool.started.wait()
        tool.gate.set()
        await settle(lambda: any(isinstance(x, p.ToolFinished) for x in client.sent))
        assert [x.status for x in client.sent if isinstance(x, p.ToolFinished)] == ["success"]
        assert provider.stream.results[0][0] == "read_1"
        await finish(client, worker, task)
    run(scenario())


@pytest.mark.parametrize("name", ["apply_plan", "verify_plan", "arbitrary"])
def test_forged_tool_is_rejected_before_handler(name):
    async def scenario():
        tool = StalledTool()
        _, provider, client, worker, task = await launch(tools=tool)
        provider.stream.emit(ToolRequest("forged", name, {"approved": True}))
        await settle(task.done)
        await finish(client, worker, task, "upstream_error")
        assert tool.executions == [] and provider.stream.results == []
    run(scenario())


def test_stalled_sender_fails_and_releases_instead_of_buffering_forever():
    async def scenario():
        _, provider, client, worker, task = await launch(limits=c.Limits(send_seconds=.02, close_seconds=.12))
        client.block_generation = 1
        provider.stream.emit(ResponseBoundary(1, "started"))
        provider.stream.emit(OutputAudio(1, b"\0"*960))
        await client.audio_blocked.wait()
        await asyncio.wait_for(asyncio.shield(task), 1)
        await finish(client, worker, task, "backpressure")
        assert client.audio_cancelled
    run(scenario())


@pytest.mark.parametrize("direction", ["input", "output", "events", "flood"])
def test_queue_byte_count_and_rate_budgets_are_bounded(direction):
    async def scenario():
        h, provider, client, worker, task = await launch()
        if direction == "input":
            provider.stream.stall_input = True
            for i in range(11):
                h.clock.advance(.02)
                client.put(p.encode_audio(p.AudioFrame("input", 1, 0, i, b"\0"*640)))
                await settle(lambda: worker.input.count >= min(i+1, 10) or worker.terminal.done())
                if worker.terminal.done(): break
        elif direction == "output":
            client.block_generation = 1
            provider.stream.emit(ResponseBoundary(1, "started"))
            provider.stream.emit(OutputAudio(1, b"\0"*1920))
            await client.audio_blocked.wait()
            for _ in range(5): provider.stream.emit(OutputAudio(1, b"\0"*1920))
        elif direction == "events":
            # The count budget is also enforced without any PCM allocation.
            buffer = c._Buffer(9600)
            for _ in range(32): buffer.put(("synthetic", {}))
            with pytest.raises(p.ProtocolError, match="backpressure"): buffer.put(("synthetic", {}))
            assert buffer.count == 32 and buffer.bytes == 0
            await finish(client, worker, task)
            return
        else:
            for i in range(11): client.put(p.encode_audio(p.AudioFrame("input", 1, 0, i, b"\0"*640)))
        await settle(task.done)
        await finish(client, worker, task, "protocol_error" if direction == "flood" else "backpressure")
        assert worker.input.bytes == worker.output.bytes == 0
    run(scenario())


def test_upstream_failure_is_sanitized_and_cancels_work(caplog):
    async def scenario():
        _, provider, client, worker, task = await launch()
        provider.stream.emit(RuntimeError("synthetic-sensitive-upstream-content"))
        await settle(task.done)
        await finish(client, worker, task, "upstream_error")
        assert "synthetic-sensitive" not in repr(client.sent) + caplog.text
    run(scenario())


def test_long_session_uses_own_clock_and_does_not_change_text_timeout():
    async def scenario():
        h, provider, client, worker, task = await launch()
        for i in range(6):
            client.put(control("heartbeat"))
            client.put(p.encode_audio(p.AudioFrame("input", 1, 0, i, b"\0"*640)))
            await settle(lambda: len(provider.stream.received) >= i+1)
            h.clock.advance(10)
            await settle(lambda: worker.lease.lease_until > h.clock.value)
        assert h.clock.mono == 60 and not task.done()
        assert worker.lease.deadline <= h.clock.value + timedelta(seconds=480)
        await finish(client, worker, task)
    run(scenario())


@pytest.mark.parametrize("timeout", ["heartbeat", "idle", "turn", "session"])
def test_own_deadlines_do_not_get_extended_by_heartbeat(timeout):
    async def scenario():
        h, provider, client, worker, task = await launch()
        if timeout == "turn": provider.stream.emit(ResponseBoundary(1, "started"))
        await settle(lambda: bool(h.clock.sleepers))
        total = {"heartbeat": 30, "idle": 120, "turn": 60, "session": 540}[timeout]
        for elapsed in range(10, total+1, 10):
            if timeout != "heartbeat":
                client.put(control("heartbeat"))
                if timeout == "session":
                    # Keep activity alive while proving the hard total cap.
                    client.put(control("mute", muted=bool((elapsed//10) % 2)))
                await settle(lambda: worker.last_packet == h.clock.monotonic())
            h.clock.advance(10)
            await settle(lambda: task.done() or (h.clock.sleepers and worker.lease.lease_until > h.clock.value))
        await settle(task.done)
        await finish(client, worker, task, "expired" if timeout == "session" else "timeout")
    run(scenario())


def test_disconnect_or_stop_during_provider_open_cancels_open():
    async def scenario():
        h, provider, client = SessionSetup(), FakeProvider(), FakeClient()
        provider.stall_open = True
        worker = coordinator(h, provider)
        task = asyncio.create_task(worker.run(TOKEN, client))
        await provider.opened.wait()
        client.put(control("stop"))
        await finish(client, worker, task)
        assert provider.open_cancelled
    run(scenario())


def test_cancellation_during_cleanup_is_joined_and_bounded_close_failure_visible():
    async def scenario():
        _, provider, client, worker, task = await launch(limits=c.Limits(close_seconds=.12))
        entered, gate = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await gate.wait()
        provider.stream.close = close
        client.put(control("stop"))
        await entered.wait()
        task.cancel()
        # close is cancelled by its bounded cleanup budget; no detached worker.
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 1)
        assert worker.cleanup_failed and all(t.done() for t in worker.tasks)
        assert client.closed
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []
    run(scenario())


def test_child_suppressing_first_cancellation_is_recancelled_and_joined():
    async def scenario():
        _, provider, client, worker, task = await launch(limits=c.Limits(close_seconds=.04))
        waiting, first_cancel, second_cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def resistant_read():
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancel.set()
                try: await asyncio.Event().wait()
                finally: second_cancel.set()
        provider.stream.receive = resistant_read
        # Release the already pending old receive so the next read uses this method.
        provider.stream.emit(ResumeUpdate("synthetic-transport", worker.lease.deadline))
        await waiting.wait()
        client.put(control("stop"))
        result = await asyncio.wait_for(task, 1)
        assert result.cleanup_failed  # cancellation contract violation is visible
        assert first_cancel.is_set() and second_cancel.is_set()
        assert all(t.done() for t in worker.tasks) and client.closed
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []
    run(scenario())


def test_metadata_flood_and_past_lease_cannot_continue_streaming():
    async def scenario():
        h, provider, client, worker, task = await launch()
        # An expired process-local lease cannot emit packets after another holder
        # could take over, even before its delayed heartbeat is scheduled.
        h.clock.advance(16)
        provider.stream.emit(ResponseBoundary(1, "started"))
        provider.stream.emit(OutputAudio(1, b"\0"*960))
        await settle(task.done)
        await finish(client, worker, task, "revoked")
        assert not any(isinstance(x, p.AudioFrame) for x in client.sent)
        h, provider, client, worker, task = await launch()
        class Flood:
            async def receive(self):
                return ResumeUpdate("synthetic-transport", worker.lease.deadline)
        provider.stream.receive = Flood().receive
        provider.stream.emit(ResumeUpdate("synthetic-transport", worker.lease.deadline))
        await settle(task.done)
        await finish(client, worker, task, "protocol_error")
    run(scenario())


def test_reconnect_opens_fresh_transport_and_rejects_old_connection_packets():
    async def scenario():
        h, provider, client, worker, task = await launch()
        provider.stream.emit(ResumeUpdate("synthetic-resumption", worker.lease.deadline))
        provider.stream.emit(OutputTranscript(0, "user_final", "user", 0, True, "Учебный вопрос"))
        await settle(lambda: len(worker.resume.history) == 1)
        await finish(client, worker, task)
        _, next_provider, next_client, next_worker, next_task = await launch(h)
        assert next_worker.lease.connection_generation == 2
        assert next_provider.resumes[0].history == worker.resume.history
        assert next_provider.resumes[0].transport_token == "synthetic-resumption"
        assert next_provider.stream.received == next_provider.stream.results == []
        assert [type(x) for x in next_client.sent] == [p.Ready]
        next_client.put(p.encode_audio(p.AudioFrame("input", 1, 0, 0, b"\0"*640)))
        await settle(next_task.done)
        await finish(next_client, next_worker, next_task, "protocol_error")
        assert next_provider.stream.received == []
    run(scenario())


def test_revoked_context_cannot_reopen_and_delayed_start_rechecks_authority():
    async def scenario():
        h, provider, client = SessionSetup(), FakeProvider(), FakeClient()
        client.incoming.get_nowait()  # delay user start after durable acquisition
        worker = coordinator(h, provider)
        task = asyncio.create_task(worker.run(TOKEN, client))
        await settle(lambda: worker.lease is not None)
        h.replace_parent(revoked=True)
        client.put(control("start"))
        await settle(task.done)
        await finish(client, worker, task, "revoked")
        assert not provider.opened.is_set()
        # Even a stored resumption token is not permission to reopen.
        next_client = FakeClient()
        next_provider = FakeProvider()
        next_worker = coordinator(h, next_provider)
        next_task = asyncio.create_task(next_worker.run(TOKEN, next_client))
        await settle(next_task.done)
        await finish(next_client, next_worker, next_task, "revoked")
        assert not next_provider.opened.is_set()
    run(scenario())


def test_resume_does_not_reexecute_retired_tool_call_after_crash():
    async def scenario():
        tool = StalledTool()
        h, provider, client, worker, task = await launch(tools=tool)
        provider.stream.emit(ToolRequest("read_before_crash", "read_calendar", {}))
        await tool.started.wait()
        # The durable record already retires the ID before the read is started.
        from campuspulse_recovery.storage.plans import _decode
        from campuspulse_recovery.voice.sessions import _Claim, _path
        claim = _decode(h.documents.data[_path(worker.lease.device_id)], "voice_stream", _Claim)
        assert claim.resume.seen_call_ids == ("read_before_crash",)
        client.put(None)
        await settle(task.done)
        await finish(client, worker, task, "disconnected")
        next_tool = StalledTool()
        _, next_provider, next_client, next_worker, next_task = await launch(h, tools=next_tool)
        next_provider.stream.emit(ToolRequest("read_before_crash", "read_calendar", {}))
        await settle(next_task.done)
        await finish(next_client, next_worker, next_task, "protocol_error")
        assert next_tool.executions == []
    run(scenario())


class ControlledDocuments(FakeDocuments):
    """Real repository callbacks; gates model store transport/ack delays.

    Wait outside the fake's commit lock so a second repository can take over.
    No replacement auth result or repository/endpoint mock is supplied.
    """
    def __init__(self):
        super().__init__()
        self.pause_renew = False
        self.renew_started, self.renew_gate = asyncio.Event(), asyncio.Event()
        self.renew_cancelled = asyncio.Event()
        self.check_count, self.pause_check = 0, None
        self.check_started, self.check_gate = asyncio.Event(), asyncio.Event()
        self.on_check_ack = lambda: None

    async def transact(self, callback):
        if self.pause_renew and callback.__name__ == "renew":
            self.renew_started.set()
            try:
                await self.renew_gate.wait()
            except asyncio.CancelledError:
                self.renew_cancelled.set()
                raise
        result = await super().transact(callback)
        if callback.__name__ == "check":
            self.check_count += 1
            if self.check_count == self.pause_check:
                self.check_started.set()
                await self.check_gate.wait()
                # Deliver the terminal/expiry signal at the awaited ack boundary,
                # before the caller can dispatch using this earlier snapshot.
                self.on_check_ack()
        return result


@pytest.mark.parametrize("elapsed,reason", [(16, "revoked"), (541, "expired")])
def test_queued_duplex_is_fenced_during_stalled_renewal_and_successor_takeover(elapsed, reason):
    async def scenario():
        h = SessionSetup(ControlledDocuments())
        tool = StalledTool()
        h, provider, client, worker, task = await launch(h, tools=tool)
        input_dispatches, output_dispatches = [], []
        original_input, original_output = provider.stream.send, client.send

        async def record_input(packet):
            input_dispatches.append((packet.connection_generation, h.clock.monotonic()))
            await original_input(packet)

        async def record_output(packet):
            if isinstance(packet, p.AudioFrame):
                output_dispatches.append((packet.connection_generation, h.clock.monotonic()))
            await original_output(packet)

        provider.stream.send, client.send = record_input, record_output
        provider.stream.stall_input = True
        client.block_generation = 1
        for sequence in range(2):
            client.put(p.encode_audio(p.AudioFrame("input", 1, 0, sequence, b"\0"*640)))
        provider.stream.emit(ResponseBoundary(1, "started"))
        for _ in range(2): provider.stream.emit(OutputAudio(1, b"\0"*960))
        await provider.stream.sending.wait()
        await client.audio_blocked.wait()
        await settle(lambda: worker.input.count == worker.output.count == 2)
        assert input_dispatches == output_dispatches == [(1, 0)]

        h.documents.pause_renew = True
        h.clock.advance(10)
        await h.documents.renew_started.wait()
        # Tool authorization is also queued behind the old renewal's local lock.
        provider.stream.emit(ToolRequest("queued_read", "read_calendar", {}))
        await settle(lambda: provider.stream.events.empty())
        h.clock.advance(elapsed - 10)
        successor_store = h.repository()
        successor, _ = await successor_store.acquire(TOKEN)
        assert successor.connection_generation == 2
        assert successor.holder_id != worker.lease.holder_id
        assert not task.done() and not worker.terminal.done()

        # Finish the sends ALREADY dispatched at t=0, releasing their successors
        # from the real input/output queues at t=16/541. New sends must be fenced.
        provider.stream.send_gate.set()
        client.audio_gate.set()
        await settle(task.done)
        await finish(client, worker, task, reason)
        assert input_dispatches == [(1, 0)]
        assert output_dispatches == [(1, 0)]
        assert tool.executions == [] and provider.stream.results == []
        assert h.documents.renew_cancelled.is_set()
        await successor_store.check(successor)
    run(scenario())


@pytest.mark.parametrize("boundary", ["stop", "lease", "deadline"])
def test_authorization_waiting_on_lock_cannot_return_access_or_dispatch_after_terminal(boundary):
    async def scenario():
        h, provider, client, worker, task = await launch()
        entered = asyncio.Event()
        dispatched = []
        await worker.authority_lock.acquire()

        async def adapter_call():
            entered.set()
            access = await worker.authorize_tool("read_calendar")
            # Simulate an SDK hook consumer. Returned access must never restart
            # a read after the boundary; this task is explicitly joined below.
            dispatched.append(access.device_id)

        authorization = asyncio.create_task(adapter_call())
        await entered.wait()  # caller is suspended on the held authority lock
        if boundary == "stop": worker._end("stopped")
        else: h.clock.advance(16 if boundary == "lease" else 541)
        worker.authority_lock.release()
        from campuspulse_recovery.security.context import AccessDenied
        with pytest.raises(AccessDenied): await authorization
        await settle(task.done)
        await finish(client, worker, task, {"stop": "stopped", "lease": "revoked", "deadline": "expired"}[boundary])
        assert dispatched == [] and provider.stream.results == []
    run(scenario())


@pytest.mark.parametrize("dispatch_check", [2, 3])
@pytest.mark.parametrize("boundary", ["stop", "lease", "deadline"])
def test_awaited_durable_check_cannot_restart_tool_or_result_dispatch(dispatch_check, boundary):
    async def scenario():
        h = SessionSetup(ControlledDocuments())
        executions = []

        class ImmediateRead:
            async def execute(self, access, request):
                access.require_tool(request.tool)
                executions.append((request.call_id, h.clock.monotonic()))
                return ToolResult("success", {"synthetic": True})

        h, provider, client, worker, task = await launch(h, tools=ImmediateRead())
        h.documents.check_count = 0
        # 1: incoming ToolRequest guard; 2: immediately before actual handler;
        # 3: immediately before returning the read result to upstream.
        h.documents.pause_check = dispatch_check
        if boundary == "stop": h.documents.on_check_ack = lambda: worker._end("stopped")
        else: h.documents.on_check_ack = lambda: h.clock.advance(16 if boundary == "lease" else 541)
        provider.stream.emit(ToolRequest("awaited_read", "read_calendar", {}))
        await h.documents.check_started.wait()
        assert executions == ([] if dispatch_check == 2 else [("awaited_read", 0)])
        h.documents.check_gate.set()
        await settle(task.done)
        await finish(client, worker, task, {"stop": "stopped", "lease": "revoked", "deadline": "expired"}[boundary])
        assert executions == ([] if dispatch_check == 2 else [("awaited_read", 0)])
        assert provider.stream.results == []
    run(scenario())


@pytest.mark.parametrize("pending_count", [1, 4])
@pytest.mark.parametrize("close_fails", [False, True])
def test_stop_after_delivered_tool_starts_obeys_closing_protocol(pending_count, close_fails):
    async def scenario():
        cancelled = set()

        class PendingReads(StalledTool):
            async def execute(self, access, request):
                try:
                    return await super().execute(access, request)
                except asyncio.CancelledError:
                    cancelled.add(request.call_id)
                    raise

        tools = PendingReads()
        h, provider, client, worker, task = await launch(tools=tools)
        call_ids = {"pending_read_" + str(i) for i in range(pending_count)}
        for call_id in sorted(call_ids):
            provider.stream.emit(ToolRequest(call_id, "read_calendar", {}))
        # Admission/handler entry alone did not expose this bug. The starts must
        # actually be delivered before the client Stop changes phase to CLOSING.
        await settle(lambda: len([x for x in client.sent if isinstance(x, p.ToolStarted)]) == pending_count
                     and len(tools.executions) == pending_count)
        assert worker.state.phase == p.Phase.ACTIVE
        if close_fails:
            async def fail_close():
                raise RuntimeError("synthetic_close_failure")
            provider.stream.close = fail_close
        client.put(control("stop", worker.lease.connection_generation))
        result = await asyncio.wait_for(asyncio.shield(task), 2)
        await finish(client, worker, task)
        assert result.cleanup_failed is close_fails
        assert cancelled == call_ids and provider.stream.cancelled
        assert provider.stream.closed is not close_fails
        assert provider.stream.results == []
        assert [type(x) for x in client.sent] == [p.Ready] + [p.ToolStarted]*pending_count + [p.Closed]
        assert client.sent[-1].reason == "stopped"
        assert worker.state.phase == p.Phase.CLOSED

        # Independent W02 checker accepts the actual delivered terminal trace;
        # no forbidden ToolFinished, fabricated success or late output is sent.
        state = p.ProtocolState(worker.lease.connection_generation)
        state.accept_client(p.parse_json(control("start"), client=True), now=0)
        for packet in client.sent:
            if isinstance(packet, p.Closed):
                state.accept_client(p.parse_json(control("stop", worker.lease.connection_generation), client=True), now=0)
            state.accept_server(packet)
        assert state.phase == p.Phase.CLOSED
        from campuspulse_recovery.storage.plans import _decode
        from campuspulse_recovery.voice.sessions import _Claim, _path
        claim = _decode(h.documents.data[_path(worker.lease.device_id)], "voice_stream", _Claim)
        assert claim.released and set(claim.resume.seen_call_ids) == call_ids
        successor, resume = await h.repository().acquire(TOKEN)
        assert successor.connection_generation == 2 and set(resume.seen_call_ids) == call_ids
    run(scenario())


def test_actual_sdk_stop_after_delivered_start_keeps_protocol_and_retired_id(monkeypatch):
    # This W04 companion uses W05's existing SDK/network doubles unchanged. It
    # does not replace/modify the separately executed W05 regression, whose old
    # hard assertions currently require the defect itself to remain present.
    pytest.importorskip("google.adk")
    from tests.voice import test_live_agent as sdk
    import google.adk.runners as sdk_runner
    from campuspulse_recovery.storage.plans import _decode
    from campuspulse_recovery.voice.sessions import _Claim, _path

    root = Path(__file__).resolve().parents[2]
    for mod, path in ((sdk.a, "src/campuspulse_recovery/agent/voice_agent.py"),
                      (sdk.v, "src/campuspulse_recovery/agent/voice_policy.py"),
                      (sdk, "tests/voice/test_live_agent.py")):
        assert Path(mod.__file__).resolve() == root / path
        print("CURRENT_SDK_INTEGRATION_SOURCE =", Path(mod.__file__).resolve())
    print("ACTUAL_ADK_RUNNER_MODULE =", Path(sdk_runner.__file__).resolve())

    async def scenario():
        h, base, _ = await sdk.setup_policy()
        await h.sessions.release(base.lease)
        mcp = sdk.McpSession(base)
        mcp.gate = asyncio.Event()
        sdk.patch_discovery(monkeypatch, mcp)
        call_id = "cancelled_stop_read"

        async def connection_run(replay=False):
            connection = sdk.Connection()
            def policy_factory(lease):
                return sdk.v.VoicePolicy(sessions=h.sessions, lease=lease, request_scope=base.request_scope,
                    constraints=base.constraints, now=base.now, lease_provider=lambda: worker.lease)
            provider = sdk.a.LocalAdkProvider(sdk.config(), policy_factory=policy_factory,
                                             model=sdk.FakeLiveModel(connection))
            worker = c.VoiceCoordinator(h.sessions, provider, selected_mode=ProviderMode.LOCAL_ADK_DEVELOPMENT,
                production=False, tools=provider, clock=h.clock)
            client = FakeClient()
            task = asyncio.create_task(worker.run(TOKEN, client))
            await client.ready.wait()
            connection.put(sdk.fc(("read_assignments", call_id,
                {"horizon": base.constraints.horizon.model_dump(mode="json")})))
            if replay:
                await settle(task.done)
                await finish(client, worker, task, "protocol_error")
            else:
                await mcp.entered.wait()
                await settle(lambda: any(isinstance(x, p.ToolStarted) for x in client.sent))
                client.put(control("stop", worker.lease.connection_generation))
                outcome = await asyncio.wait_for(asyncio.shield(task), 2)
                await finish(client, worker, task)
                assert not outcome.cleanup_failed and mcp.cancelled
                assert [type(x) for x in client.sent] == [p.Ready, p.ToolStarted, p.Closed]
            assert connection.closed and provider.session.bridge.pending == {}
            claim = _decode(h.documents.data[_path(worker.lease.device_id)], "voice_stream", _Claim)
            assert claim.released and call_id in claim.resume.seen_call_ids

        await connection_run()
        assert len(mcp.calls) == 1
        mcp.gate = None
        await connection_run(replay=True)
        assert len(mcp.calls) == 1  # retired cancelled read cannot execute twice
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    run(scenario())
