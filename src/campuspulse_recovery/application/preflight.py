"""T18 read-only source reconciliation, never a Calendar dispatch permit.

The caller acquires a durable calendar claim first. Dependencies are installed
by trusted server startup; there are no model-supplied sources, operations,
approval DTOs or policies. T19 must still claim each dispatch with the same-tx
T16 approval guard and recheck the lease immediately before Calendar I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from ..domain.models import (
    ApplyPlanInput, AssignmentsSnapshot, CalendarEvent, CalendarSnapshot, Contract,
    ExecutionState, ExecutionStatus, Finding, Identifier, Instant, Interval, OperationStatus,
    PlanVersion, SourceReference, calendar_event_id,
)
from ..domain.ports import BindingConflict, CalendarLease
from ..domain.time_rules import instant
from ..integrations.assignments import AssignmentsReader
from ..integrations.calendar_read import CalendarReader
from ..integrations.calendar_write import TargetComparison, compare_target
from ..security.context import AccessDenied, TrustedRequestContext
from ..security.resources import ResourcePolicy
from ..storage.leases import FirestoreCalendarCoordination
from ..storage.operations import FirestoreExecutionStore
from ..storage.owned_blocks import FirestoreOwnedBlockRegistry
from ..storage.plans import FirestorePlanStore, plan_reference
from ..storage.snapshots import FirestoreSnapshotStore
from ..web.approval import FirestoreApprovalStore


class OperationObservation(Contract):
    operation_id: Identifier
    comparison: TargetComparison
    observed_event: CalendarEvent | None = None


class PreflightResult(Contract):
    """Internal observations only. Even `ready` cannot authorize Calendar I/O.

    replan_required retains the old immutable plan and durable completed work;
    source_references identify the actual newly saved evidence for a NEW plan.
    Reconciliation observations do not change the operation ledger or establish
    absence of an outstanding writer. Final success requires T20 verification.
    """

    status: Literal["ready", "replan_required", "reconciliation_required", "already_applied"]
    plan: PlanVersion
    execution: ExecutionState
    checked_at: Instant
    source_references: tuple[SourceReference, ...]
    findings: tuple[Finding, ...] = ()
    observations: tuple[OperationObservation, ...] = ()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise BindingConflict(code)


def _covers(coverage: Interval | None, horizon: Interval) -> bool:
    return coverage is not None and coverage.start <= horizon.start and coverage.end >= horizon.end


def _work(snapshot: AssignmentsSnapshot):



    return {a.assignment_id: (None if a.deadline is None else a.deadline.at, a.remaining_minutes)
            for a in snapshot.assignments}


def _busy(events, horizon: Interval):
    """UTC half-open union: event order, IDs, links and etags are irrelevant."""
    spans = sorted((max(e.interval.start, horizon.start), min(e.interval.end, horizon.end))
                   for e in events if e.busy and e.interval is not None
                   and e.interval.start < horizon.end and horizon.start < e.interval.end)
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


class PreflightService:
    def __init__(self, plans: FirestorePlanStore, snapshots: FirestoreSnapshotStore,
                 approvals: FirestoreApprovalStore, executions: FirestoreExecutionStore,
                 coordination: FirestoreCalendarCoordination, registry: FirestoreOwnedBlockRegistry,
                 assignments: AssignmentsReader, calendar: CalendarReader, policy: ResourcePolicy,
                 *, clock: Callable[[], datetime]):

        documents = plans.documents
        for store in (plans, snapshots, approvals, executions, coordination, registry):
            _require(store.documents is documents and store.policy is policy,
                     "preflight_store_authority_mismatch")
        _require(executions.coordination is coordination, "preflight_coordination_mismatch")
        guard = executions.approval_guard
        _require(getattr(guard, "store", None) is approvals
                 and getattr(guard, "documents", None) is documents,
                 "preflight_transactional_approval_guard_required")
        for reader in (assignments, calendar):
            _require(reader.clients.policy is policy and reader.snapshots is snapshots,
                     "preflight_reader_authority_mismatch")
        _require(calendar.registry is registry, "preflight_registry_mismatch")
        self.plans, self.snapshots, self.approvals = plans, snapshots, approvals
        self.executions, self.coordination, self.registry = executions, coordination, registry
        self.assignments, self.calendar, self.policy, self.clock = assignments, calendar, policy, clock

    def _mcp(self, context):
        trusted = self.policy.require_context(context)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        return trusted

    def _now(self):
        return instant(self.clock())

    async def _consent(self, context, reference):



        value = await self.approvals.get(context, reference)
        _require(value is not None and value.active_at(self._now()),
                 "active_exact_ui_approval_required")
        return await self.approvals.require_active(context, reference, self._now())

    async def check(self, context: TrustedRequestContext, request: ApplyPlanInput,
                    lease: CalendarLease) -> PreflightResult:
        self._mcp(context)
        request = ApplyPlanInput.model_validate(request)
        lease = CalendarLease.model_validate(lease)
        plan = PlanVersion.model_validate(await self.plans.get(context, request.plan_id, request.version))
        self.policy.require_plan(context, plan)
        reference = plan_reference(plan)
        await self._consent(context, reference)


        await self.executions.bind_request(context, request_key=request.request_key,
                                           plan=reference, at=self._now())
        original_a, original_c = await self.snapshots.get_for_plan(context, reference)
        await self.coordination.assert_current(context, lease, now=self._now())
        execution = await self.executions.get(context, request.request_key)
        horizon = plan.payload.constraints.horizon
        started = self._now()
        fresh_a = AssignmentsSnapshot.model_validate(await self.assignments.read(context, horizon))
        await self.coordination.assert_current(context, lease, now=self._now())
        fresh_c = CalendarSnapshot.model_validate(await self.calendar.read(context, horizon))
        self._mcp(context)


        for fresh, old in ((fresh_a, original_a), (fresh_c, original_c)):
            self.policy.require_source(context, fresh.source.reference)
            _require(started <= fresh.source.reference.read_at <= self._now()
                     and fresh.source.reference.snapshot_id != old.source.reference.snapshot_id,
                     "preflight_fresh_source_required")
        _require(await self.snapshots.get_assignments(context, fresh_a.source.reference) == fresh_a
                 and await self.snapshots.get_calendar(context, fresh_c.source.reference) == fresh_c,
                 "preflight_persisted_source_mismatch")


        after = await self.executions.get(context, request.request_key)
        _require(after == execution, "preflight_execution_changed")
        await self._consent(context, reference)
        await self.coordination.assert_current(context, lease, now=self._now())
        self._mcp(context)
        findings, observations = await self._compare(context, plan, execution,
            original_a, original_c, fresh_a, fresh_c)

        approval = await self._consent(context, reference)
        await self.coordination.assert_current(context, lease, now=self._now())
        _require(await self.executions.get(context, request.request_key) == execution,
                 "preflight_execution_changed")



        self._mcp(context)
        checked_at = self._now()
        _require(approval.active_at(checked_at), "active_exact_ui_approval_required")
        _require(lease.state == "active" and lease.acquired_at <= checked_at < lease.expires_at,
                 "calendar_lease_stale")
        if any(target.interval.start <= checked_at for target in plan.payload.blocks):

            findings.append(Finding(code="preflight_target_started"))
        states = {state.operation_id: state for state in execution.operations}
        for op in plan.payload.operations:
            if (op.kind == "update" and states[op.operation_id].status in (
                    OperationStatus.PENDING, OperationStatus.FAILED) and op.before.start <= checked_at):
                findings.append(Finding(code="preflight_original_block_started", operation_id=op.operation_id))
        unresolved = any(s.status in (OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT)
                         for s in execution.operations)
        if unresolved:
            status = "reconciliation_required"
        elif findings:
            status = "replan_required"
        elif all(s.status == OperationStatus.COMPLETED for s in execution.operations):
            status = "already_applied"
        else:
            status = "ready"
        return PreflightResult(status=status, plan=plan, execution=execution,
            checked_at=checked_at, source_references=(fresh_a.source.reference, fresh_c.source.reference),
            findings=tuple(findings), observations=tuple(observations))

    async def _compare(self, context, plan, execution, old_a, old_c, fresh_a, fresh_c):
        findings, observations = [], []

        def finding(code, op=None):
            value = Finding(code=code, operation_id=None if op is None else op.operation_id)
            if value not in findings:
                findings.append(value)

        horizon = plan.payload.constraints.horizon
        if execution.status in (ExecutionStatus.REJECTED, ExecutionStatus.FAILED):
            finding("preflight_execution_terminal")
        if (not all(s.source.complete for s in (old_a, old_c, fresh_a, fresh_c))
                or not old_c.recurrence_expanded or not fresh_c.recurrence_expanded
                or not _covers(old_c.source.covered_interval, horizon)
                or not _covers(fresh_c.source.covered_interval, horizon)
                or any(s.source.covered_interval is not None and not _covers(s.source.covered_interval, horizon)
                       for s in (old_a, fresh_a))):
            finding("preflight_incomplete_sources")
            return findings, observations
        if _work(old_a) != _work(fresh_a):
            finding("preflight_assignments_changed")
        if any(a.deadline is None or a.remaining_minutes is None for a in fresh_a.assignments):
            finding("preflight_unresolved_work_or_deadline")
            return findings, observations
        current_events = {e.event_id: e for e in fresh_c.events}
        old_blocks = {b.block_id: b for b in old_c.owned_blocks}
        current_blocks = {b.block_id: b for b in fresh_c.owned_blocks}
        projected_events = {e.event_id: e for e in old_c.events}
        ops = {op.target.block_id: op for op in plan.payload.operations}
        states = {s.operation_id: s for s in execution.operations}
        for op in plan.payload.operations:
            state = states[op.operation_id]
            record = await self.registry.get_by_block(context, op.target.block_id)
            by_event = await self.registry.get_by_event(context, op.event_id)
            _require(record == by_event, "preflight_registry_binding_mismatch")
            if record is not None:
                _require((record.owner_id, record.calendar_id, record.block_id, record.assignment_id, record.event_id)
                         == (plan.payload.owner_id, plan.payload.calendar_id, op.target.block_id,
                             op.target.assignment_id, op.event_id), "preflight_registry_binding_mismatch")
            observed = current_events.get(op.event_id)
            if state.status in (OperationStatus.IN_FLIGHT, OperationStatus.UNKNOWN):
                finding("preflight_operation_reconciliation_required", op)



                comparison = (TargetComparison.UNKNOWN if record is None or observed is None else
                              compare_target(op, record, observed, read_complete=True))
                observations.append(OperationObservation(operation_id=op.operation_id,
                    comparison=comparison, observed_event=observed))
                continue
            if state.status == OperationStatus.FAILED:
                finding("preflight_operation_failed", op)
            if op.kind == "update":
                old = old_blocks.get(op.target.block_id)
                if (old is None or (old.event_id, old.assignment_id, old.interval, old.etag) != (
                        op.event_id, op.target.assignment_id, op.before, op.expected_etag)):
                    finding("preflight_original_update_mismatch", op)
            elif op.event_id in projected_events:
                finding("preflight_original_create_collision", op)
            if state.status == OperationStatus.COMPLETED:
                if (record is None or record.state != "confirmed"
                        or compare_target(op, record, state.observed_event) != TargetComparison.MATCHED
                        or compare_target(op, record, observed) != TargetComparison.MATCHED
                        or op.target.block_id not in current_blocks):
                    finding("preflight_completed_target_changed", op)


                projected_events[op.event_id] = state.observed_event
            elif op.kind == "update":
                if (record is None or record.state != "confirmed"
                        or compare_target(op, record, observed) != TargetComparison.NO_EFFECT
                        or op.target.block_id not in current_blocks):
                    finding("preflight_expected_event_version_changed", op)
            else:
                if observed is not None or (record is not None and (
                        record.state != "reserved" or record.intent_plan != plan_reference(plan)
                        or record.intent_operation_id != op.operation_id)):
                    finding("preflight_create_collision", op)
        if _busy(projected_events.values(), horizon) != _busy(fresh_c.events, horizon):
            finding("preflight_availability_changed")
        targets = {b.block_id: b for b in plan.payload.blocks}
        for bid, old in old_blocks.items():
            if bid in ops:
                continue
            actual = current_blocks.get(bid)
            if (actual is None or (actual.event_id, actual.assignment_id, actual.interval) != (
                    old.event_id, old.assignment_id, old.interval)):
                finding("preflight_preserved_block_changed")
            if old.interval.end > plan.payload.created_at and old.interval.start < horizon.end:
                target = targets.get(bid)
                if target is None or (target.assignment_id, target.interval) != (old.assignment_id, old.interval):
                    finding("preflight_preserved_identity_mismatch")
        assignments = {a.assignment_id: a for a in fresh_a.assignments}
        planned_ids = {calendar_event_id(plan.payload.owner_id, plan.payload.calendar_id, bid) for bid in targets}
        external_busy = [e for e in fresh_c.events if e.event_id not in planned_ids and e.busy]
        for target in targets.values():
            assignment = assignments.get(target.assignment_id)
            if assignment is None or target.interval.end > assignment.deadline.at:
                finding("preflight_target_deadline_changed")
            if target.block_id not in old_blocks and target.block_id not in ops:
                finding("preflight_target_without_operation")
            if any(e.interval is not None and target.interval.start < e.interval.end
                   and e.interval.start < target.interval.end for e in external_busy):
                finding("preflight_new_busy_conflict")
        allocations = {a.assignment_id: a for a in plan.payload.allocations}
        if set(allocations) != set(assignments):
            finding("preflight_assignment_set_changed")
        for aid, assignment in assignments.items():
            allocation = allocations.get(aid)
            if allocation is None:
                continue
            deferred = assignment.deadline.at > horizon.end
            if (allocation.remaining_minutes != assignment.remaining_minutes
                    or allocation.required_minutes != assignment.required_minutes
                    or allocation.deferred != deferred):
                finding("preflight_work_allocation_changed")
        return findings, observations
