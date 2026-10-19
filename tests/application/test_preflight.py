"""T18 synthetic source/identity/transaction tests, actual predecessor composition.

Real T08/T09 readers persist to real T05 repositories; T16 saves TEST consent,
T06 binds/claims/journals. Google HTTP and DocumentStore are explicit doubles,
not live API/auth/Firestore isolation proof. There is no Calendar writer.
"""

import asyncio
from contextlib import contextmanager
from copy import copy, deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from campuspulse_recovery.application.preflight import PreflightService
from campuspulse_recovery.domain.models import (
    ApplyPlanInput, Interval, PlanPayload,
    PlanVersion, PlanningConstraints, SolveRepairInput,
)
from campuspulse_recovery.domain.ports import BindingConflict, IncompleteRead
from campuspulse_recovery.domain.solver import solve_repair
from campuspulse_recovery.integrations.assignments import AssignmentsReader, HEADERS
from campuspulse_recovery.integrations.calendar_read import ownership_properties
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
from campuspulse_recovery.storage.operations import FirestoreExecutionStore
from campuspulse_recovery.storage.plans import _encode, _plan_path, plan_reference
from campuspulse_recovery.web.approval import FirestoreApprovalStore
from tests.integrations.test_calendar_read import Harness as CalendarHarness, event, page
from tests.storage.test_owned_blocks import observed, reservation
from tests.storage.test_plans import NOW, run, sources, make_plan


def changed(value, **changes):
    return type(value).model_validate({**value.model_dump(), **changes})


