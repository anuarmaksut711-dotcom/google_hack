"""Versioned shared domain and tool DTOs; all external text is untrusted data.

Frozen DTOs are values, never authentication or proof that external I/O occurred.
Validate persisted/wire JSON with model_validate_json; do not use model_construct
or unchecked model_copy(update=...) at trust boundaries.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field,
    StrictBool, StrictInt, StringConstraints, model_validator,
)

from .time_rules import date_deadline, iana_zone, instant, iso_date, required_minutes

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f]+$")]
ResourceId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"^[^\s\x00-\x1f]+$")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
EventId = Annotated[str, StringConstraints(strict=True, min_length=5, max_length=1024, pattern=r"^[0-9a-v]+$")]
Instant = Annotated[datetime, BeforeValidator(instant)]
IanaZone = Annotated[str, AfterValidator(iana_zone)]
IsoDate = Annotated[date, BeforeValidator(iso_date)]
Minutes = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
SafeCode = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")
    schema_version: Literal["1.0"] = "1.0"


def canonical_json(value: Any) -> str:
    """Stable UTF-8 JSON: UTC timestamps, sorted keys, preserved array order."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def calendar_event_id(owner_id: str, calendar_id: str, block_id: str) -> str:
    binding = ["campuspulse-event-v1", owner_id, calendar_id, block_id]
    for item in binding[1:]:
        if not item or item != item.strip():
            raise ValueError("explicit event identity components required")
    return "cp" + canonical_hash(binding)


def operation_id(plan_id: str, version: int, ordinal: int) -> str:
    if not plan_id or type(version) is not int or version < 1 or type(ordinal) is not int or ordinal < 0:
        raise ValueError("invalid operation identity")
    return "op_" + canonical_hash(["campuspulse-operation-v1", plan_id, version, ordinal])


def new_block_id(owner_id: str, calendar_id: str, assignment_id: str, allocation_key: str) -> str:
    """Stable logical allocation key, independent of dates and plan revisions."""
    if any(not part or part != part.strip() for part in (owner_id, calendar_id, assignment_id, allocation_key)):
        raise ValueError("explicit block identity components required")
    return "blk_" + canonical_hash(["campuspulse-block-v1", owner_id, calendar_id, assignment_id, allocation_key])


class Interval(Contract):
    start: Instant
    end: Instant

    @model_validator(mode="after")
    def positive(self) -> Self:
        if self.end <= self.start:
            raise ValueError("interval is half-open and must have positive duration")
        return self

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


class Deadline(Contract):
    at: Instant
    timezone: IanaZone
    precision: Literal["timestamp", "date"] = "timestamp"
    source_date: IsoDate | None = None

    @model_validator(mode="after")
    def date_rule(self) -> Self:
        if self.precision == "date":
            if self.source_date is None or self.at != date_deadline(self.source_date, self.timezone):
                raise ValueError("date deadline must equal next local midnight boundary")
        elif self.source_date is not None:
            raise ValueError("timestamp deadline cannot have source_date")
        return self


class Assignment(Contract):
    assignment_id: Identifier
    title: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
    source_row: Identifier
    deadline: Deadline | None
    remaining_minutes: Minutes | None

    @property
    def required_minutes(self) -> int:
        return required_minutes(self.remaining_minutes)


class SourceKind(StrEnum):
    ASSIGNMENTS = "assignments"
    CALENDAR = "calendar"


class SourceReference(Contract):
    snapshot_id: Identifier
    kind: SourceKind
    owner_id: Identifier
    resource_id: ResourceId
    run_id: Identifier
    content_hash: Digest
    read_at: Instant
    config_revision: Identifier


class SourceIssue(Contract):
    code: SafeCode
    item_ref: Identifier | None = None



