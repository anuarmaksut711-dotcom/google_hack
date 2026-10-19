"""T19 fault injection: actual repositories/preflight/readers/writer + fake HTTP.

Synthetic authenticated contexts/consent and serialized fake documents are NOT
live consent, distributed isolation or runtime stop proof. No network allowed.
"""

import asyncio
from contextlib import contextmanager
from copy import copy, deepcopy
from datetime import timedelta
import socket
from types import SimpleNamespace

import pytest

from campuspulse_recovery.application.apply import ApplyService
from campuspulse_recovery.application.reconcile import changed
from campuspulse_recovery.domain.models import ApplyResult, PlanPayload, PlanVersion, SolveRepairInput
from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.integrations import google_clients
from campuspulse_recovery.integrations.calendar_write import CalendarAdapter
from campuspulse_recovery.integrations.google_clients import GoogleWorkspaceClients
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from campuspulse_recovery.storage.leases import WriterStoppedEvidence, _Claim, _claim_path
from campuspulse_recovery.storage.operations import _Ledger, _request_path
from campuspulse_recovery.storage.plans import _decode, _encode, plan_reference
from tests.application.test_preflight import Setup, shared_clock_request
from tests.integrations.test_calendar_write import FakeAPI
from tests.integrations.test_calendar_read import event
from tests.storage.test_apply_coordination import FakeStopGuard
from tests.storage.test_plans import NOW, run


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("T19 forbids network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Harness(Setup):
    def __init__(self, monkeypatch):
        super().__init__()
        self.api = FakeAPI()
        self.bootstrapping = True
        monkeypatch.setattr(google_clients, "create_workspace_credentials", lambda *a, **k: None)
        monkeypatch.setattr(google_clients, "_RequestsHttp", lambda *a, **k: self.api)
        self.clients = GoogleWorkspaceClients(self.policy, environment="local")
        self.writer = CalendarAdapter(self.clients, self.snapshots, self.registry,
            self.coordination, self.plans, clock=self.clock)
        self.executor = ApplyService(self.service, self.writer, clock=self.clock)

    def calendar_request(self, context, **options):
        if self.bootstrapping:
            self.api.events = {e["id"]: deepcopy(e) for e in self.pages[0]["items"]}
        return self.clients.calendar_request(context, **options)

    async def prepare(self, **kwargs):
        await super().prepare(**kwargs)
        self.bootstrapping = False
        self.reader.clients = self.clients
        self.api.calls.clear()

    def scope(self, *, boundary="mcp", lifetime=timedelta(minutes=5)):
        return shared_clock_request(self, boundary=boundary, lifetime=lifetime)

    @contextmanager
    def recovery_scope(self, run_id):
        """A genuinely NEW synthetic authenticated run, with the current clock."""
        caller = AuthenticatedCaller(principal_id=self.config.identities.agent_service_account,
            session_id="t19-recovery-session", run_id=run_id, authenticated_at=self.now,
            expires_at=self.now + timedelta(minutes=5))
        binding = ServerRunBinding(owner_id=self.config.student.trusted_user_id,
            signin_principal_id=self.config.student.allowed_signin_principal,
            session_id=caller.session_id, run_id=run_id, config_revision=self.config.config_revision,
            expires_at=caller.expires_at)
        factory = RequestContextFactory(self.policy, boundary="mcp", clock=self.clock,
            authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
            bindings=SimpleNamespace(resolve=lambda **k: binding))
        with factory.request(object()) as ctx:
            yield ctx

    async def apply(self):
        with self.scope() as ctx:
            return await self.executor.apply(ctx, self.request_input)

    def ledger(self):
        path = _request_path(self.config.student.trusted_user_id,
            self.config.student.calendar_id, self.request_input.request_key)
        return _decode(self.documents.data[path], "execution", _Ledger)

    def claim_value(self):
        return _decode(self.documents.data[_claim_path(self.config.student.calendar_id)], "calendar_claim", _Claim)

    async def stop(self, *, at=None):
        """Trusted TEST ONLY durable attestation, never production runtime proof."""
        previous = self.claim_value().previous or self.claim_value().lease
        value = WriterStoppedEvidence(holder_id=previous.holder_id, generation=previous.generation,
            stopped_at=self.now if at is None else at, evidence_reference="synthetic-t19-runtime-stop")
        async def put(tx):
            tx.set("test_stops/" + str(previous.generation), _encode("test_stop", value))
        await self.documents.transact(put)
        self.coordination.recovery_guard = FakeStopGuard()

    def expire_lease(self):
        self.now = self.claim_value().lease.expires_at + timedelta(microseconds=1)


def codes(result):
    return {f.code for f in result.findings}


@pytest.mark.parametrize("update,two", [(False, False), (False, True), (True, False), (True, True)])
def test_actual_composition_success_duplicate_saved_intent(monkeypatch, update, two):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(update=update, two=two)
        result = await h.apply()
        assert result.execution.status == "applied_unverified", result
        assert all(s.status == "completed" and s.attempts == 1 for s in result.execution.operations)
        assert not result.findings and result.verification is None
        assert ApplyResult.model_validate_json(result.model_dump_json()) == result
        before = deepcopy(h.api.calls)
        h.now += timedelta(minutes=16)
        replay = await h.apply()
        assert replay.execution == result.execution and h.api.calls == before
        assert len(h.api.mutations) == len(h.plan.payload.operations)
        with h.scope() as ctx:
            ledger = await h.executions.get_ledger(ctx, "apply-key")
            assert all(a.ordinal == 1 for a in ledger.attempts)
        for call in h.api.mutations:
            if call[0] == "PATCH":
                op = next(o for o in h.plan.payload.operations if o.kind == "update")
                assert call[4]["If-Match"] == op.expected_etag
                assert set(call[3]) == {"start", "end"}
    run(scenario())


@pytest.mark.parametrize("case", ["new_key", "same_key_other_plan", "changed_payload"])
def test_idempotency_cannot_change_payload_or_hide_outcomes(monkeypatch, case):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare()
        await h.apply()
        execution_before = h.ledger().execution
        before = deepcopy(h.api.mutations)
        if case == "new_key":
            h.request_input = changed(h.request_input, request_key="hide-existing")
        elif case == "same_key_other_plan":
            other = await h.seed(version=2)
            h.request_input = changed(h.request_input, plan_id=other.payload.plan_id,
                                      version=other.payload.version)
            with pytest.raises(BindingConflict, match="^request_key_payload_conflict$"):
                await h.apply()
            assert h.ledger().execution == execution_before
            assert h.api.mutations == before
            return
        else:
            altered = PlanVersion.seal(changed(h.plan.payload, created_at=NOW + timedelta(microseconds=1)))
            with h.scope(boundary="backend") as ctx, pytest.raises(BindingConflict):
                await h.plans.create(ctx, altered)
            assert h.api.mutations == before
            return
        with pytest.raises(BindingConflict):
            await h.apply()
        assert h.api.mutations == before
    run(scenario())


@contextmanager
def normal_proposal_scope(h, *, boundary="mcp", lifetime=timedelta(minutes=5)):
    """Real capabilities with current clocks and one proposal run on both boundaries."""
    principal = (h.config.student.allowed_signin_principal if boundary == "backend"
                 else h.config.identities.agent_service_account)
    caller = AuthenticatedCaller(principal_id=principal, session_id="t19-round1-session",
        run_id="t19-round1-normal-run", authenticated_at=h.now, expires_at=h.now + lifetime)
    binding = ServerRunBinding(owner_id=h.config.student.trusted_user_id,
        signin_principal_id=h.config.student.allowed_signin_principal, session_id=caller.session_id,
        run_id=caller.run_id, config_revision=h.config.config_revision, expires_at=caller.expires_at)
    factory = RequestContextFactory(h.policy, boundary=boundary, clock=h.clock,
        authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
        bindings=SimpleNamespace(resolve=lambda **k: binding))
    with factory.request(object()) as ctx:
        token = h.current.set(ctx)
        try:
            yield ctx
        finally:
            h.current.reset(token)


async def normal_t15_recovery(h, *, two_updates=False):
    """Actual T15 bootstrap/recovery; only old bootstrap Calendar effect is synthetic."""
    from campuspulse_recovery.application.proposals import ProposalBasis, ProposalService, _validate_executable
    from campuspulse_recovery.domain.models import (
        ApplyPlanInput, CalendarEvent, Interval, OwnedBlockRecord, OwnershipMarker,
        PlanningConstraints, new_block_id,
    )
    from campuspulse_recovery.domain.solver import solve_repair
    from campuspulse_recovery.integrations.calendar_read import ownership_properties
    from tests.integrations.test_calendar_read import page

    owner, calendar = h.config.student.trusted_user_id, h.config.student.calendar_id
    aid = next(f"round1-assignment-{i}" for i in range(100) if (
        new_block_id(owner, calendar, f"round1-assignment-{i}", "solver-v1-session-0")
        < new_block_id(owner, calendar, f"round1-assignment-{i}", "solver-v1-session-1")) != two_updates)
    h.rows[0][0] = aid
    h.rows[0][4] = 120 if two_updates else 60
    h.scope = lambda **kwargs: normal_proposal_scope(h, **kwargs)

    async def publish(plan_id, expected):
        with h.scope() as ctx:
            assignments = await h.assignment_reader.read(ctx, h.horizon)
            current_calendar = await h.reader.read(ctx, h.horizon)
        constraints = PlanningConstraints(config_revision=h.config.config_revision,
            timezone=h.config.student.timezone, horizon=h.horizon,
            working_windows=(Interval(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=5)),))
        basis = ProposalBasis(plan_id=plan_id, version=1, assignments=assignments.source.reference,
            calendar=current_calendar.source.reference, constraints=constraints, now=NOW, expected_current=expected)
        async def resolve(ctx):
            return basis
        service = ProposalService(h.snapshots, h.plans, h.policy,
            SimpleNamespace(resolve=resolve), clock=h.clock)
        with h.scope(boundary="backend") as ctx:
            result = await service.propose(ctx)
        assert result.approval_ready
        inputs = SolveRepairInput(assignments=assignments, calendar=current_calendar,
            constraints=constraints, now=NOW, plan_id=plan_id, version=1)
        solved = solve_repair(inputs)
        _validate_executable(inputs, solved)
        assert result.plan.payload.operations == solved.operations
        return result.plan

    bootstrap = await publish("round1-bootstrap", None)
    raw_events = []
    with h.scope() as ctx:
        for op in bootstrap.payload.operations:
            record = OwnedBlockRecord(owner_id=owner, calendar_id=calendar,
                block_id=op.target.block_id, assignment_id=op.target.assignment_id, event_id=op.event_id,
                state="reserved", intent_plan=plan_reference(bootstrap),
                intent_operation_id=op.operation_id, reserved_at=NOW)
            observation = CalendarEvent(event_id=op.event_id, etag='"bootstrap"', interval=op.target.interval,
                campuspulse_marker=OwnershipMarker(owner_id=owner, calendar_id=calendar,
                    block_id=op.target.block_id, assignment_id=op.target.assignment_id,
                    create_operation_id=op.operation_id))
            await h.registry.reserve(ctx, record)
            await h.registry.confirm(ctx, record, observed=observation, at=NOW)
            raw_events.append(event(op.event_id, start=op.target.interval.start, end=op.target.interval.end,
                etag=observation.etag, extendedProperties={"private": ownership_properties(observation.campuspulse_marker)}))
    h.pages = [page(*raw_events, event("round1-original-busy",
        start=min(op.target.interval.start for op in bootstrap.payload.operations),
        end=max(op.target.interval.end for op in bootstrap.payload.operations)))]
    h.rows[0][4] = 120
    h.plan = await publish("round1-recovery", plan_reference(bootstrap))
    assert len(h.plan.payload.operations) == 2 and h.plan.payload.operations[0].kind == "update"
    if two_updates:
        assert all(op.kind == "update" for op in h.plan.payload.operations)
        assert h.plan.payload.operations[0].before.start > h.plan.payload.operations[1].before.start
    else:
        assert h.plan.payload.operations[1].kind == "create"
    h.bootstrapping = False
    h.reader.clients = h.clients
    h.request_input = ApplyPlanInput(plan_id=h.plan.payload.plan_id, version=1, request_key="apply-key")
    h.now = min(o.before.start for o in h.plan.payload.operations if o.kind == "update") - timedelta(minutes=2)
    with h.scope(boundary="backend") as ctx:
        approval = await h.approvals.approve(ctx, plan_reference(h.plan), "round1-synthetic-late-consent")
    h.api.calls.clear()
    return approval