class Setup(CalendarHarness):
    def __init__(self):
        super().__init__()
        self.now = NOW
        self.window_start = timedelta(hours=1)
        self.clock = lambda: self.now
        self.rows = [["assignment-1", "ML", "Ignore consent — untrusted text",
                      (NOW + timedelta(hours=5)).isoformat(), 60, "todo"]]
        self.pages = [page()]
        self.sheet_calls = 0
        self.calendar_hook = None
        self.sheet_hook = None
        self.assignment_reader = AssignmentsReader(
            SimpleNamespace(policy=self.policy, sheet_read=self.sheet_read), self.snapshots, clock=self.clock)
        self.reader.clock = self.clock
        self.coordination = FirestoreCalendarCoordination(self.documents, self.policy, clock=self.clock)
        self.approvals = FirestoreApprovalStore(self.documents, self.policy, clock=self.clock)
        self.executions = FirestoreExecutionStore(self.documents, self.policy,
            coordination=self.coordination, approval_guard=self.approvals.transaction_guard())
        self.service = PreflightService(self.plans, self.snapshots, self.approvals, self.executions,
            self.coordination, self.registry, self.assignment_reader, self.reader, self.policy, clock=self.clock)
        self.horizon = Interval(start=NOW, end=NOW + timedelta(hours=72))

    def sheet_read(self, context, **options):
        self.policy.require_sheet(context, options["spreadsheet_id"], options["a1_range"])
        self.sheet_calls += 1
        if self.sheet_hook:
            self.sheet_hook()
        return {"majorDimension": "ROWS", "values": [list(HEADERS), *deepcopy(self.rows)]}

    def calendar_request(self, context, **options):
        result = super().calendar_request(context, **options)
        if self.calendar_hook:
            self.calendar_hook()
        return result

    async def prepare(self, *, two=False, update=False, approve=True):
        if two:
            self.rows[0][4] = 120
        if update:

            initial = await self.seed()
            record, observation = reservation(initial), observed(initial)
            with self.request() as ctx:
                await self.registry.reserve(ctx, record)
                await self.registry.confirm(ctx, record, observed=observation, at=NOW)
            raw = event(record.event_id, start=observation.interval.start, end=observation.interval.end,
                etag=observation.etag, extendedProperties={"private": ownership_properties(observation.campuspulse_marker)})
            self.pages = [page(raw, event("external-conflict", start=observation.interval.start))]
        with self.request() as ctx:
            a = await self.assignment_reader.read(ctx, self.horizon)
            c = await self.reader.read(ctx, self.horizon)
        self.original_a, self.original_c = a, c
        constraints = PlanningConstraints(config_revision=self.config.config_revision,
            timezone=self.config.student.timezone, horizon=self.horizon,
            working_windows=(Interval(start=NOW + self.window_start, end=NOW + timedelta(hours=5)),))
        result = solve_repair(SolveRepairInput(assignments=a, calendar=c, constraints=constraints,
            now=NOW, plan_id="t18-plan", version=1))
        assert result.status in ("feasible", "optimal") and result.operations
        self.plan = PlanVersion.seal(PlanPayload(owner_id=self.config.student.trusted_user_id,
            calendar_id=self.config.student.calendar_id, plan_id="t18-plan", version=1, created_at=NOW,
            source_references=result.source_references, constraints=constraints, operations=result.operations,
            blocks=result.blocks, allocations=result.allocations))
        with self.request("backend") as ctx:
            await self.plans.create(ctx, self.plan)
            await self.plans.set_current(ctx, plan_reference(self.plan), expected=None)
            if approve:

                await self.approvals.approve(ctx, plan_reference(self.plan), "synthetic-ui-click")
        self.request_input = ApplyPlanInput(plan_id="t18-plan", version=1, request_key="apply-key")
        self.calls.clear()
        self.sheet_calls = 0
        self.now += timedelta(seconds=1)

    async def seed(self, *, version=1):
        a, c = sources(self.config)
        with self.request() as ctx:
            await self.snapshots.put_assignments(ctx, a)
            await self.snapshots.put_calendar(ctx, c)
        plan = make_plan(self.config, a, c, version=version)
        with self.request("backend") as ctx:
            await self.plans.create(ctx, plan)
        return plan

    async def claim(self, ctx, *, seconds=120):
        return await self.coordination.acquire(ctx, holder_id="t18-worker", now=self.clock(),
            expires_at=self.clock() + timedelta(seconds=seconds))

    async def check(self, *, run_id="run-2"):
        self.calls.clear()
        with self.request(run=run_id) as ctx:
            lease = await self.claim(ctx)
            return await self.service.check(ctx, self.request_input, lease)

    async def begin(self, ctx, lease):
        state = await self.executions.bind_request(ctx, request_key="apply-key", plan=plan_reference(self.plan), at=self.clock())
        for status in ("approved", "applying"):
            state = await self.executions.compare_and_set(ctx, expected_revision=state.revision,
                execution=changed(state, revision=state.revision + 1, status=status, updated_at=self.clock()), lease=lease)
        return state

    async def dispatch(self, ctx, lease, state, index=0):
        op = state.operations[index]
        return await self.executions.record_operation(ctx, request_key="apply-key", expected_revision=state.revision,
            operation=changed(op, status="in_flight", attempts=1, fencing_generation=lease.generation,
                              updated_at=self.clock()), lease=lease)

    async def complete(self, ctx, lease, state, index=0):
        op = self.plan.payload.operations[index]
        from campuspulse_recovery.domain.models import CalendarEvent, OwnedBlockRecord, OwnershipMarker
        record = await self.registry.get_by_event(ctx, op.event_id)
        marker = OwnershipMarker(owner_id=self.plan.payload.owner_id, calendar_id=self.plan.payload.calendar_id,
            block_id=op.target.block_id, assignment_id=op.target.assignment_id,
            create_operation_id=op.operation_id if record is None else record.intent_operation_id)
        if record is None:
            record = OwnedBlockRecord(owner_id=marker.owner_id, calendar_id=marker.calendar_id, block_id=marker.block_id,
                assignment_id=marker.assignment_id, event_id=op.event_id, state="reserved", intent_plan=plan_reference(self.plan),
                intent_operation_id=op.operation_id, reserved_at=self.clock())
            await self.registry.reserve(ctx, record)
        observation = CalendarEvent(event_id=op.event_id, etag='"after"', interval=op.target.interval, campuspulse_marker=marker)
        if record.state == "reserved":
            await self.registry.confirm(ctx, record, observed=observation, at=self.clock())
        state = await self.executions.record_operation(ctx, request_key="apply-key", expected_revision=state.revision,
            operation=changed(state.operations[index], status="completed", observed_event=observation,
                              updated_at=self.clock()), lease=lease)
        self.pages[0]["items"] = [e for e in self.pages[0]["items"] if e["id"] != op.event_id]
        self.pages[0]["items"].append(event(op.event_id, start=op.target.interval.start, end=op.target.interval.end,
            etag=observation.etag, extendedProperties={"private": ownership_properties(marker)}))
        return state