class SourceSnapshot(Contract):
    reference: SourceReference
    timezone: IanaZone
    complete: StrictBool
    issues: tuple[SourceIssue, ...] = ()
    covered_interval: Interval | None = None
    source_version: Identifier | None = None

    @model_validator(mode="after")
    def completeness(self) -> Self:
        if self.complete and self.issues:
            raise ValueError("snapshot with unresolved source issues cannot be complete")
        if not self.complete and not self.issues:
            raise ValueError("incomplete snapshot requires an explicit issue")
        if self.reference.kind == SourceKind.CALENDAR and self.covered_interval is None:
            raise ValueError("calendar snapshot requires covered interval")
        return self


class AssignmentsSnapshot(Contract):
    source: SourceSnapshot
    assignments: tuple[Assignment, ...]

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.source.reference.kind != SourceKind.ASSIGNMENTS:
            raise ValueError("wrong source kind")
        if len({a.assignment_id for a in self.assignments}) != len(self.assignments):
            raise ValueError("duplicate assignment identity")
        if self.source.reference.content_hash != assignments_content_hash(self.assignments):
            raise ValueError("assignment snapshot content hash mismatch")
        return self


def assignments_content_hash(assignments: tuple[Assignment, ...]) -> str:
    return canonical_hash({"assignments": [item.model_dump(mode="json") for item in assignments]})


class RecurrenceInstance(Contract):
    series_event_id: ResourceId
    original_start_time: Instant | None = None
    original_start_date: IsoDate | None = None

    @model_validator(mode="after")
    def exactly_one_original(self) -> Self:
        if (self.original_start_time is None) == (self.original_start_date is None):
            raise ValueError("instance requires exactly one original start identity")
        return self


class OwnershipMarker(Contract):
    """Untrusted Calendar private extendedProperties, corroboration only."""

    owner_id: Identifier
    calendar_id: ResourceId
    block_id: Identifier
    assignment_id: Identifier
    create_operation_id: Identifier


class CalendarEvent(Contract):
    event_id: ResourceId
    etag: Identifier | None = None
    interval: Interval | None
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    transparency: Literal["opaque", "transparent"] = "opaque"
    all_day_start: IsoDate | None = None
    all_day_end: IsoDate | None = None
    recurrence: RecurrenceInstance | None = None
    campuspulse_marker: OwnershipMarker | None = None
    event_link: Annotated[str, StringConstraints(strict=True, pattern=r"^https://", max_length=4096)] | None = None

    @model_validator(mode="after")
    def boundaries(self) -> Self:
        if self.status != "cancelled" and self.interval is None:
            raise ValueError("non-cancelled event requires valid boundaries")
        if (self.all_day_start is None) != (self.all_day_end is None):
            raise ValueError("all-day start and exclusive end must be paired")
        if self.all_day_start is not None and self.all_day_end <= self.all_day_start:
            raise ValueError("invalid all-day dates")
        return self

    @property
    def busy(self) -> bool:
        return self.status != "cancelled" and self.transparency != "transparent"


class OwnedBlock(Contract):
    owner_id: Identifier
    calendar_id: ResourceId
    block_id: Identifier
    assignment_id: Identifier
    event_id: EventId
    interval: Interval
    etag: Identifier
    event_link: Annotated[str, StringConstraints(strict=True, pattern=r"^https://", max_length=4096)] | None = None

    @model_validator(mode="after")
    def stable_event_identity(self) -> Self:
        if self.event_id != calendar_event_id(self.owner_id, self.calendar_id, self.block_id):
            raise ValueError("owned event ID does not match stable block identity")
        return self


