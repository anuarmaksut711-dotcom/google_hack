"""W05 network-denied policy tests against current workspace modules."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest
from mcp import types as mt
from google.adk.tools.mcp_tool.mcp_tool import McpTool

from campuspulse_recovery.agent import voice_policy as v
from campuspulse_recovery.domain.models import PlanningConstraints, ReadAssignmentsInput
from campuspulse_recovery.security.context import AuthenticatedCaller, RequestContextFactory, AccessDenied
from campuspulse_recovery.voice.ports import ToolRequest, ToolResult
from campuspulse_recovery.voice.sessions import ResumeState
from tests.storage.test_plans import NOW, interval, sources, run
from tests.voice.test_sessions import SessionSetup, TOKEN


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*a, **k): raise AssertionError("W05 network denied")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


async def setup_policy():
    h = SessionSetup()
    lease, resume = await h.sessions.acquire(TOKEN)
    @contextmanager
    def scope():
        b = lease.binding
        caller = AuthenticatedCaller(principal_id=h.config.identities.backend_service_account,
            session_id=b.session_id, run_id=b.run_id, authenticated_at=NOW, expires_at=b.expires_at)
        factory = RequestContextFactory(h.policy, boundary="agent", clock=h.clock,
            authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
            bindings=SimpleNamespace(resolve=lambda **k: b))
        with factory.request(object()) as context:
            yield context
    constraints = PlanningConstraints(config_revision=h.config.config_revision,
        timezone=h.config.student.timezone, horizon=interval(hours=72), working_windows=(interval(hours=5),))
    policy = v.VoicePolicy(sessions=h.sessions, lease=lease, request_scope=scope, constraints=constraints, now=NOW)
    return h, policy, resume


def snapshot(policy, name):
    a, c = sources(policy.resources.config)
    value = a if name == "read_assignments" else c
    body = value.model_dump(mode="json")
    body["source"]["reference"]["run_id"] = policy.lease.binding.run_id
    return body


class McpSession:
    def __init__(self, policy):
        self.policy, self.calls = policy, []
        self.gate = None
        self.entered = asyncio.Event()
        self.cancelled = False
        self.error = False
    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        self.entered.set()
        try:
            if self.gate: await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error: raise RuntimeError("synthetic-private-source-and-token")
        return mt.CallToolResult(content=[], structuredContent=snapshot(self.policy, name))


def remote_tools(policy, session):
    async def create_session(**kwargs): return session
    manager = SimpleNamespace(create_session=create_session, _get_session_context=lambda **k: None)
    return [McpTool(mcp_tool=mt.Tool(name=name, description="Read configured resource",
        inputSchema=ReadAssignmentsInput.model_json_schema()), mcp_session_manager=manager) for name in v.READS]


def context(policy, call_id="call_1"):
    return SimpleNamespace(function_call_id=call_id, user_id=policy.lease.binding.owner_id,
                          session=SimpleNamespace(id=policy.lease.session_id))


async def durable_dispatch(h, policy, bridge, request, resume):
    """Test-only coordinator equivalent; main live tests use actual W04."""
    if request.call_id in resume.seen_call_ids:
        raise AccessDenied("retired")
    resume = resume.changed(seen_call_ids=(*resume.seen_call_ids, request.call_id))
    await h.sessions.checkpoint(policy.lease, resume)
    result = await bridge.execute(await h.sessions.check(policy.lease, tool=request.tool), request)
    await bridge.tool_result(request.call_id, result)
    return resume


def test_current_source_and_policy_authority_not_serializable():
    import pickle
    expected = Path(__file__).resolve().parents[2] / "src/campuspulse_recovery/agent/voice_policy.py"
    print("W05_POLICY_SOURCE=", v.__file__)
    assert Path(v.__file__).resolve() == expected
    async def scenario():
        h, policy, _ = await setup_policy()
        await policy.authorize()
        with pytest.raises(TypeError): pickle.dumps(policy)
        h.clock.advance(16)
        with pytest.raises(AccessDenied): await policy.authorize()
    run(scenario())


@pytest.mark.parametrize("name,args", [
    ("apply_plan", {}), ("verify_plan", {}), ("unknown", {}),
    ("read_calendar", {"calendar_id": "spoof"}),
    ("read_assignments", {"horizon": {"start": "invalid"}}),
    ("solve_repair", {"request": {"approved": True}}),
    ("solve_repair", {"request": {}, "approved": True}),
    ("read_calendar", {"horizon": {"start": float("nan")}}),
])
def test_malformed_write_unknown_resource_arguments_never_dispatch(name, args):
    async def scenario():
        _, policy, _ = await setup_policy()
        emitted = []
        async def emit(x): emitted.append(x)
        bridge = v.LiveToolBridge(policy, emit)
        result = await bridge.call(name, args, context(policy))
        assert result["status"] == "blocked" and emitted == []
    run(scenario())


def test_guarded_real_mcp_impl_sanitizes_injected_source_and_preserves_envelope():
    async def scenario():
        h, policy, resume = await setup_policy()
        session = McpSession(policy)
        policy.remote = {t.name: t for t in remote_tools(policy, session)}
        queue = asyncio.Queue()
        bridge = v.LiveToolBridge(policy, queue.put)
        args = {"horizon": policy.constraints.horizon.model_dump(mode="json")}
        task = asyncio.create_task(bridge.call("read_assignments", args, context(policy)))
        request = await queue.get()
        resume = await durable_dispatch(h, policy, bridge, request, resume)
        result = await task
        assert len(session.calls) == 1 and result["complete"]
        assert "Untrusted" not in str(result) and "ignore approval" not in str(result)
        assert "read_at" in result and result["timezone"] == "Asia/Qyzylorda"
        assert "не сохранена в Sheets" in result["speech"]
        assert "2026-10-18T02:00:00+05:00" in result["speech"]
        assert (await bridge.call("read_assignments", args, context(policy)))["status"] == "blocked"
        assert resume.seen_call_ids == ("call_1",)
        assert policy.observations[-1]["operation_ids"] == []
    run(scenario())


@pytest.mark.parametrize("mutation", ["resource", "stale", "future", "incomplete", "unavailable", "unknown_work"])
def test_untrusted_or_unusable_source(mutation):
    async def scenario():
        h, policy, _ = await setup_policy()
        body = snapshot(policy, "read_assignments")
        if mutation == "resource": body["source"]["reference"]["resource_id"] = "another-sheet"
        if mutation == "stale": body["source"]["reference"]["read_at"] = (NOW - timedelta(minutes=3)).isoformat()
        if mutation == "future": body["source"]["reference"]["read_at"] = (NOW + timedelta(seconds=1)).isoformat()
        if mutation == "incomplete":
            body["source"]["complete"] = False
            body["source"]["issues"] = [{"code": "source_partial"}]
            result = policy.source("read_assignments", {"structuredContent": body})
            assert result["status"] == "incomplete" and "неполны" in policy.speech()
            return
        if mutation == "unknown_work":
            from campuspulse_recovery.domain.models import Assignment, assignments_content_hash
            body["assignments"][0]["remaining_minutes"] = None
            values = tuple(Assignment.model_validate(x) for x in body["assignments"])
            body["source"]["reference"]["content_hash"] = assignments_content_hash(values)
            policy.source("read_assignments", {"structuredContent": body})
            assert "Уточните" in policy.speech() and "Прошедший блок" in policy.speech()
            return
        with pytest.raises((ValueError, AccessDenied)):
            policy.source("read_assignments", {"structuredContent": body, "isError": mutation == "unavailable"})
    run(scenario())


def test_solver_authoritative_snapshots_and_deadline_capacity():
    async def scenario():
        _, policy, _ = await setup_policy()
        with pytest.raises(ValueError): policy.solve()
        policy.source("read_assignments", {"structuredContent": snapshot(policy, "read_assignments")})
        policy.source("read_calendar", {"structuredContent": snapshot(policy, "read_calendar")})
        result = policy.solve()
        assert result["saved"] is False and result["status"] in ("feasible", "optimal", "no_change")
        assert "72 часа" in result["speech"]
        policy.constraints = PlanningConstraints.model_validate(policy.constraints.model_dump() | {"working_windows": ()})
        result = policy.solve()
        assert result["status"] == "infeasible" and "дефицит" in result["speech"]
    run(scenario())


def test_expiry_during_read_discards_result_and_transport_error_is_safe():
    async def scenario():
        h, policy, resume = await setup_policy()
        session = McpSession(policy); session.gate = asyncio.Event()
        policy.remote = {t.name: t for t in remote_tools(policy, session)}
        queue = asyncio.Queue(); bridge = v.LiveToolBridge(policy, queue.put)
        args = {"horizon": policy.constraints.horizon.model_dump(mode="json")}
        caller = asyncio.create_task(bridge.call("read_assignments", args, context(policy)))
        request = await queue.get()
        worker = asyncio.create_task(bridge.execute(await policy.authorize(request.tool), request))
        await session.entered.wait()
        h.clock.advance(16); session.gate.set()
        result = await worker
        assert result.status == "unavailable" and policy.assignments is None
        await bridge.tool_result(request.call_id, result)
        assert (await caller)["status"] == "blocked"
        assert "synthetic-private" not in str(result.value)
    run(scenario())


def test_remote_failure_is_safe_single_attempt_and_new_horizon_needs_server_basis():
    async def scenario():
        h, policy, resume = await setup_policy()
        session = McpSession(policy); session.error = True
        policy.remote = {t.name: t for t in remote_tools(policy, session)}
        queue = asyncio.Queue(); bridge = v.LiveToolBridge(policy, queue.put)
        task = asyncio.create_task(bridge.call("read_assignments", {"horizon": policy.constraints.horizon.model_dump(mode="json")}, context(policy)))
        request = await queue.get()
        await durable_dispatch(h, policy, bridge, request, resume)
        result = await task
        assert len(session.calls) == 1 and result["status"] == "blocked"
        assert "synthetic-private" not in str(result)
        assert policy.assignments is None
        h.clock.advance(1)
        with pytest.raises(ValueError, match="refresh_server_planning_basis"):
            policy.solve()
    run(scenario())


def delayed_first_check(monkeypatch, policy):
    """Delay before actual durable validation; never fake its result."""
    original = policy.sessions.check
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []
    async def check(lease, *, tool=None):
        attempts.append(lease)
        if len(attempts) == 1:
            entered.set()
            await release.wait()
        return await original(lease, tool=tool)
    monkeypatch.setattr(policy.sessions, "check", check)
    return entered, release, attempts


def test_actual_durable_check_racing_same_holder_renewal_revalidates(monkeypatch):
    async def scenario():
        h, policy, _ = await setup_policy()
        current = [policy.lease]
        policy.lease_provider = lambda: current[0]
        entered, release, attempts = delayed_first_check(monkeypatch, policy)
        task = asyncio.create_task(policy.authorize("read_assignments"))
        await entered.wait()
        h.clock.advance(1)
        current[0] = await h.sessions.renew(current[0])
        release.set()
        access = await task
        assert attempts == [policy._lease, current[0]]
        assert access == await h.repository().check(current[0], tool="read_assignments")
        assert current[0].lease_until > policy._lease.lease_until
    run(scenario())


@pytest.mark.parametrize("change", ["successor", "revoke", "expiry", "config", "unrelated_denial"])
def test_renewal_retry_never_grants_changed_or_denied_authority(monkeypatch, change):
    async def scenario():
        h, policy, _ = await setup_policy()
        current = [policy.lease]; policy.lease_provider = lambda: current[0]
        entered, release, attempts = delayed_first_check(monkeypatch, policy)
        task = asyncio.create_task(policy.authorize("read_calendar"))
        await entered.wait()
        h.clock.advance(1)
        current[0] = await h.sessions.renew(current[0])
        if change == "successor":
            h.clock.advance(16)
            current[0], _ = await h.sessions.acquire(TOKEN)
        elif change == "revoke": h.replace_credential(revoked=True)
        elif change == "expiry": h.clock.advance(1200)
        elif change == "config":
            raw = h.config.model_dump()
            raw["student"]["calendar_id"] = "changed-calendar"
            h.policy._config = type(h.config).model_validate(raw)
        else:
            original = h.sessions._current
            async def denied(*args):
                await original(*args)
                raise AccessDenied("independent_policy_denial")
            monkeypatch.setattr(h.sessions, "_current", denied)
        release.set()
        with pytest.raises(AccessDenied): await task
        # The durable check may retry only an exact stale-lease mismatch.
        assert len(attempts) <= 2
        if change in ("revoke", "expiry", "config", "successor"):
            assert len(attempts) == 1
    run(scenario())


@pytest.mark.parametrize("change", ["revoke", "expiry", "config", "successor"])
def test_fresh_retry_fully_rechecks_durable_authority(monkeypatch, change):
    async def scenario():
        h, policy, _ = await setup_policy()
        current = [policy.lease]; policy.lease_provider = lambda: current[0]
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        attempts = []
        original = h.sessions.check
        async def delayed(lease, *, tool=None):
            index = len(attempts); attempts.append(lease)
            assert index < 2
            entered[index].set(); await release[index].wait()
            return await original(lease, tool=tool)
        monkeypatch.setattr(h.sessions, "check", delayed)
        task = asyncio.create_task(policy.authorize("read_assignments"))
        await entered[0].wait()
        h.clock.advance(1); current[0] = await h.sessions.renew(current[0])
        release[0].set()
        await entered[1].wait()
        if change == "revoke": h.replace_credential(revoked=True)
        elif change == "expiry": h.clock.advance(1200)
        elif change == "config":
            raw = h.config.model_dump(); raw["student"]["calendar_id"] = "changed-calendar"
            h.policy._config = type(h.config).model_validate(raw)
        else:
            h.clock.advance(16); current[0], _ = await h.sessions.acquire(TOKEN)
        release[1].set()
        with pytest.raises(AccessDenied): await task
        assert len(attempts) == 2
    run(scenario())


def test_authorization_retry_attempt_and_walltime_bounds(monkeypatch):
    async def scenario():
        h, policy, _ = await setup_policy()
        current = [policy.lease]; policy.lease_provider = lambda: current[0]
        original = h.sessions.check
        attempts = []
        async def repeatedly_renew(lease, *, tool=None):
            attempts.append(lease)
            h.clock.advance(1)
            current[0] = await h.sessions.renew(current[0])
            return await original(lease, tool=tool)
        monkeypatch.setattr(h.sessions, "check", repeatedly_renew)
        with pytest.raises(AccessDenied): await policy.authorize("read_assignments")
        assert len(attempts) == 2
        entered = asyncio.Event(); cancelled = []
        async def stalled(*args, **kwargs):
            entered.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        monkeypatch.setattr(h.sessions, "check", stalled)
        monkeypatch.setattr(v, "AUTHORIZATION_SECONDS", 0.002)
        with pytest.raises(TimeoutError): await policy.authorize("read_assignments")
        assert entered.is_set() and cancelled == [True]
    run(scenario())


def test_deadline_expiring_after_successful_durable_read_cannot_authorize(monkeypatch):
    async def scenario():
        h, policy, _ = await setup_policy()
        original = h.sessions.check
        attempts = []
        async def late_result(lease, *, tool=None):
            access = await original(lease, tool=tool)
            attempts.append(lease)
            h.clock.advance(1200)
            return access
        monkeypatch.setattr(h.sessions, "check", late_result)
        with pytest.raises(AccessDenied): await policy.authorize("read_calendar")
        assert len(attempts) == 1
    run(scenario())