def codes(result):
    return {f.code for f in result.findings}


def test_semantically_unchanged_new_refs_prior_run_lineage_and_exact_payload():
    async def scenario():
        h = Setup()
        await h.prepare()
        h.rows[0][1:3] = ["new harmless course", "new harmless title"]
        result = await h.check()
        assert result.status == "ready" and not result.findings
        assert result.plan == h.plan and result.plan.payload.source_references[0].run_id == "run-1"
        assert result.execution.run_id == "run-2"
        assert all(r.run_id == "run-2" and r.read_at == h.now for r in result.source_references)
        assert result.source_references[0].snapshot_id != h.original_a.source.reference.snapshot_id
        assert len(h.calls) == 1 and h.sheet_calls == 1
        assert h.calls[0]["timeMin"] == h.horizon.start.isoformat()
        assert all(s.status == "pending" for s in result.execution.operations)
    run(scenario())


@pytest.mark.parametrize("change", ["deadline", "work", "busy", "all_day", "recurring"])
def test_relevant_change_returns_new_evidence_without_patching_approval(change):
    async def scenario():
        h = Setup()
        await h.prepare()
        if change == "deadline":
            h.rows[0][3] = (NOW + timedelta(hours=4)).isoformat()
        elif change == "work":
            h.rows[0][4] = 75
        else:
            target = h.plan.payload.blocks[0].interval
            raw = event("new-event", start=target.start)
            if change == "all_day":
                raw = event("all-day", start={"date": "2026-10-17"}, end={"date": "2026-10-18"})
            elif change == "recurring":
                raw.update(recurringEventId="series", originalStartTime={"dateTime": target.start.isoformat()})
            h.pages[0]["items"].append(raw)
        result = await h.check()
        assert result.status == "replan_required"
        assert ("preflight_assignments_changed" if change in ("deadline", "work") else "preflight_new_busy_conflict") in codes(result)
        assert result.plan == h.plan and len(result.source_references) == 2
        with h.request("backend") as ctx:
            assert (await h.approvals.get(ctx, plan_reference(h.plan))).plan == plan_reference(h.plan)
    run(scenario())


def test_new_external_busy_is_detected_despite_unchanged_owned_etag():
    async def scenario():
        h = Setup()
        await h.prepare(update=True)
        assert h.plan.payload.operations[0].kind == "update"
        old_etag = h.pages[0]["items"][0]["etag"]
        target = h.plan.payload.operations[0].target.interval
        h.pages[0]["items"].append(event("new-busy", start=target.start))
        result = await h.check()
        assert result.status == "replan_required" and "preflight_new_busy_conflict" in codes(result)
        assert h.pages[0]["items"][0]["etag"] == old_etag
        assert "preflight_expected_event_version_changed" not in codes(result)
    run(scenario())