class CalendarSnapshot(Contract):
    source: SourceSnapshot
    events: tuple[CalendarEvent, ...]
    owned_blocks: tuple[OwnedBlock, ...]
    recurrence_expanded: StrictBool

    @model_validator(mode="after")
    def binding(self) -> Self:
        ref = self.source.reference
        if ref.kind != SourceKind.CALENDAR:
            raise ValueError("wrong source kind")
        if self.source.complete and not self.recurrence_expanded:
            raise ValueError("complete availability requires expanded recurrence/exception pages")
        if len({e.event_id for e in self.events}) != len(self.events):
            raise ValueError("duplicate event instance ID")
        if len({b.block_id for b in self.owned_blocks}) != len(self.owned_blocks):
            raise ValueError("duplicate owned block")
        for block in self.owned_blocks:
            if (block.owner_id, block.calendar_id) != (ref.owner_id, ref.resource_id):
                raise ValueError("owned block outside snapshot binding")
            observed = next((event for event in self.events if event.event_id == block.event_id), None)
            if observed is None or observed.interval != block.interval or observed.etag != block.etag or not observed.busy or observed.recurrence is not None:
                raise ValueError("owned block must match actual single-event observation")
            marker = observed.campuspulse_marker
            if marker is None or (marker.owner_id, marker.calendar_id, marker.block_id, marker.assignment_id) != (
                block.owner_id, block.calendar_id, block.block_id, block.assignment_id
            ):
                raise ValueError("owned block requires matching external corroboration, plus registry join")

        from .time_rules import all_day_bounds
        for event in self.events:
            if event.all_day_start is not None and event.interval is not None:
                bounds = all_day_bounds(event.all_day_start, event.all_day_end, self.source.timezone)
                if (event.interval.start, event.interval.end) != bounds:
                    raise ValueError("all-day interval must match exclusive dates/calendar zone")
        if ref.content_hash != calendar_content_hash(self.events, self.owned_blocks, self.recurrence_expanded):
            raise ValueError("calendar snapshot content hash mismatch")
        return self


def calendar_content_hash(events: tuple[CalendarEvent, ...], owned_blocks: tuple[OwnedBlock, ...], recurrence_expanded: bool) -> str:
    return canonical_hash({
        "events": [item.model_dump(mode="json") for item in events],
        "owned_blocks": [item.model_dump(mode="json") for item in owned_blocks],
        "recurrence_expanded": recurrence_expanded,
    })


class PlanningConstraints(Contract):
    config_revision: Identifier
    timezone: IanaZone
    horizon: Interval
    working_windows: tuple[Interval, ...] | None
    grid_minutes: Literal[15] = 15
    min_block_minutes: Literal[15] = 15
    max_block_minutes: Literal[60] = 60
    preferred_block_minutes: Literal[60] = 60
    search_node_budget: Annotated[StrictInt, Field(gt=0, le=100_000)] = 100_000
    objective: tuple[Literal["changed_blocks", "displacement_minutes", "new_blocks", "preferences"], ...] = (
        "changed_blocks", "displacement_minutes", "new_blocks", "preferences"
    )

    @model_validator(mode="after")
    def horizon_and_windows(self) -> Self:
        if self.horizon.minutes != 72 * 60:
            raise ValueError("MVP horizon is exactly 72 elapsed hours")
        if self.horizon.start.minute % 15 or self.horizon.start.second or self.horizon.start.microsecond:
            raise ValueError("horizon starts on a UTC quarter-hour")
        if self.objective != ("changed_blocks", "displacement_minutes", "new_blocks", "preferences"):
            raise ValueError("MVP objective order is fixed")
        if self.working_windows is not None:
            ordered = sorted(self.working_windows, key=lambda item: item.start)
            for window in ordered:
                if window.start < self.horizon.start or window.end > self.horizon.end:
                    raise ValueError("working windows must be clipped to horizon")
            if any(left.end > right.start for left, right in zip(ordered, ordered[1:])):
                raise ValueError("working windows must not overlap")
        return self


class BlockTarget(Contract):
    block_id: Identifier
    assignment_id: Identifier
    interval: Interval

    @model_validator(mode="after")
    def grid(self) -> Self:
        if self.interval.minutes not in (15, 30, 45, 60):
            raise ValueError("study duration must be 15/30/45/60 elapsed minutes")
        for point in (self.interval.start, self.interval.end):
            if point.minute % 15 or point.second or point.microsecond:
                raise ValueError("study boundaries must be on UTC quarter-hours")
        return self


class Operation(Contract):
    operation_id: Identifier
    kind: Literal["create", "update"]
    event_id: EventId
    target: BlockTarget
    expected_etag: Identifier | None = None
    before: Interval | None = None

    @model_validator(mode="after")
    def conditional_update(self) -> Self:
        if self.kind == "update":
            if self.expected_etag is None or self.before is None:
                raise ValueError("update requires observed etag and before interval")
            if self.before == self.target.interval:
                raise ValueError("unchanged block is not an update")
        elif self.expected_etag is not None or self.before is not None:
            raise ValueError("create cannot have previous event state")
        return self


