"""Backend-only proposal boundary; publishing never approves or executes.

Startup injects a server-owned basis resolver (run records + planning policy).
There is deliberately no default policy inferred from AppConfig, model windows,
session state or a config revision. This module is not an agent/MCP tool.
Snapshots are loaded through the current-run repository API, never get_for_plan.
Later fresh preflight and final Calendar verification remain mandatory.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from ..domain.models import (
    AssignmentsSnapshot, CalendarSnapshot, Contract, Identifier, Instant,
    Operation, PlanPayload, PlanReference, PlanVersion, PlanningConstraints,
    PositiveInt, RepairResult, RepairStatus, SolveRepairInput, SourceReference,
    calendar_event_id, operation_id,
)
from ..domain.ports import BindingConflict
from ..domain.solver import solve_repair
from ..domain.time_rules import instant
from ..security.context import AccessDenied, TrustedRequestContext
from ..security.resources import ResourcePolicy


class ProposalBasis(Contract):
    """Internal server value, NOT request JSON or a capability.

    The resolver must load exact references/identity from its server-owned run
    record and constraints from a server policy source. Persist explicit now
    with that proposal identity: changing it is a different immutable proposal.
    Never construct this value from the model's SolveRepairInput. expected_current
    is the pointer observed by the backend when starting the proposal, not latest
    read opportunistically after solving. None explicitly means no current plan.
    """

    plan_id: Identifier
    version: PositiveInt
    assignments: SourceReference
    calendar: SourceReference
    constraints: PlanningConstraints
    now: Instant
    expected_current: PlanReference | None


class ProposalBasisSource(Protocol):
    async def resolve(self, context: TrustedRequestContext) -> ProposalBasis:
        """Resolve this active user's run + configured policy server-side.

        No browser/model basis, owner/session/run/working-window parameters.
        This injected implementation is trusted startup code, not an existing
        production policy resolver supplied by this module.
        """
        ...


class ProposalSnapshots(Protocol):
    async def get_assignments(self, context: TrustedRequestContext, reference: SourceReference) -> AssignmentsSnapshot: ...
    async def get_calendar(self, context: TrustedRequestContext, reference: SourceReference) -> CalendarSnapshot: ...


class ProposalPlans(Protocol):
    async def create(self, context: TrustedRequestContext, plan: PlanVersion) -> PlanReference: ...
    async def get(self, context: TrustedRequestContext, plan_id: str, version: int) -> PlanVersion: ...
    async def set_current(self, context: TrustedRequestContext, reference: PlanReference, *, expected: PlanReference | None) -> PlanReference: ...


class ProposalOutcome(Contract):
    """No saved plan identity/hash/diff is issued for non-actionable results."""

    result: RepairResult
    plan: PlanVersion | None = None

    @property
    def approval_ready(self) -> bool:
        return self.plan is not None


def _reject_unless(condition: bool, code: str) -> None:
    if not condition:
        raise BindingConflict(code)


def _validate_executable(request: SolveRepairInput, result: RepairResult) -> None:
    """Source-based hard checks independent of the solver's candidate checker.

    Includes actual source deadlines/busy, history/in-progress and deferred
    volume rules which PlanPayload's structural/hash validators cannot prove.
    No solver-private functions or self-reported allocation/status are authority.
    """
    result = RepairResult.model_validate(result)
    constraints, horizon = request.constraints, request.constraints.horizon
    _reject_unless(result.status in (RepairStatus.FEASIBLE, RepairStatus.OPTIMAL)
                   and bool(result.operations), "proposal_not_executable")
    _reject_unless(result.constraints == constraints and result.source_references == (
        request.assignments.source.reference, request.calendar.source.reference), "solver_provenance_mismatch")
    assignments = {a.assignment_id: a for a in request.assignments.assignments}
    owned = {b.block_id: b for b in request.calendar.owned_blocks}
    events = {e.event_id: e for e in request.calendar.events}
    mutable = {}
    for block in owned.values():
        if block.interval.end <= request.now:
            continue
        _reject_unless(block.interval.start > request.now, "proposal_in_progress_block")
        assignment = assignments.get(block.assignment_id)
        _reject_unless(assignment is not None and assignment.deadline is not None,
                       "proposal_owned_assignment_missing")
        event = events[block.event_id]
        _reject_unless(event.status == "confirmed" and event.all_day_start is None,
                       "proposal_unsupported_owned_event")
        if assignment.deadline.at > horizon.end:
            if block.interval.start >= horizon.end:
                continue
            _reject_unless(block.interval.end <= horizon.end, "proposal_deferred_crosses_horizon")
            _reject_unless(block.interval.minutes in (15, 30, 45, 60) and all(
                p.minute % 15 == 0 and p.second == 0 and p.microsecond == 0
                for p in (block.interval.start, block.interval.end)), "proposal_invalid_deferred_block")
        mutable[block.block_id] = block
    targets = {b.block_id: b for b in result.blocks}
    _reject_unless(set(mutable) <= set(targets), "proposal_dropped_owned_identity")
    busy = tuple(e.interval for e in events.values() if e.busy and e.interval is not None
                 and e.event_id not in {b.event_id for b in mutable.values()})
    for target in result.blocks:
        assignment = assignments.get(target.assignment_id)
        _reject_unless(assignment is not None and assignment.deadline is not None,
                       "proposal_target_assignment_missing")
        _reject_unless(target.interval.start >= request.now and target.interval.end <= assignment.deadline.at,
                       "proposal_deadline_or_past_target")
        _reject_unless(not any(target.interval.start < span.end and span.start < target.interval.end
                               for span in busy), "proposal_busy_conflict")
        previous = owned.get(target.block_id)
        if previous is not None:
            _reject_unless(target.block_id in mutable and previous.assignment_id == target.assignment_id,
                           "proposal_owned_identity_changed")
    allocations = {a.assignment_id: a for a in result.allocations}
    _reject_unless(set(allocations) == set(assignments), "proposal_allocation_set_mismatch")
    for aid, assignment in assignments.items():
        _reject_unless(assignment.deadline is not None and assignment.remaining_minutes is not None,
                       "proposal_missing_work_or_deadline")
        required = ((assignment.remaining_minutes + 14) // 15) * 15
        _reject_unless(not required or assignment.deadline.at > request.now, "proposal_overdue_work")
        deferred = assignment.deadline.at > horizon.end
        volume = sum(int(b.interval.minutes) for b in result.blocks if b.assignment_id == aid)
        demand = sum(int(b.interval.minutes) for b in mutable.values() if b.assignment_id == aid) if deferred else required
        if deferred:
            outside = sum(b.interval.minutes for b in owned.values()
                          if b.assignment_id == aid and b.interval.start >= horizon.end)
            _reject_unless(demand + outside <= required, "proposal_deferred_excess_work")
        actual = allocations[aid]
        _reject_unless((actual.remaining_minutes, actual.required_minutes, actual.padding_minutes,
                        actual.scheduled_minutes, actual.deferred) == (
                            assignment.remaining_minutes, required, required - assignment.remaining_minutes,
                            volume, deferred) and volume == demand, "proposal_work_mismatch")


    expected = []
    ref = request.calendar.source.reference
    for target in sorted(result.blocks, key=lambda b: b.block_id):
        old = mutable.get(target.block_id)
        if old is not None and old.interval == target.interval:
            continue
        eid = calendar_event_id(ref.owner_id, ref.resource_id, target.block_id)
        _reject_unless(old is not None or eid not in events, "proposal_create_identity_collision")
        expected.append(Operation(operation_id=operation_id(request.plan_id, request.version, len(expected)),
            kind="update" if old else "create", event_id=eid, target=target,
            expected_etag=old.etag if old else None, before=old.interval if old else None))
    _reject_unless(tuple(expected) == result.operations, "proposal_diff_mismatch")


class ProposalService:
    """One backend call; no sixth tool, implicit consent, or Calendar side effects.

    A failed current CAS can leave a valid immutable historical version; it
    raises, never returns an approval-ready current proposal. Retry the same
    basis after an unknown storage outcome; do not change timestamp/version.
    """

    def __init__(self, snapshots: ProposalSnapshots, plans: ProposalPlans, policy: ResourcePolicy,
                 basis: ProposalBasisSource, *, clock: Callable[[], datetime]) -> None:
        self.snapshots, self.plans, self.policy = snapshots, plans, policy
        self.basis, self.clock = basis, clock

    def _backend(self, context: TrustedRequestContext):
        trusted = self.policy.require_context(context)
        if context.boundary != "backend":
            raise AccessDenied("proposal_backend_required")
        return trusted

    async def propose(self, context: TrustedRequestContext, *,
                      candidate_input: SolveRepairInput | None = None,
                      candidate_result: RepairResult | None = None) -> ProposalOutcome:
        trusted = self._backend(context)
        basis = ProposalBasis.model_validate(await self.basis.resolve(context))
        self._backend(context)
        self.policy.require_source(context, basis.assignments)
        self.policy.require_source(context, basis.calendar)
        _reject_unless(basis.constraints.config_revision == trusted.config_revision
                       and basis.constraints.timezone == trusted.timezone, "proposal_policy_binding_mismatch")
        _reject_unless(basis.now <= instant(self.clock()), "proposal_future_time")
        assignments = AssignmentsSnapshot.model_validate(await self.snapshots.get_assignments(context, basis.assignments))
        calendar = CalendarSnapshot.model_validate(await self.snapshots.get_calendar(context, basis.calendar))
        self._backend(context)
        _reject_unless((assignments.source.reference, calendar.source.reference) == (
            basis.assignments, basis.calendar), "proposal_snapshot_reference_mismatch")
        _reject_unless(all(s.source.reference.read_at <= basis.now for s in (assignments, calendar)),
                       "proposal_future_source")
        request = SolveRepairInput(assignments=assignments, calendar=calendar,
            constraints=basis.constraints, now=basis.now, plan_id=basis.plan_id, version=basis.version)
        if candidate_input is not None:
            _reject_unless(SolveRepairInput.model_validate(candidate_input) == request, "proposal_input_mismatch")
        result = RepairResult.model_validate(solve_repair(request))
        if candidate_result is not None:
            _reject_unless(RepairResult.model_validate(candidate_result) == result, "proposal_result_mismatch")
        if result.status not in (RepairStatus.FEASIBLE, RepairStatus.OPTIMAL) or not result.operations:
            return ProposalOutcome(result=result)
        _reject_unless(assignments.source.complete and calendar.source.complete and calendar.recurrence_expanded,
                       "proposal_incomplete_source")
        _validate_executable(request, result)
        plan = PlanVersion.seal(PlanPayload(owner_id=trusted.owner_id, calendar_id=trusted.calendar_id,
            plan_id=basis.plan_id, version=basis.version, created_at=basis.now,
            source_references=result.source_references, constraints=request.constraints,
            operations=result.operations, blocks=result.blocks, allocations=result.allocations))
        self._backend(context)
        reference = await self.plans.create(context, plan)
        expected = PlanReference(plan_id=basis.plan_id, version=basis.version, payload_hash=plan.payload_hash)
        _reject_unless(reference == expected, "proposal_persisted_reference_mismatch")

        saved = PlanVersion.model_validate(await self.plans.get(context, basis.plan_id, basis.version))
        _reject_unless(saved == plan, "proposal_persisted_version_mismatch")
        self._backend(context)
        current = await self.plans.set_current(context, reference, expected=basis.expected_current)
        self._backend(context)
        _reject_unless(current == reference, "proposal_current_reference_mismatch")
        return ProposalOutcome(result=result, plan=saved)