@pytest.mark.parametrize("case", ["missing", "revoked", "expired", "superseded", "tampered", "modified_payload", "new_key"])
def test_rejects_consent_integrity_and_key_before_source_io(case):
    async def scenario():
        h = Setup()
        await h.prepare(approve=case != "missing")
        if case == "tampered":
            path = _plan_path(h.plan.payload.owner_id, h.plan.payload.calendar_id, "t18-plan", 1)
            h.documents.data[path]["body"] += b" "
        elif case == "modified_payload":


            replacement = PlanVersion.seal(changed(h.plan.payload, created_at=NOW + timedelta(microseconds=1)))
            path = _plan_path(h.plan.payload.owner_id, h.plan.payload.calendar_id, "t18-plan", 1)
            h.documents.data[path] = _encode("plan", replacement)
        elif case == "expired":
            h.now = NOW + timedelta(minutes=10)
        elif case in ("revoked", "superseded"):
            with h.request("backend") as ctx:
                if case == "revoked":
                    with h.approvals.ui_action(ctx, plan_reference(h.plan), "synthetic-revoke") as action:
                        await h.approvals.invalidate(ctx, plan_reference(h.plan), reason="revoked", at=action.at)
                else:

                    other = await h.seed(version=2)
                    await h.plans.set_current(ctx, plan_reference(other), expected=plan_reference(h.plan))
        elif case == "new_key":
            with h.request() as ctx:
                await h.executions.bind_request(ctx, request_key="original-key", plan=plan_reference(h.plan), at=h.clock())
        with pytest.raises(BindingConflict):
            await h.check()
        assert h.sheet_calls == 0 and not h.calls
    run(scenario())


@pytest.mark.parametrize("boundary", ["backend", "agent", "dto", "copy", "expired"])
def test_only_active_same_policy_mcp_capability(boundary):
    async def scenario():
        h = Setup()
        await h.prepare()
        with h.request() as ctx:
            lease = await h.claim(ctx)
        if boundary in ("backend", "agent"):
            with h.request(boundary) as wrong, pytest.raises(AccessDenied):
                await h.service.check(wrong, h.request_input, lease)
        elif boundary == "expired":
            with pytest.raises(AccessDenied):
                await h.service.check(ctx, h.request_input, lease)
        else:
            with h.request() as active, pytest.raises(AccessDenied):
                await h.service.check(active.as_port_context() if boundary == "dto" else copy(active.as_port_context()), h.request_input, lease)
        assert h.sheet_calls == 0 and not h.calls
    run(scenario())


@pytest.mark.parametrize("case", ["pages", "work", "deadline", "coverage"])
def test_incomplete_source_never_proves_free_time(case):
    async def scenario():
        h = Setup()
        await h.prepare()
        if case == "pages":
            h.reader.max_pages = 1
            h.pages[0]["nextPageToken"] = "more"
        elif case in ("work", "deadline"):
            h.rows[0][4 if case == "work" else 3] = ""
        else:
            original = h.reader.read
            async def limited(context, horizon):
                return await original(context, Interval(start=horizon.start, end=horizon.end - timedelta(hours=1)))
            h.reader.read = limited
        result = await h.check()
        assert result.status == "replan_required" and "preflight_incomplete_sources" in codes(result)
    run(scenario())


@pytest.mark.parametrize("when", ["before", "after_assignments", "after_calendar", "consent_expiry", "consent_revocation"])
def test_lease_and_consent_rechecked_across_awaits(when):
    async def scenario():
        h = Setup()
        await h.prepare()
        with h.request(run="run-2") as ctx:
            lease = await h.claim(ctx, seconds=120)
            if when == "before":
                await h.coordination.release(ctx, lease)
            elif when == "after_assignments":
                h.sheet_hook = lambda: setattr(h, "now", h.now + timedelta(seconds=121))
            elif when == "after_calendar":
                h.calendar_hook = lambda: setattr(h, "now", h.now + timedelta(seconds=121))
            elif when == "consent_expiry":

                await h.coordination.release(ctx, lease)
                h.now = NOW + timedelta(minutes=9, seconds=59)
                lease = await h.claim(ctx)
                h.sheet_hook = lambda: setattr(h, "now", NOW + timedelta(minutes=10))
            else:
                original = h.reader.read
                async def revoke(context, horizon):
                    result = await original(context, horizon)
                    with h.request("backend") as backend:
                        with h.approvals.ui_action(backend, plan_reference(h.plan), "revoke-during-read") as action:
                            await h.approvals.invalidate(backend, plan_reference(h.plan), reason="revoked", at=action.at)
                    return result
                h.reader.read = revoke
            with pytest.raises(BindingConflict):
                await h.service.check(ctx, h.request_input, lease)
            state = await h.executions.get(ctx, "apply-key")
            assert all(s.status == "pending" and s.attempts == 0 for s in state.operations)
    run(scenario())