class RepairStatus(StrEnum):
    FEASIBLE = "feasible"
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    UNKNOWN = "unknown"
    NEEDS_INPUT = "needs_input"
    NEEDS_REVIEW = "needs_review"
    NO_CHANGE = "no_change"


class Finding(Contract):
    code: SafeCode
    assignment_id: Identifier | None = None
    block_id: Identifier | None = None
    operation_id: Identifier | None = None
    deficit_minutes: Minutes | None = None
    deadline: Instant | None = None


class SearchEvidence(Contract):
    explored_nodes: Minutes
    node_budget: PositiveInt
    exhausted: StrictBool = False
    interrupted: StrictBool = False
    proof: Literal["none", "capacity_certificate", "exhaustive_search"] = "none"
    certificate: tuple[Finding, ...] = ()

    @model_validator(mode="after")
    def proof_claim(self) -> Self:
        if self.explored_nodes > self.node_budget:
            raise ValueError("exploration exceeds declared budget")
        if self.proof == "exhaustive_search" and (not self.exhausted or self.interrupted):
            raise ValueError("exhaustive proof requires completed exhaustive search")
        if self.proof == "capacity_certificate" and not self.certificate:
            raise ValueError("capacity proof requires deadline-specific certificate")
        if self.proof == "capacity_certificate" and any(
            finding.deadline is None or finding.deficit_minutes is None or finding.deficit_minutes <= 0
            for finding in self.certificate
        ):
            raise ValueError("capacity certificate requires explicit deadline and positive deficit")
        return self


class WorkAllocation(Contract):
    assignment_id: Identifier
    remaining_minutes: Minutes
    required_minutes: Minutes
    padding_minutes: Minutes
    scheduled_minutes: Minutes
    deferred: StrictBool = False

    @model_validator(mode="after")
    def padding(self) -> Self:
        if self.required_minutes != required_minutes(self.remaining_minutes):
            raise ValueError("invalid rounded work")
        if self.padding_minutes != self.required_minutes - self.remaining_minutes:
            raise ValueError("invalid padding")
        return self


def validate_projected_schedule(blocks: tuple[BlockTarget, ...], allocations: tuple[WorkAllocation, ...], constraints: PlanningConstraints) -> None:
    """Structural feasibility only; source deadlines/busy/ownership need revalidation."""
    if constraints.working_windows is None:
        raise ValueError("unknown working windows cannot produce an executable schedule")
    if len({block.block_id for block in blocks}) != len(blocks):
        raise ValueError("duplicate projected block")
    if len({item.assignment_id for item in allocations}) != len(allocations):
        raise ValueError("duplicate work allocation")
    allocation_map = {item.assignment_id: item for item in allocations}
    ordered = sorted(blocks, key=lambda block: block.interval.start)
    if any(left.interval.end > right.interval.start for left, right in zip(ordered, ordered[1:])):
        raise ValueError("projected study blocks overlap")
    for block in blocks:
        if block.assignment_id not in allocation_map:
            raise ValueError("projected block has no explicit work allocation")
        if block.interval.start < constraints.horizon.start or block.interval.end > constraints.horizon.end:
            raise ValueError("projected future block outside horizon")
        if not any(window.start <= block.interval.start and block.interval.end <= window.end for window in constraints.working_windows):
            raise ValueError("projected block outside configured working windows")
    for item in allocations:
        scheduled = sum(int(block.interval.minutes) for block in blocks if block.assignment_id == item.assignment_id)
        if scheduled != item.scheduled_minutes:
            raise ValueError("allocation volume differs from projected schedule")
        if not item.deferred and item.scheduled_minutes != item.required_minutes:
            raise ValueError("relevant explicit remaining work is not fully scheduled")