def test_normal_t15_partial_retry_ignores_only_completed_update_original_time(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        approval = await normal_t15_recovery(h)
        original_plan = h.plan
        update = original_plan.payload.operations[0]
        def interrupt_next_read(raw, response):
            h.api.failures["GET"] = TimeoutError("synthetic actual next-preflight interruption")
            return response(200, raw)
        h.api.after_write = interrupt_next_read
        first = await h.apply()
        assert first.execution.status == "partial"
        assert [s.status for s in first.execution.operations] == ["completed", "pending"]
        assert len(h.api.mutations) == 1 and h.api.mutations[0][0] == "PATCH"
        assert h.claim_value().released
        prior_attempt = h.ledger().attempts[0]
        h.api.after_write = None
        h.now = update.before.start + timedelta(seconds=1)
        assert all(b.interval.start > h.now for b in original_plan.payload.blocks)
        assert approval.active_at(h.now)
        with h.scope() as ctx:
            lease = await h.claim(ctx)
            ready = await h.service.check(ctx, h.request_input, lease)
            await h.coordination.release(ctx, lease)
        assert ready.status == "ready" and not ready.findings
        second = await h.apply()
        assert second.execution.status == "applied_unverified", second
        assert not second.findings and second.verification is None
        assert [c[0] for c in h.api.mutations] == ["PATCH", "POST"]
        assert second.execution.operations[0] == first.execution.operations[0]
        assert h.ledger().attempts[0] == prior_attempt
        assert second.execution.operations[1].attempts == 1
        assert second.execution.run_id == first.execution.run_id
        with h.scope() as ctx:
            assert await h.plans.get(ctx, original_plan.payload.plan_id, 1) == original_plan
        assert h.claim_value().released
    run(scenario())


@pytest.mark.parametrize("stage", ["before_dispatch", "after_dispatch", "final_io", "last_guard_read"])
@pytest.mark.parametrize("which_update", ["current", "other_pending"])
def test_normal_t15_unfinished_update_original_start_still_blocks(monkeypatch, stage, which_update):
    async def scenario():
        h = Harness(monkeypatch)
        approval = await normal_t15_recovery(h, two_updates=which_update == "other_pending")



        h.executor = ApplyService(h.service, h.writer, clock=h.clock, deadline_seconds=240)
        current = h.plan.payload.operations[0]
        guarded = h.plan.payload.operations[1] if which_update == "other_pending" else current
        limit = guarded.before.start
        assert all(b.interval.start > limit for b in h.plan.payload.blocks)
        if which_update == "other_pending":
            assert current.before.start > limit
        with h.scope() as ctx:
            injected = False
            armed = False
            def cross_boundary():
                nonlocal injected
                injected = True
                h.now = limit
                assert approval.active_at(h.now) and h.now < ctx.expires_at
                assert h.now < h.claim_value().lease.expires_at

            if stage == "before_dispatch":
                original = h.service.check
                async def delayed(*args):
                    result = await original(*args)
                    assert result.status == "ready"
                    cross_boundary()
                    return result
                h.service.check = delayed
            elif stage in ("after_dispatch", "last_guard_read"):
                original = h.executions.record_operation
                async def dispatched(*args, **kwargs):
                    nonlocal armed
                    result = await original(*args, **kwargs)
                    if kwargs["operation"].status == "in_flight":
                        armed = True
                        if stage == "after_dispatch":
                            cross_boundary()
                    return result
                h.executions.record_operation = dispatched
                if stage == "last_guard_read":
                    original_get = h.executions.get
                    async def delayed_get(*args, **kwargs):
                        result = await original_get(*args, **kwargs)
                        if armed and not injected:
                            assert result.operations[0].status == "in_flight"
                            cross_boundary()
                        return result
                    h.executions.get = delayed_get
            else:
                original = h.registry.get_by_event
                async def delayed(context, event_id):
                    record = await original(context, event_id)
                    if not injected and h.ledger().execution.operations[0].status == "in_flight":
                        cross_boundary()
                    return record
                h.registry.get_by_event = delayed
            result = await h.executor.apply(ctx, h.request_input)
        assert injected and not h.api.mutations
        states = result.execution.operations
        if stage == "before_dispatch":
            assert all(s.status == "pending" and s.attempts == 0 for s in states)
        else:
            assert states[0].status == "unknown" and states[0].attempts == 1
            assert h.claim_value().writer_uncertain
        assert states[1].status == "pending" and states[1].attempts == 0
    run(scenario())


@pytest.mark.parametrize("boundary", ["lease", "consent", "context", "target"])
def test_guard_rechecks_time_and_scope_after_last_durable_execution_read(monkeypatch, boundary):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(approve=boundary != "target")
        if boundary == "consent":
            h.now = NOW + timedelta(minutes=9, seconds=59)
        elif boundary == "target":
            h.now = min(b.interval.start for b in h.plan.payload.blocks) - timedelta(seconds=1)
            with h.scope(boundary="backend") as backend:
                await h.approvals.approve(backend, plan_reference(h.plan), "round1-target-consent")
        with h.scope(lifetime=timedelta(seconds=1) if boundary == "context" else timedelta(minutes=5)) as ctx:
            armed = False
            injected = False
            original = h.executions.record_operation
            async def dispatch(*args, **kwargs):
                nonlocal armed
                result = await original(*args, **kwargs)
                if kwargs["operation"].status == "in_flight":
                    armed = True
                return result
            h.executions.record_operation = dispatch
            original_get = h.executions.get
            async def delayed_get(*args, **kwargs):
                nonlocal injected
                result = await original_get(*args, **kwargs)
                if armed and not injected:
                    injected = True
                    h.now = {"lease": h.claim_value().lease.expires_at,
                        "consent": NOW + timedelta(minutes=10), "context": ctx.expires_at,
                        "target": min(b.interval.start for b in h.plan.payload.blocks)}[boundary]
                return result
            h.executions.get = delayed_get
            if boundary == "context":
                with pytest.raises(AccessDenied):
                    await h.executor.apply(ctx, h.request_input)
            else:
                await h.executor.apply(ctx, h.request_input)
        assert injected and not h.api.mutations
        assert h.ledger().execution.operations[0].status in ("in_flight", "unknown")
    run(scenario())


@pytest.mark.parametrize("revoke", [True, False], ids=["revoked", "active_control"])
def test_final_io_guard_rechecks_actual_consent_after_execution_document_delivery(monkeypatch, revoke):
    """Delay the actual DB read, never fabricate a high-level approval result."""
    async def scenario():
        import inspect
        from campuspulse_recovery.application.proposals import _validate_executable
        from campuspulse_recovery.domain.solver import solve_repair

        h = Harness(monkeypatch)
        await h.prepare()
        inputs = SolveRepairInput(assignments=h.original_a, calendar=h.original_c,
            constraints=h.plan.payload.constraints, now=NOW,
            plan_id=h.plan.payload.plan_id, version=h.plan.payload.version)
        _validate_executable(inputs, solve_repair(inputs))
        path = _request_path(h.config.student.trusted_user_id,
            h.config.student.calendar_id, h.request_input.request_key)
        original_get = h.documents.get
        in_flight_deliveries = 0
        injected = False
        observed_approval = None
        saved_intent = None
        clock_before = h.now

        async def delayed_document_get(read_path):
            nonlocal in_flight_deliveries, injected, observed_approval, saved_intent
            document = await original_get(read_path)
            if read_path == path and document is not None:
                execution = _decode(document, "execution", _Ledger).execution
                if execution.operations[0].status == "in_flight":
                    in_flight_deliveries += 1
                    if in_flight_deliveries == 2:


                        stack = {frame.function for frame in inspect.stack()}
                        assert {"_send_guard", "_call", "_write"} <= stack
                        injected = True
                        unchanged = deepcopy(document)
                        saved_intent = _decode(document, "execution", _Ledger)
                        assert not h.api.mutations and saved_intent.attempts
                        await asyncio.sleep(0)
                        with h.scope(boundary="backend") as backend:
                            if revoke:
                                with h.approvals.ui_action(backend, plan_reference(h.plan),
                                        "round2-synthetic-ledger-delivery-revoke") as action:
                                    await h.approvals.invalidate(backend, plan_reference(h.plan),
                                        reason="revoked", at=action.at)
                            observed_approval = await h.approvals.get(backend, plan_reference(h.plan))



                        assert document == unchanged == await original_get(read_path)
            return document

        h.documents.get = delayed_document_get
        with h.scope() as ctx:
            result = await h.executor.apply(ctx, h.request_input)
            ctx.require_active()
            assert h.now < h.claim_value().lease.expires_at
            assert await h.plans.get(ctx, h.plan.payload.plan_id, h.plan.payload.version) == h.plan
        assert injected and h.now == clock_before and saved_intent is not None
        assert observed_approval.status == ("revoked" if revoke else "active")
        assert observed_approval.active_at(h.now) is not revoke
        state = result.execution.operations[0]
        assert state.attempts == 1 and h.ledger().attempts == saved_intent.attempts
        assert result.execution.plan == saved_intent.execution.plan
        assert result.execution.run_id == saved_intent.execution.run_id
        assert result.verification is None
        if revoke:
            assert not h.api.mutations
            assert result.execution.status == "unknown" and state.status == "unknown"
            assert state.observed_event is None and state.error_code is not None
            assert h.claim_value().writer_uncertain and not h.claim_value().released
            assert result.findings
        else:
            assert [c[0] for c in h.api.mutations] == ["POST"]
            assert result.execution.status == "applied_unverified" and state.status == "completed"
            assert state.observed_event.interval == h.plan.payload.operations[0].target.interval
            assert not result.findings and h.claim_value().released
    run(scenario())


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "invalid_response", "postwrite_binding", "checkpoint", "confirmation"])
def test_timeout_success_and_postwrite_failures_reconcile_actual_target(monkeypatch, update, failure):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(update=update)
        if failure in ("timeout", "invalid_response", "postwrite_binding"):
            def fail(raw, response):
                if failure == "invalid_response":
                    return response(200, {"id": raw["id"]})
                if failure == "postwrite_binding":
                    raise BindingConflict("synthetic_after_remote_success")
                raise TimeoutError("private data must not escape")
            h.api.after_write = fail
        elif failure == "confirmation" and not update:
            original = h.registry.confirm
            count = 0
            async def fail_once(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    raise BindingConflict("synthetic_confirmation_failed")
                return await original(*args, **kwargs)
            h.registry.confirm = fail_once
        else:
            original = h.executions.record_operation
            failed = False
            async def fail_once(*args, **kwargs):
                nonlocal failed
                if kwargs["operation"].status == "completed" and not failed:
                    failed = True
                    raise PortError("storage_transaction_outcome_unknown")
                return await original(*args, **kwargs)
            h.executions.record_operation = fail_once
        result = await h.apply()
        assert result.execution.status == "applied_unverified", result
        assert result.execution.operations[0].reconciled_at is not None
        assert len(h.api.mutations) == 1
        assert h.claim_value().writer_uncertain and not h.claim_value().released
        assert result.verification is None and "private data" not in result.model_dump_json()
        before = deepcopy(h.api.calls)
        assert (await h.apply()).execution == result.execution
        assert h.api.calls == before
    run(scenario())


@pytest.mark.parametrize("failure", ["timeout_before_write", "ambiguous404", "read_failure", "collision", "stale412"])
def test_unproven_effect_stays_unknown_and_never_blindly_resends(monkeypatch, failure):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(update=failure == "stale412")
        if failure == "stale412":
            h.api.before_patch = lambda raw: raw.update(etag='"user-edit"')
        elif failure == "collision":

            original = h.executor._write
            async def collision(ctx, plan, op, record, lease, request_key):
                h.api.events[op.event_id] = event(op.event_id, start=op.target.interval.start)
                return await original(ctx, plan, op, record, lease, request_key)
            h.executor._write = collision
        elif failure == "read_failure":
            def fail(raw, response):
                h.api.failures["GET"] = TimeoutError()
                raise TimeoutError()
            h.api.after_write = fail
        else:
            h.api.failures["POST"] = TimeoutError() if failure == "timeout_before_write" else 503
        first = await h.apply()
        assert first.execution.status == "unknown", first
        assert first.execution.operations[0].attempts == 1
        before = len(h.api.mutations)
        assert (await h.apply()).execution == first.execution
        assert len(h.api.mutations) == before
        h.expire_lease()
        again = await h.apply()
        assert len(h.api.mutations) == before and again.execution.status in ("unknown", "applied_unverified")
        if failure == "stale412":
            assert next(v for v in h.api.events.values() if v["etag"] == '"user-edit"')
    run(scenario())


@pytest.mark.parametrize("stage", ["dispatch_commit_unknown", "dispatch_no_commit", "outcome_commit_unknown"])
def test_uncertain_transaction_commit_never_grants_another_send(monkeypatch, stage):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare()
        original = h.executions.record_operation
        injected = False
        async def ambiguous(*args, **kwargs):
            nonlocal injected
            selected = "completed" if stage == "outcome_commit_unknown" else "in_flight"
            if not injected and kwargs["operation"].status == selected:
                injected = True
                if stage != "dispatch_no_commit":
                    await original(*args, **kwargs)
                raise PortError("storage_transaction_outcome_unknown")
            return await original(*args, **kwargs)
        h.executions.record_operation = ambiguous
        result = await h.apply()
        assert len(h.api.mutations) == (1 if stage == "outcome_commit_unknown" else 0)
        if stage == "dispatch_commit_unknown":
            assert result.execution.operations[0].status == "in_flight"
            assert not h.claim_value().released
        elif stage == "dispatch_no_commit":
            assert result.execution.operations[0].status == "pending"
        else:
            assert result.execution.status == "applied_unverified"
    run(scenario())


@pytest.mark.parametrize("case", ["clean", "external_busy", "consent_revoked", "missing_stop"])
def test_partial_restart_preserves_completed_and_checks_new_availability(monkeypatch, tmp_path, case):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(two=True)


        original_check = h.service.check
        checks = 0
        async def fail_preflight(*args):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise PortError("synthetic_read_interruption")
            return await original_check(*args)
        h.service.check = fail_preflight
        first = await h.apply()
        assert first.execution.status == "partial" and len(h.api.mutations) == 1
        checkpoint = tmp_path / "t19-fake-documents.pkl"
        h.documents.checkpoint(checkpoint)
        h.documents.data = type(h.documents).restore(checkpoint).data
        h.service.check = original_check
        if case == "external_busy":
            op = h.plan.payload.operations[1]
            h.api.events["new-busy"] = event("new-busy", start=op.target.interval.start)
        elif case == "consent_revoked":
            with h.scope(boundary="backend") as ctx:
                with h.approvals.ui_action(ctx, plan_reference(h.plan), "synthetic-revoke") as action:
                    await h.approvals.invalidate(ctx, plan_reference(h.plan), reason="revoked", at=action.at)
        elif case == "missing_stop":


            with h.scope() as ctx:
                await h.coordination.acquire(ctx, holder_id="synthetic-crash", now=h.now,
                    expires_at=h.now + timedelta(seconds=1))
            h.expire_lease()
        second = await h.apply()
        assert second.execution.operations[0] == first.execution.operations[0]
        assert second.execution.status == ("applied_unverified" if case == "clean" else "partial")
        assert len(h.api.mutations) == (2 if case == "clean" else 1)
    run(scenario())


@pytest.mark.parametrize("boundary", ["lease", "consent", "context", "target"])
@pytest.mark.parametrize("stage", ["after_ready", "after_dispatch"])
def test_expiry_between_awaits_and_send_zero_mutations(monkeypatch, boundary, stage):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(approve=boundary != "target")
        if boundary == "consent":
            h.now = NOW + timedelta(minutes=9, seconds=59)
        elif boundary == "target":
            h.now = min(b.interval.start for b in h.plan.payload.blocks) - timedelta(seconds=1)
            with h.scope(boundary="backend") as ctx:
                await h.approvals.approve(ctx, plan_reference(h.plan), "synthetic-late-consent")
        lifetime = timedelta(seconds=1) if boundary == "context" else timedelta(minutes=5)
        with h.scope(lifetime=lifetime) as ctx:
            async def advance():
                if boundary == "lease":
                    h.now = h.claim_value().lease.expires_at
                elif boundary == "context":
                    h.now = ctx.expires_at
                elif boundary == "consent":
                    h.now = NOW + timedelta(minutes=10)
                else:
                    h.now = min(b.interval.start for b in h.plan.payload.blocks)
            if stage == "after_ready":
                original = h.service.check
                async def delayed(*args):
                    result = await original(*args)
                    await advance()
                    return result
                h.service.check = delayed
            else:
                original = h.executions.record_operation
                async def delayed(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    if kwargs["operation"].status == "in_flight":
                        await advance()
                    return result
                h.executions.record_operation = delayed
            if boundary == "context":
                with pytest.raises(AccessDenied):
                    await h.executor.apply(ctx, h.request_input)
            else:
                await h.executor.apply(ctx, h.request_input)
        assert not h.api.mutations
        state = h.ledger().execution.operations[0]
        assert state.status == "pending" if stage == "after_ready" else state.status in ("in_flight", "unknown")
    run(scenario())


def test_concurrent_callers_and_same_token_cas_only_one_permit(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare()
        with h.scope() as ctx:
            a, b = await asyncio.gather(h.executor.apply(ctx, h.request_input), h.executor.apply(ctx, h.request_input))
        assert len(h.api.mutations) == 1
        assert h.ledger().execution.status == "applied_unverified"
        assert all(s.attempts == 1 for s in h.ledger().execution.operations)
        assert any("apply_interrupted_recovery_required" in codes(r) for r in (a, b))

        h2 = Harness(monkeypatch)
        await h2.prepare()
        with h2.scope() as ctx:
            lease = await h2.claim(ctx)
            state = await h2.begin(ctx, lease)
            op = changed(state.operations[0], status="in_flight", attempts=1,
                         fencing_generation=lease.generation, updated_at=h2.now)
            async def send():
                return await h2.executions.record_operation(ctx, request_key="apply-key",
                    expected_revision=state.revision, operation=op, lease=lease)
            outcomes = await asyncio.gather(send(), send(), return_exceptions=True)
            assert sum(not isinstance(o, Exception) for o in outcomes) == 1
    run(scenario())


@pytest.mark.parametrize("stop", [False, True])
def test_read_no_effect_requires_stopped_original_writer_then_new_read(monkeypatch, stop):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(update=True)
        h.api.failures["PATCH"] = TimeoutError()
        first = await h.apply()
        assert first.execution.status == "unknown" and len(h.api.mutations) == 1
        if stop:
            await h.stop()
        h.expire_lease()
        result = await h.apply()
        assert result.execution.status == ("applied_unverified" if stop else "unknown"), result
        assert len(h.api.mutations) == (2 if stop else 1)
        assert h.ledger().attempts[0].ordinal == (2 if stop else 1)
    run(scenario())


def test_recovery_after_consent_expiry_uses_fresh_scope_no_new_write(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(two=True)
        def timeout(raw, response):
            h.api.failures["GET"] = TimeoutError()
            raise TimeoutError()
        h.api.after_write = timeout
        first = await h.apply()
        assert first.execution.status == "unknown"
        original_run = first.execution.run_id
        h.now += timedelta(minutes=16)
        h.api.after_write = None

        async def forbidden(*args):
            raise AssertionError("consent preflight during read-only recovery")
        h.service.check = forbidden
        with h.recovery_scope("distinct-recovery-after-expired-consent") as recovery:
            assert recovery.trusted.run_id != original_run
            result = await h.executor.apply(recovery, h.request_input)
        assert result.execution.status == "partial" and len(h.api.mutations) == 1
        assert result.execution.run_id == original_run
        assert result.execution.operations[0].status == "completed"
        assert h.claim_value().writer_uncertain and h.claim_value().previous is not None
    run(scenario())


@pytest.mark.parametrize("bad", ["copied", "closed", "dto", "backend"])
def test_invalid_capabilities_never_read_or_write(monkeypatch, bad):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare()
        with h.scope(boundary="backend" if bad == "backend" else "mcp") as ctx:
            if bad == "copied":
                with pytest.raises(TypeError):
                    copy(ctx)
                candidate = copy(ctx.as_port_context())
            else:
                candidate = ctx.as_port_context() if bad == "dto" else ctx
            if bad != "closed":
                with pytest.raises(AccessDenied):
                    await h.executor.apply(candidate, h.request_input)
        if bad == "closed":
            with pytest.raises(AccessDenied):
                await h.executor.apply(ctx, h.request_input)
        assert not h.api.calls
    run(scenario())


def test_attempt_limits_and_terminal_failure_cannot_be_hidden(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(update=True)
        h.executor = ApplyService(h.service, h.writer, clock=h.clock, max_attempts=1)
        h.api.failures["PATCH"] = TimeoutError()
        await h.apply()
        await h.stop()
        h.expire_lease()
        final = await h.apply()
        assert final.execution.status == "failed" and len(h.api.mutations) == 1
        assert (await h.apply()).execution == final.execution
        h.request_input = changed(h.request_input, request_key="hide-failed")
        with pytest.raises(BindingConflict):
            await h.apply()
    run(scenario())


def test_intent_reservation_precede_io_and_callbacks_never_send(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare()
        original = h.documents.transact
        in_transaction = False
        async def tracked(callback):
            nonlocal in_transaction
            async def invoke(tx):
                nonlocal in_transaction
                in_transaction = True
                try:
                    return await callback(tx)
                finally:
                    in_transaction = False
            return await original(invoke)
        h.documents.transact = tracked
        original_request = h.api.request
        def request(*args, **kwargs):
            assert not in_transaction
            if kwargs.get("method") in ("POST", "PATCH"):
                ledger = h.ledger()
                assert ledger.execution.operations[0].status == "in_flight" and ledger.attempts
                assert any(v.get("kind") == "owned_block" for v in h.documents.data.values())
            return original_request(*args, **kwargs)
        h.api.request = request
        assert (await h.apply()).execution.status == "applied_unverified"
    run(scenario())


@pytest.mark.parametrize("boundary", ["lease", "context", "consent"])
def test_postwrite_expiry_retains_inflight_then_fresh_scope_recovers(monkeypatch, boundary):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(two=True)
        if boundary == "consent":
            h.now = NOW + timedelta(minutes=9, seconds=59)
        with h.scope(lifetime=timedelta(seconds=1) if boundary == "context" else timedelta(minutes=5)) as ctx:
            def after(raw, response):
                h.now = (ctx.expires_at if boundary == "context" else
                         NOW + timedelta(minutes=10) if boundary == "consent" else h.claim_value().lease.expires_at)
                return response(200, raw)
            h.api.after_write = after
            if boundary == "context":
                with pytest.raises(AccessDenied):
                    await h.executor.apply(ctx, h.request_input)
            else:
                result = await h.executor.apply(ctx, h.request_input)
                assert result.execution.status == ("partial" if boundary == "consent" else "applying")
        assert len(h.api.mutations) == 1
        h.api.after_write = None
        if boundary != "consent":
            h.expire_lease()
            recovered = await h.apply()
            assert recovered.execution.status == "partial"
            assert recovered.execution.operations[0].status == "completed"
            assert len(h.api.mutations) == 1 and h.claim_value().writer_uncertain
    run(scenario())


def test_outcome_checkpoint_failure_survives_new_service_instances(monkeypatch, tmp_path):
    async def scenario():
        from campuspulse_recovery.application.preflight import PreflightService
        from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
        from campuspulse_recovery.storage.operations import FirestoreExecutionStore
        from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
        from campuspulse_recovery.storage.plans import FirestorePlanStore
        from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
        from campuspulse_recovery.web.approval import FirestoreApprovalStore
        h = Harness(monkeypatch)
        await h.prepare(two=True)
        original = h.executions.record_operation
        async def fail_outcomes(*args, **kwargs):
            if kwargs["operation"].status != "in_flight":
                raise PortError("synthetic_checkpoint_unavailable")
            return await original(*args, **kwargs)
        h.executions.record_operation = fail_outcomes
        first = await h.apply()
        assert first.execution.operations[0].status == "in_flight" and len(h.api.mutations) == 1
        checkpoint = tmp_path / "t19-restart.pkl"
        h.documents.checkpoint(checkpoint)
        h.documents = type(h.documents).restore(checkpoint)
        h.plans = FirestorePlanStore(h.documents, h.policy)
        h.snapshots = FirestoreSnapshotStore(h.documents, h.policy)
        h.registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        h.coordination = FirestoreCalendarCoordination(h.documents, h.policy, clock=h.clock)
        h.approvals = FirestoreApprovalStore(h.documents, h.policy, clock=h.clock)
        h.executions = FirestoreExecutionStore(h.documents, h.policy, coordination=h.coordination,
            approval_guard=h.approvals.transaction_guard())
        h.assignment_reader.snapshots = h.snapshots
        h.reader.snapshots, h.reader.registry = h.snapshots, h.registry
        h.service = PreflightService(h.plans, h.snapshots, h.approvals, h.executions,
            h.coordination, h.registry, h.assignment_reader, h.reader, h.policy, clock=h.clock)
        h.writer = CalendarAdapter(h.clients, h.snapshots, h.registry, h.coordination, h.plans, clock=h.clock)
        h.executor = ApplyService(h.service, h.writer, clock=h.clock)
        h.expire_lease()
        recovered = await h.apply()
        assert recovered.execution.status == "partial" and len(h.api.mutations) == 1
        assert recovered.execution.run_id == first.execution.run_id
        assert h.ledger().attempts[0].ordinal == 1
    run(scenario())


@pytest.mark.parametrize("mode", ["cancel", "write_timeout", "total_deadline"])
def test_cancelled_await_is_not_writer_stop_proof(monkeypatch, mode):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare()
        h.executor = ApplyService(h.service, h.writer, clock=h.clock,
            deadline_seconds=0.03 if mode == "total_deadline" else 30,
            write_timeout_seconds=0.01 if mode != "cancel" else 20)
        entered = asyncio.Event()
        original = h.executor._write
        async def delayed(*args):

            result = await original(*args)
            entered.set()
            await asyncio.Event().wait()
            return result
        h.executor._write = delayed
        with h.scope() as ctx:
            task = asyncio.create_task(h.executor.apply(ctx, h.request_input))
            if mode == "cancel":
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
        assert len(h.api.mutations) <= 1
        if h.api.mutations:
            assert h.claim_value().writer_uncertain and not h.claim_value().released
            assert h.ledger().execution.operations[0].status in ("unknown", "completed")
    run(scenario())


def test_matching_read_does_not_clear_uncertainty_without_new_generation_stop(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(two=True)
        h.api.after_write = lambda raw, response: (_ for _ in ()).throw(TimeoutError())
        first = await h.apply()
        assert first.execution.status == "partial" and h.claim_value().writer_uncertain
        original_holder = h.claim_value().lease
        h.expire_lease()
        blocked = await h.apply()
        assert blocked.execution == first.execution and len(h.api.mutations) == 1
        assert h.claim_value().previous == original_holder

        await h.stop()
        h.expire_lease()
        h.api.after_write = None
        recovered = await h.apply()
        assert recovered.execution.status == "applied_unverified" and len(h.api.mutations) == 2
        assert h.claim_value().lease.generation == original_holder.generation + 2
        assert h.claim_value().released and not h.claim_value().writer_uncertain
        assert recovered.execution.operations[0] == first.execution.operations[0]
    run(scenario())


def test_no_effect_observation_before_stop_requires_later_read(monkeypatch):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(update=True)
        h.api.failures["PATCH"] = TimeoutError()
        await h.apply()
        stop_at = h.claim_value().lease.expires_at + timedelta(seconds=1)
        await h.stop(at=stop_at)
        h.expire_lease()
        first = await h.apply()
        assert first.execution.status == "unknown" and len(h.api.mutations) == 1
        count = len(h.api.calls)
        h.now = stop_at + timedelta(seconds=1)

        with h.scope() as ctx:
            execution, findings = await h.executor.reconciler.reconcile(ctx, h.plan,
                first.execution, h.claim_value().lease)
        assert not findings and execution.operations[0].status == "pending"
        assert len(h.api.calls) > count and len(h.api.mutations) == 1
        assert execution.operations[0].reconciled_at > stop_at
    run(scenario())


@pytest.mark.parametrize("value", [0, -1, 11, True])
def test_limits_reject_invalid_attempt_settings(monkeypatch, value):
    h = Harness(monkeypatch)
    with pytest.raises(ValueError):
        ApplyService(h.service, h.writer, clock=h.clock, max_attempts=value)


@pytest.mark.parametrize("change", ["external_busy", "deadline", "revoke"])
def test_new_change_between_successful_operations_forces_fresh_preflight(monkeypatch, change):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(two=True)
        original = h.executions.record_operation
        async def change_after_outcome(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs["operation"].status == "completed":
                if change == "external_busy":
                    op = h.plan.payload.operations[1]
                    h.api.events["new-busy"] = event("new-busy", start=op.target.interval.start)
                elif change == "deadline":
                    h.rows[0][3] = (NOW + timedelta(hours=2)).isoformat()
                else:
                    with h.scope(boundary="backend") as backend:
                        with h.approvals.ui_action(backend, plan_reference(h.plan), "synthetic-revoke-between") as action:
                            await h.approvals.invalidate(backend, plan_reference(h.plan), reason="revoked", at=action.at)
            return result
        h.executions.record_operation = change_after_outcome
        result = await h.apply()
        assert result.execution.status == "partial" and len(h.api.mutations) == 1
        assert [s.status for s in result.execution.operations] == ["completed", "pending"]
        assert h.sheet_calls >= (1 if change == "revoke" else 2)
        if change != "revoke":
            assert "apply_replan_required" in codes(result)
    run(scenario())


def test_no_change_uses_legal_aggregate_edges_and_no_calendar_writes(monkeypatch):
    async def scenario():
        from campuspulse_recovery.domain.solver import solve_repair
        from campuspulse_recovery.domain.models import Interval
        from campuspulse_recovery.domain.time_rules import planning_horizon
        h = Harness(monkeypatch)
        await h.prepare()
        await h.apply()
        start, end = planning_horizon(h.now)
        h.horizon = Interval(start=start, end=end)
        constraints = changed(h.plan.payload.constraints, horizon=h.horizon)
        with h.request(run="t19-nochange-proposal") as ctx:
            a = await h.assignment_reader.read(ctx, h.horizon)
            c = await h.reader.read(ctx, h.horizon)
        result = solve_repair(SolveRepairInput(assignments=a, calendar=c,
            constraints=constraints, now=h.now, plan_id="t19-nochange", version=1))
        assert result.status == "no_change" and not result.operations
        plan = PlanVersion.seal(PlanPayload(owner_id=h.plan.payload.owner_id,
            calendar_id=h.plan.payload.calendar_id, plan_id="t19-nochange", version=1,
            created_at=h.now, source_references=result.source_references,
            constraints=constraints, operations=(), blocks=result.blocks, allocations=result.allocations))
        with h.request("backend", run="t19-nochange-proposal") as ctx:
            await h.plans.create(ctx, plan)
            await h.plans.set_current(ctx, plan_reference(plan), expected=plan_reference(h.plan))
            await h.approvals.approve(ctx, plan_reference(plan), "synthetic-nochange-click")
        h.plan = plan
        h.request_input = changed(h.request_input, plan_id="t19-nochange", request_key="nochange-key")
        before = len(h.api.mutations)
        final = await h.apply()
        assert final.execution.status == "applied_unverified" and not final.execution.operations
        assert final.execution.revision == 3 and final.verification is None
        assert len(h.api.mutations) == before
    run(scenario())


def test_cancelled_to_thread_keeps_actual_original_writer_uncertain(monkeypatch):
    async def scenario():
        from threading import Event
        h = Harness(monkeypatch)
        await h.prepare()
        entered, finish, finished = Event(), Event(), Event()
        def delayed_remote(raw, response):
            entered.set()
            assert finish.wait(5), "test must release the synthetic HTTP thread"
            finished.set()
            return response(200, raw)
        h.api.after_write = delayed_remote
        with h.scope() as ctx:
            task = asyncio.create_task(h.executor.apply(ctx, h.request_input))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not finished.is_set()
                assert h.claim_value().writer_uncertain and not h.claim_value().released
                assert h.ledger().execution.operations[0].status == "unknown"
            finally:
                finish.set()
                await asyncio.to_thread(finished.wait, 5)
                if not task.done():
                    task.cancel()
        assert finished.is_set() and len(h.api.mutations) == 1
    run(scenario())


@pytest.mark.parametrize("boundary", ["lease", "consent", "context", "target", "revoke"])
def test_final_guard_after_t17_awaited_registry_read(monkeypatch, boundary):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(approve=boundary != "target")
        if boundary == "consent":
            h.now = NOW + timedelta(minutes=9, seconds=59)
        elif boundary == "target":
            h.now = min(b.interval.start for b in h.plan.payload.blocks) - timedelta(seconds=1)
            with h.scope(boundary="backend") as backend:
                await h.approvals.approve(backend, plan_reference(h.plan), "synthetic-delayed-consent")
        with h.scope(lifetime=timedelta(seconds=1) if boundary == "context" else timedelta(minutes=5)) as ctx:
            original = h.registry.get_by_event
            injected = False
            async def delayed(context, event_id):
                nonlocal injected
                record = await original(context, event_id)
                if not injected and h.ledger().execution.operations[0].status == "in_flight":
                    injected = True
                    if boundary == "revoke":
                        with h.scope(boundary="backend") as backend:
                            with h.approvals.ui_action(backend, plan_reference(h.plan), "synthetic-revoke-t17") as action:
                                await h.approvals.invalidate(backend, plan_reference(h.plan), reason="revoked", at=action.at)
                    else:
                        h.now = {"lease": h.claim_value().lease.expires_at,
                            "consent": NOW + timedelta(minutes=10), "context": ctx.expires_at,
                            "target": min(b.interval.start for b in h.plan.payload.blocks)}[boundary]
                return record
            h.registry.get_by_event = delayed
            if boundary == "context":
                with pytest.raises(AccessDenied):
                    await h.executor.apply(ctx, h.request_input)
            else:
                await h.executor.apply(ctx, h.request_input)
            assert injected and not h.api.mutations
            assert h.ledger().execution.operations[0].status in ("in_flight", "unknown")
    run(scenario())