@pytest.mark.parametrize("case", ["clean", "external_conflict", "edited_completed", "new_key"])
def test_partial_continuation_projects_only_durable_completed_effects(case):
    async def scenario():
        h = Setup()
        await h.prepare(two=True)
        with h.request(run="first-execution") as ctx:
            lease = await h.claim(ctx)
            state = await h.begin(ctx, lease)
            state = await h.dispatch(ctx, lease, state)
            state = await h.complete(ctx, lease, state)
            assert state.status == "partial"
            if case == "external_conflict":
                h.pages[0]["items"].append(event("new-busy", start=h.plan.payload.blocks[0].interval.start))
            elif case == "edited_completed":
                h.pages[0]["items"][0]["start"]["dateTime"] = (NOW + timedelta(hours=4)).isoformat()
                h.pages[0]["items"][0]["end"]["dateTime"] = (NOW + timedelta(hours=5)).isoformat()
            elif case == "new_key":
                h.request_input = changed(h.request_input, request_key="hide-partial")
            h.calls.clear()
            if case == "new_key":
                with pytest.raises(BindingConflict, match="plan_already_bound"):
                    await h.service.check(ctx, h.request_input, lease)
            else:
                with h.request(run="recovery-request") as recovery:
                    result = await h.service.check(recovery, h.request_input, lease)
                assert result.execution == state and result.execution.run_id == "first-execution"
                assert result.status == ("ready" if case == "clean" else "replan_required")
                if case == "clean":
                    assert not result.findings
                else:
                    assert codes(result)
            assert (await h.executions.get(ctx, "apply-key")).operations[0].status == "completed"
    run(scenario())


@pytest.mark.parametrize("case", ["in_flight", "unknown"])
def test_unresolved_outcome_never_becomes_retry_permission(case):
    async def scenario():
        h = Setup()
        await h.prepare()
        with h.request() as ctx:
            lease = await h.claim(ctx)
            state = await h.begin(ctx, lease)
            state = await h.dispatch(ctx, lease, state)
            if case == "unknown":
                state = await h.executions.record_operation(ctx, request_key="apply-key", expected_revision=state.revision,
                    operation=changed(state.operations[0], status="unknown", error_code="synthetic_timeout"), lease=lease)
                with pytest.raises(BindingConflict, match="calendar_writer_recovery_required"):
                    await h.service.check(ctx, h.request_input, lease)
            else:
                result = await h.service.check(ctx, h.request_input, lease)
                assert result.status == "reconciliation_required"
                assert result.observations[0].comparison == "unknown"
            assert await h.executions.get(ctx, "apply-key") == state
    run(scenario())


def test_pending_update_exact_version_and_original_block_time_are_checked():
    async def scenario():
        h = Setup()
        await h.prepare(update=True)
        h.pages[0]["items"][0]["etag"] = '"user-new-version"'
        result = await h.check()
        assert result.status == "replan_required" and "preflight_expected_event_version_changed" in codes(result)
    run(scenario())


def test_unchanged_update_and_partial_completed_update_projection():
    async def scenario():
        h = Setup()
        await h.prepare(two=True, update=True)
        with h.request() as ctx:
            lease = await h.claim(ctx)
            result = await h.service.check(ctx, h.request_input, lease)
            assert result.status == "ready"
            state = await h.begin(ctx, lease)
            index = next(i for i, op in enumerate(h.plan.payload.operations) if op.kind == "update")
            state = await h.dispatch(ctx, lease, state, index)
            state = await h.complete(ctx, lease, state, index)
            h.calls.clear()
            result = await h.service.check(ctx, h.request_input, lease)
            assert result.status == "ready" and not result.findings
            assert result.execution == state and state.status == "partial"
    run(scenario())