class RepairResult(Contract):
    status: RepairStatus
    constraints: PlanningConstraints
    source_references: tuple[SourceReference, ...]
    blocks: tuple[BlockTarget, ...] = ()
    operations: tuple[Operation, ...] = ()
    allocations: tuple[WorkAllocation, ...] = ()
    findings: tuple[Finding, ...] = ()
    search: SearchEvidence

    @model_validator(mode="after")
    def claims(self) -> Self:
        executable = {RepairStatus.FEASIBLE, RepairStatus.OPTIMAL, RepairStatus.NO_CHANGE}
        if self.status not in executable and self.operations:
            raise ValueError("non-executable repair cannot carry executable operations")
        if self.status == RepairStatus.NO_CHANGE and self.operations:
            raise ValueError("no_change requires empty diff")
        if self.status in {RepairStatus.OPTIMAL, RepairStatus.INFEASIBLE} and self.search.proof == "none":
            raise ValueError("optimal/infeasible require proof in declared model")
        if self.status == RepairStatus.OPTIMAL and self.search.proof != "exhaustive_search":
            raise ValueError("optimal requires exhaustive objective proof")
        if self.status in {RepairStatus.NEEDS_INPUT, RepairStatus.NEEDS_REVIEW, RepairStatus.INFEASIBLE} and not self.findings:
            raise ValueError("non-actionable result requires concrete findings")
        if self.search.interrupted and self.status != RepairStatus.UNKNOWN:
            raise ValueError("interrupted search must be unknown")
        if self.search.node_budget != self.constraints.search_node_budget:
            raise ValueError("search evidence budget differs from constraints")
        if self.status in executable:
            validate_projected_schedule(self.blocks, self.allocations, self.constraints)
        return self


class PlanPayload(Contract):
    owner_id: Identifier
    calendar_id: ResourceId
    plan_id: Identifier
    version: PositiveInt
    created_at: Instant
    source_references: tuple[SourceReference, ...]
    constraints: PlanningConstraints
    operations: tuple[Operation, ...]
    blocks: tuple[BlockTarget, ...]
    allocations: tuple[WorkAllocation, ...]

    @model_validator(mode="after")
    def binding(self) -> Self:
        if {ref.kind for ref in self.source_references} != {SourceKind.ASSIGNMENTS, SourceKind.CALENDAR} or len(self.source_references) != 2:
            raise ValueError("exactly one assignment and calendar reference required")
        for ref in self.source_references:
            if ref.owner_id != self.owner_id or ref.config_revision != self.constraints.config_revision:
                raise ValueError("source owner/config binding mismatch")
            if ref.kind == SourceKind.CALENDAR and ref.resource_id != self.calendar_id:
                raise ValueError("source calendar mismatch")
        if len({ref.run_id for ref in self.source_references}) != 1:
            raise ValueError("sources must belong to one trusted run")
        validate_projected_schedule(self.blocks, self.allocations, self.constraints)
        targets = {block.block_id: block for block in self.blocks}
        if len({op.target.block_id for op in self.operations}) != len(self.operations):
            raise ValueError("at most one operation per block per plan")
        for ordinal, op in enumerate(self.operations):
            if op.operation_id != operation_id(self.plan_id, self.version, ordinal):
                raise ValueError("operation identity must bind plan/version/ordinal")
            if op.event_id != calendar_event_id(self.owner_id, self.calendar_id, op.target.block_id):
                raise ValueError("operation event identity mismatch")
            if targets.get(op.target.block_id) != op.target:
                raise ValueError("operation target must match projected schedule")
            if op.kind == "update" and op.before.start <= self.created_at:
                raise ValueError("past/in-progress block cannot be moved")
        return self


class PlanVersion(Contract):
    payload: PlanPayload
    payload_hash: Digest

    @model_validator(mode="after")
    def hash_matches(self) -> Self:
        if self.payload_hash != canonical_hash(self.payload):
            raise ValueError("immutable plan hash mismatch")
        return self

    @classmethod
    def seal(cls, payload: PlanPayload) -> PlanVersion:
        return cls(payload=payload, payload_hash=canonical_hash(payload))


class PlanReference(Contract):
    plan_id: Identifier
    version: PositiveInt
    payload_hash: Digest


class Approval(Contract):
    owner_id: Identifier
    calendar_id: ResourceId
    plan: PlanReference
    approved_by: Identifier
    approved_at: Instant
    expires_at: Instant
    status: Literal["active", "revoked", "superseded", "expired"] = "active"
    invalidated_at: Instant | None = None

    @model_validator(mode="after")
    def lifetime(self) -> Self:
        if self.expires_at <= self.approved_at or (self.expires_at - self.approved_at).total_seconds() > 900:
            raise ValueError("approval lifetime must be positive and at most 15 minutes")
        if self.status in {"revoked", "superseded"} and self.invalidated_at is None:
            raise ValueError("revocation/supersession requires timestamp")
        if self.status == "active" and self.invalidated_at is not None:
            raise ValueError("active approval cannot be invalidated")
        if self.invalidated_at is not None and self.invalidated_at < self.approved_at:
            raise ValueError("approval invalidation predates approval")
        return self

    def active_at(self, now: datetime) -> bool:
        current = instant(now)
        return self.status == "active" and self.invalidated_at is None and self.approved_at <= current < self.expires_at


class OperationStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLYING = "applying"
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    APPLIED_UNVERIFIED = "applied_unverified"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    REJECTED = "rejected"
    FAILED = "failed"


OPERATION_TRANSITIONS = {
    OperationStatus.PENDING: frozenset({OperationStatus.IN_FLIGHT, OperationStatus.FAILED}),
    OperationStatus.IN_FLIGHT: frozenset({OperationStatus.COMPLETED, OperationStatus.UNKNOWN, OperationStatus.FAILED}),
    OperationStatus.UNKNOWN: frozenset({OperationStatus.COMPLETED, OperationStatus.PENDING, OperationStatus.FAILED}),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
}
EXECUTION_TRANSITIONS = {
    ExecutionStatus.PROPOSED: frozenset({ExecutionStatus.APPROVED, ExecutionStatus.REJECTED}),
    ExecutionStatus.APPROVED: frozenset({ExecutionStatus.APPLYING, ExecutionStatus.REJECTED}),
    ExecutionStatus.APPLYING: frozenset({ExecutionStatus.UNKNOWN, ExecutionStatus.PARTIAL, ExecutionStatus.APPLIED_UNVERIFIED, ExecutionStatus.FAILED, ExecutionStatus.REJECTED}),
    ExecutionStatus.UNKNOWN: frozenset({ExecutionStatus.APPLYING, ExecutionStatus.PARTIAL, ExecutionStatus.APPLIED_UNVERIFIED, ExecutionStatus.FAILED, ExecutionStatus.REJECTED}),
    ExecutionStatus.PARTIAL: frozenset({ExecutionStatus.APPLYING, ExecutionStatus.APPLIED_UNVERIFIED}),
    ExecutionStatus.APPLIED_UNVERIFIED: frozenset({ExecutionStatus.VERIFIED, ExecutionStatus.VERIFICATION_FAILED}),
    ExecutionStatus.VERIFICATION_FAILED: frozenset({ExecutionStatus.VERIFIED}),
    ExecutionStatus.VERIFIED: frozenset({ExecutionStatus.VERIFICATION_FAILED}),
    ExecutionStatus.REJECTED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
}


def validate_operation_transition(before: OperationStatus, after: OperationStatus, *, reconciled: bool = False) -> None:
    if before == after:
        return
    if after not in OPERATION_TRANSITIONS[before]:
        raise ValueError("forbidden operation transition")
    if before == OperationStatus.UNKNOWN and not reconciled:
        raise ValueError("unknown outcome requires actual external reconciliation")


def validate_execution_transition(before: ExecutionStatus, after: ExecutionStatus) -> None:
    if before != after and after not in EXECUTION_TRANSITIONS[before]:
        raise ValueError("forbidden execution transition")