def test_complete_replay_is_only_already_applied_and_never_verified():
    async def scenario():
        h = Setup()
        await h.prepare()
        with h.request() as ctx:
            lease = await h.claim(ctx)
            state = await h.begin(ctx, lease)
            state = await h.dispatch(ctx, lease, state)
            state = await h.complete(ctx, lease, state)
            h.calls.clear()
            result = await h.service.check(ctx, h.request_input, lease)
            assert result.status == "already_applied" and result.execution.status == "applied_unverified"
            assert result.execution == state and not result.findings
    run(scenario())


def test_inflight_actual_target_observation_is_reconciliation_not_succeeded():
    async def scenario():
        h = Setup()
        await h.prepare()
        with h.request() as ctx:
            lease = await h.claim(ctx)
            state = await h.begin(ctx, lease)
            state = await h.dispatch(ctx, lease, state)
            record, observation = reservation(h.plan), observed(h.plan)
            await h.registry.reserve(ctx, record)
            await h.registry.confirm(ctx, record, observed=observation, at=h.clock())
            h.pages[0]["items"].append(event(record.event_id, start=observation.interval.start,
                end=observation.interval.end, etag=observation.etag,
                extendedProperties={"private": ownership_properties(observation.campuspulse_marker)}))
            result = await h.service.check(ctx, h.request_input, lease)
            assert result.status == "reconciliation_required"
            assert result.observations[0].comparison == "matched"
            assert await h.executions.get(ctx, "apply-key") == state
            assert "preflight_availability_changed" in codes(result)
    run(scenario())


def test_bound_request_key_cannot_change_exact_plan():
    async def scenario():
        h = Setup()
        await h.prepare()
        other = await h.seed(version=2)
        with h.request() as ctx:
            await h.executions.bind_request(ctx, request_key="apply-key", plan=plan_reference(other), at=h.clock())
        with pytest.raises(BindingConflict, match="request_key_payload_conflict"):
            await h.check()
        assert not h.calls and not h.sheet_calls
    run(scenario())


def test_elapsed_completed_calendar_write_is_not_completed_assignment_work():
    async def scenario():
        h = Setup()
        h.window_start = timedelta(minutes=15)
        await h.prepare(two=True, approve=False)
        h.now = NOW + timedelta(minutes=10)
        with h.request("backend") as ctx:
            await h.approvals.approve(ctx, plan_reference(h.plan), "delayed-synthetic-click")
        with h.request() as ctx:
            lease = await h.claim(ctx)
            state = await h.begin(ctx, lease)

            index = min(range(len(h.plan.payload.operations)),
                        key=lambda i: h.plan.payload.operations[i].target.interval.start)
            state = await h.dispatch(ctx, lease, state, index)
            state = await h.complete(ctx, lease, state, index)
            await h.coordination.release(ctx, lease)
            h.now = NOW + timedelta(minutes=16)
            lease = await h.claim(ctx)
            h.calls.clear()
            result = await h.service.check(ctx, h.request_input, lease)
            assert result.status == "replan_required" and "preflight_target_started" in codes(result)
            assert result.execution == state and state.operations[index].status == "completed"
    run(scenario())


@pytest.mark.parametrize("case", ["read_failure", "old_reference", "unpersisted", "ledger_race", "forged_lease", "foreign_policy"])
def test_untrusted_or_racing_evidence_fails_closed(case):
    async def scenario():
        h = Setup()
        await h.prepare()
        with h.request() as ctx:
            lease = await h.claim(ctx)
            expected = BindingConflict
            if case == "read_failure":
                h.pages = [{}]
                expected = IncompleteRead
            elif case == "old_reference":
                async def old_source(context, horizon):
                    return h.original_a
                h.assignment_reader.read = old_source
            elif case == "unpersisted":
                original = h.assignment_reader.read
                async def fabricated(context, horizon):
                    result = await original(context, horizon)
                    return changed(result, source=changed(result.source, reference=changed(
                        result.source.reference, snapshot_id="unpersisted-snapshot")))
                h.assignment_reader.read = fabricated
            elif case == "ledger_race":
                original = h.reader.read
                async def race(context, horizon):
                    result = await original(context, horizon)
                    state = await h.executions.get(context, "apply-key")
                    await h.executions.compare_and_set(context, expected_revision=state.revision,
                        execution=changed(state, revision=state.revision + 1, status="approved"), lease=lease)
                    return result
                h.reader.read = race
            elif case == "forged_lease":
                lease = changed(lease, generation=lease.generation + 1)
            else:
                expected = AccessDenied
                with Setup().request() as foreign, pytest.raises(expected):
                    await h.service.check(foreign, h.request_input, lease)
                return
            with pytest.raises(expected):
                await h.service.check(ctx, h.request_input, lease)
    run(scenario())