class OperationState(Contract):
    operation_id: Identifier
    status: OperationStatus
    attempts: Minutes = 0
    updated_at: Instant
    observed_event: CalendarEvent | None = None
    error_code: SafeCode | None = None
    reconciled_at: Instant | None = None
    fencing_generation: PositiveInt | None = None

    @model_validator(mode="after")
    def observed_completion(self) -> Self:
        if self.status == OperationStatus.COMPLETED and self.observed_event is None:
            raise ValueError("completed write needs response or reconciled event evidence")
        if self.status == OperationStatus.COMPLETED and (
            self.observed_event.etag is None or self.observed_event.interval is None
            or self.observed_event.status == "cancelled"
        ):
            raise ValueError("completed write needs actual timed event/version evidence")
        return self


class ExecutionState(Contract):
    owner_id: Identifier
    calendar_id: ResourceId
    plan: PlanReference
    request_key: Identifier
    run_id: Identifier
    status: ExecutionStatus
    operations: tuple[OperationState, ...] = ()
    revision: Minutes = 0
    updated_at: Instant

    @model_validator(mode="after")
    def aggregate(self) -> Self:
        if len({op.operation_id for op in self.operations}) != len(self.operations):
            raise ValueError("duplicate operation state")
        if self.status in {ExecutionStatus.APPLIED_UNVERIFIED, ExecutionStatus.VERIFIED} and any(op.status != OperationStatus.COMPLETED for op in self.operations):
            raise ValueError("application cannot be complete with unfinished operations")
        if self.status == ExecutionStatus.PARTIAL and not (
            any(op.status == OperationStatus.COMPLETED for op in self.operations)
            and any(op.status != OperationStatus.COMPLETED for op in self.operations)
        ):
            raise ValueError("partial requires both completed and unfinished operations")
        if self.status in {ExecutionStatus.FAILED, ExecutionStatus.REJECTED} and any(
            op.status in {OperationStatus.COMPLETED, OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT}
            for op in self.operations
        ):
            raise ValueError("failed/rejected cannot conceal completed or unresolved effects")
        if self.status == ExecutionStatus.UNKNOWN and (
            any(op.status == OperationStatus.COMPLETED for op in self.operations)
            or not any(op.status in {OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT} for op in self.operations)
        ):
            raise ValueError("unknown requires unresolved effects and no completed writes; otherwise partial")
        return self


class VerificationResult(Contract):
    plan: PlanReference
    run_id: Identifier
    status: Literal["verified", "verification_failed", "applied_unverified", "unknown"]
    attempted_at: Instant
    verification_at: Instant | None = None
    calendar_source: SourceSnapshot | None = None
    findings: tuple[Finding, ...] = ()
    observed_events: tuple[CalendarEvent, ...] = ()
    checked_operations: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def actual_read_evidence(self) -> Self:
        if self.calendar_source is not None and self.calendar_source.reference.kind != SourceKind.CALENDAR:
            raise ValueError("verification requires calendar source")
        if self.calendar_source is not None and self.calendar_source.reference.run_id != self.run_id:
            raise ValueError("verification read must bind current run")
        if self.status in {"verified", "verification_failed"}:
            if self.calendar_source is None or not self.calendar_source.complete or self.verification_at is None:
                raise ValueError("conclusive verification requires complete actual read and timestamp")
            if not self.attempted_at <= self.calendar_source.reference.read_at <= self.verification_at:
                raise ValueError("verification must use a fresh read after attempt began")
        if self.status == "verified" and self.findings:
            raise ValueError("verified cannot contain failing findings")
        if self.status == "verification_failed" and not self.findings:
            raise ValueError("failed verification requires concrete findings")
        return self


class ApplyResult(Contract):
    execution: ExecutionState
    findings: tuple[Finding, ...] = ()
    verification: VerificationResult | None = None

    @model_validator(mode="after")
    def no_unsupported_verified_claim(self) -> Self:
        if self.verification is not None and self.verification.plan != self.execution.plan:
            raise ValueError("verification must bind exact execution plan")
        if self.execution.status == ExecutionStatus.VERIFIED and (
            self.verification is None or self.verification.status != "verified"
        ):
            raise ValueError("verified replay needs stored actual verification; apply alone cannot verify")
        return self


class OwnedBlockRecord(Contract):
    owner_id: Identifier
    calendar_id: ResourceId
    block_id: Identifier
    assignment_id: Identifier
    event_id: EventId
    state: Literal["reserved", "confirmed"]
    intent_plan: PlanReference
    intent_operation_id: Identifier
    reserved_at: Instant
    confirmed_at: Instant | None = None
    confirmed_etag: Identifier | None = None

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if self.event_id != calendar_event_id(self.owner_id, self.calendar_id, self.block_id):
            raise ValueError("registry identity mismatch")
        if self.state == "reserved" and (self.confirmed_at is not None or self.confirmed_etag is not None):
            raise ValueError("reservation is not confirmed evidence")
        if self.state == "confirmed" and (self.confirmed_at is None or self.confirmed_etag is None):
            raise ValueError("confirmed ownership needs actual event evidence")
        if self.confirmed_at is not None and self.confirmed_at < self.reserved_at:
            raise ValueError("confirmation predates intent")
        return self


class RunEvent(Contract):
    event_id: Identifier
    run_id: Identifier
    call_id: Identifier | None = None
    operation_id: Identifier | None = None
    plan: PlanReference | None = None
    sequence: Minutes
    occurred_at: Instant
    tool: Literal["read_assignments", "read_calendar", "solve_repair", "apply_plan", "verify_plan"] | None = None
    phase: Literal["started", "result", "decision", "error"]
    status: SafeCode
    duration_ms: Minutes | None = None
    code: SafeCode | None = None
    result_reference: Identifier | None = None
    arguments_reference: Identifier | None = None

    source_references: tuple[SourceReference, ...] = ()



class ReadWindowInput(Contract):
    horizon: Interval

    @model_validator(mode="after")
    def bounded_read(self) -> Self:
        if self.horizon.minutes > 72 * 60:
            raise ValueError("MVP read horizon cannot exceed 72 elapsed hours")
        return self


class ReadAssignmentsInput(ReadWindowInput):
    pass


class ReadCalendarInput(ReadWindowInput):
    pass


class SolveRepairInput(Contract):
    assignments: AssignmentsSnapshot
    calendar: CalendarSnapshot
    constraints: PlanningConstraints
    now: Instant
    plan_id: Identifier
    version: PositiveInt

    @model_validator(mode="after")
    def consistent_sources(self) -> Self:
        from .time_rules import planning_horizon

        assignments_ref = self.assignments.source.reference
        calendar_ref = self.calendar.source.reference
        if (assignments_ref.owner_id, assignments_ref.run_id, assignments_ref.config_revision) != (
            calendar_ref.owner_id, calendar_ref.run_id, calendar_ref.config_revision
        ):
            raise ValueError("solver sources must share owner/run/config binding")
        if self.constraints.config_revision != calendar_ref.config_revision:
            raise ValueError("solver config differs from sources")
        if self.constraints.timezone != self.assignments.source.timezone:
            raise ValueError("solver student zone differs from assignment source")


        start, end = planning_horizon(self.now)
        if (self.constraints.horizon.start, self.constraints.horizon.end) != (start, end):
            raise ValueError("solver horizon must derive from explicit now")
        coverage = self.calendar.source.covered_interval
        if coverage.start > start or coverage.end < end:
            raise ValueError("calendar read must cover entire search horizon")
        return self


class ApplyPlanInput(Contract):
    plan_id: Identifier
    version: PositiveInt
    request_key: Identifier


class VerifyPlanInput(Contract):
    plan_id: Identifier
    version: PositiveInt


TOOL_CONTRACTS = {
    "read_assignments": (ReadAssignmentsInput, AssignmentsSnapshot),
    "read_calendar": (ReadCalendarInput, CalendarSnapshot),
    "solve_repair": (SolveRepairInput, RepairResult),
    "apply_plan": (ApplyPlanInput, ApplyResult),
    "verify_plan": (VerifyPlanInput, VerificationResult),
}