def test_harmless_calendar_metadata_order_and_half_open_adjacency():
    async def scenario():
        h = Setup()
        h.pages = [page(event("external", start=NOW + timedelta(hours=7)))]
        await h.prepare()

        h.pages[0]["items"][0].update(etag='"new-meta"', htmlLink="https://calendar.google.com/new-link")
        target = h.plan.payload.blocks[0].interval
        h.pages[0]["items"].append(event("transparent", start=target.start, transparency="transparent"))
        result = await h.check()
        assert result.status == "ready"
        from campuspulse_recovery.application.preflight import _busy

        from campuspulse_recovery.domain.models import CalendarEvent
        assert _busy((CalendarEvent(event_id="one", interval=target),
            CalendarEvent(event_id="two", interval=Interval(start=target.end, end=target.end + timedelta(hours=1)))),
            h.horizon) == ((target.start, target.end + timedelta(hours=1)),)
    run(scenario())


def test_constructor_rejects_foreign_authority_and_fake_approval_guard():
    h = Setup()
    for target, field, value in ((h.snapshots, "policy", Setup().policy),
                                 (h.executions, "approval_guard", SimpleNamespace())):
        original = getattr(target, field)
        setattr(target, field, value)
        with pytest.raises(BindingConflict):
            PreflightService(h.plans, h.snapshots, h.approvals, h.executions, h.coordination,
                h.registry, h.assignment_reader, h.reader, h.policy, clock=h.clock)
        setattr(target, field, original)


@contextmanager
def shared_clock_request(h, *, boundary="mcp", lifetime=timedelta(minutes=5)):
    """Real server-minted T04 capability; identity remains explicitly synthetic.

    The changing clock is shared by context, consent, coordination, readers and
    preflight. No service clock/method or storage implementation is replaced.
    """
    now = h.clock()
    caller = AuthenticatedCaller(principal_id=(h.config.student.allowed_signin_principal
        if boundary == "backend" else h.config.identities.agent_service_account),
        session_id="t18-round1-session", run_id="t18-round1-" + boundary,
        authenticated_at=now, expires_at=now + lifetime)
    binding = ServerRunBinding(owner_id=h.config.student.trusted_user_id,
        signin_principal_id=h.config.student.allowed_signin_principal,
        session_id=caller.session_id, run_id=caller.run_id,
        config_revision=h.config.config_revision, expires_at=now + lifetime)
    factory = RequestContextFactory(h.policy, boundary=boundary, clock=h.clock,
        authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
        bindings=SimpleNamespace(resolve=lambda **k: binding))
    with factory.request(object()) as context:
        token = h.current.set(context)
        try:
            yield context
        finally:
            h.current.reset(token)


@pytest.mark.parametrize("boundary", ["lease", "approval", "target", "context"])
@pytest.mark.parametrize("delivery", ["before", "equal", "after"])
def test_final_delivery_uses_one_current_time_after_last_actual_document_read(boundary, delivery):
    """Delay only the last real ledger document delivery, never the guards.

    Before-fix lease/approval/target equal+after cases FAIL; context expiry is
    an existing correct control. Both source provenance and actual persisted
    immutable intent remain intact while the server clock crosses a boundary.
    """
    async def scenario():
        from campuspulse_recovery.application.proposals import _validate_executable
        from campuspulse_recovery.storage.operations import _Ledger, _request_path
        from campuspulse_recovery.storage.plans import _decode

        h = Setup()
        await h.prepare(approve=boundary != "target")
        original_input = SolveRepairInput(assignments=h.original_a, calendar=h.original_c,
            constraints=h.plan.payload.constraints, now=NOW,
            plan_id=h.plan.payload.plan_id, version=h.plan.payload.version)
        original_result = solve_repair(original_input)
        _validate_executable(original_input, original_result)
        assert (original_result.operations, original_result.blocks, original_result.allocations) == (
            h.plan.payload.operations, h.plan.payload.blocks, h.plan.payload.allocations)
        first_target = min(op.target.interval.start for op in h.plan.payload.operations)
        if boundary == "approval":
            h.now = NOW + timedelta(minutes=9, seconds=59)
        elif boundary == "target":
            h.now = first_target - timedelta(seconds=1)
            with shared_clock_request(h, boundary="backend") as backend:

                await h.approvals.approve(backend, plan_reference(h.plan), "synthetic-late-first-consent")
        lifetime = timedelta(seconds=1) if boundary == "context" else timedelta(minutes=5)
        with shared_clock_request(h, lifetime=lifetime) as context:
            lease = await h.claim(context, seconds=1 if boundary == "lease" else 120)
            approval = await h.approvals.get(context, plan_reference(h.plan))
            limit = {"lease": lease.expires_at, "approval": approval.expires_at,
                     "target": first_target, "context": context.expires_at}[boundary]
            delivered_at = limit + {"before": -timedelta(microseconds=1),
                                    "equal": timedelta(0), "after": timedelta(seconds=1)}[delivery]
            ledger_path = _request_path(h.config.student.trusted_user_id,
                h.config.student.calendar_id, h.request_input.request_key)
            original_get = h.documents.get
            deliveries = []
            ledger_reads = 0

            async def delayed_get(path):
                nonlocal ledger_reads
                document = await original_get(path)
                if path == ledger_path:
                    ledger_reads += 1
                    if ledger_reads == 3:
                        before = deepcopy(document)
                        deliveries.append(("fetched", h.clock()))
                        await asyncio.sleep(0)
                        h.now = delivered_at
                        deliveries.append(("delivered", h.clock()))
                        assert before == document
                return document

            h.documents.get = delayed_get
            if boundary in ("lease", "approval", "context") and delivery != "before":
                expected = AccessDenied if boundary == "context" else BindingConflict
                code = {"lease": "calendar_lease_stale", "approval": "active_exact_ui_approval_required",
                        "context": "inactive_request_context"}[boundary]
                with pytest.raises(expected, match=code):
                    await h.service.check(context, h.request_input, lease)
            else:
                result = await h.service.check(context, h.request_input, lease)
                assert result.checked_at == delivered_at == h.clock()
                assert result.plan == h.plan and len(result.source_references) == 2
                assert all(ref.run_id == context.trusted.run_id
                           and ref.read_at < delivered_at for ref in result.source_references)
                assert approval.active_at(result.checked_at) and result.checked_at < lease.expires_at
                if boundary == "target" and delivery != "before":
                    assert result.status == "replan_required" and "preflight_target_started" in codes(result)
                else:
                    assert result.status == "ready" and not result.findings
            assert ledger_reads == 3 and len(deliveries) == 2
            assert deliveries[0][1] < deliveries[1][1] == delivered_at
            assert h.sheet_calls == 1 and len(h.calls) == 1


            state = _decode(h.documents.data[ledger_path], "execution", _Ledger).execution
            assert state.plan == plan_reference(h.plan) and state.revision == 0
            assert all(s.status == "pending" and s.attempts == 0 for s in state.operations)
            assert h.clock() == delivered_at
            if boundary != "context" or delivery == "before":
                context.require_active()
            else:
                with pytest.raises(AccessDenied, match="inactive_request_context"):
                    context.require_active()
    run(scenario())
