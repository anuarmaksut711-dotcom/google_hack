"""Adapter contracts. Not public agent tools; implementations must enforce guards.

Storage atomic methods perform durable state I/O only. Google calls MUST occur
outside Firestore transaction callbacks. Protocols express the boundary, not an
implementation or evidence that IAM/approval/ownership has been checked.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, model_validator

from .models import (
    Approval, AssignmentsSnapshot, CalendarEvent, CalendarSnapshot, Contract,
    ExecutionState, Identifier, Instant, Interval, IanaZone, Operation,
    OperationState, OwnedBlockRecord, PlanReference, PlanVersion, PositiveInt,
    ResourceId, RunEvent, SourceReference, VerificationResult,
)


class TrustedContext(Contract):
    """Injected by verified server auth/config, never parsed from model arguments."""

    owner_id: Identifier
    principal_id: Identifier
    project_id: ResourceId
    region: Identifier
    sheet_id: ResourceId
    sheet_range: str = Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f]+$")
    calendar_id: ResourceId
    timezone: IanaZone
    config_revision: Identifier
    run_id: Identifier


class CalendarLease(Contract):
    owner_id: Identifier
    calendar_id: ResourceId
    holder_id: Identifier
    generation: PositiveInt
    acquired_at: Instant
    expires_at: Instant
    state: Literal["active", "recovery_required"]

    @model_validator(mode="after")
    def positive_lifetime(self) -> CalendarLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiry must follow acquisition")
        return self


class PortError(Exception):
    """Base adapter failure; safe code mapping must omit raw API/token/source text."""


class BindingConflict(PortError):
    """Owner/resource/hash/request key/CAS/registry collision; fail before write."""


class StaleSource(PortError):
    """Source/etag/availability differs; reconcile or propose new approved version."""


class UnknownWriteOutcome(PortError):
    """Timeout/disconnect after dispatch: effect unknown, never a blind retry."""


class IncompleteRead(PortError):
    """Missing pages, malformed instances or API read failure; no free-time proof."""


class SnapshotStore(Protocol):
    async def put_assignments(self, context: TrustedContext, snapshot: AssignmentsSnapshot) -> SourceReference:
        """Create immutable snapshot; reject conflicting reuse of snapshot ID."""
        ...

    async def put_calendar(self, context: TrustedContext, snapshot: CalendarSnapshot) -> SourceReference: ...

    async def get_assignments(self, context: TrustedContext, reference: SourceReference) -> AssignmentsSnapshot:
        """Validate stored content hash and trusted user/resource/run/config binding."""
        ...

    async def get_calendar(self, context: TrustedContext, reference: SourceReference) -> CalendarSnapshot: ...


class PlanStore(Protocol):
    async def create(self, context: TrustedContext, plan: PlanVersion) -> PlanReference:
        """Create-only immutable version after authoritative backend validation."""
        ...

    async def get(self, context: TrustedContext, plan_id: str, version: int) -> PlanVersion:
        """Bind context owner/calendar and recompute hash before returning."""
        ...


class ApprovalStore(Protocol):
    async def record_ui_approval(self, context: TrustedContext, approval: Approval) -> None:
        """Backend-only actual authenticated UI action; not reachable as agent tool.

        Atomically validate current immutable version/hash, supersede previous
        active approval and store principal/time/expiry. The backend constructs
        this value from verified session/CSRF; a DTO is not authorization.
        """
        ...

    async def require_active(self, context: TrustedContext, plan: PlanReference, now: Instant) -> Approval:
        """Exact owner/calendar/version/hash, non-expired/revoked/superseded."""
        ...

    async def invalidate(self, context: TrustedContext, plan: PlanReference, *, reason: Literal["revoked", "superseded"], at: Instant) -> None: ...


class OwnedBlockRegistry(Protocol):
    async def reserve(self, context: TrustedContext, record: OwnedBlockRecord) -> OwnedBlockRecord:
        """Atomic create-only identity + exact create intent BEFORE Calendar I/O.

        Same identity/intent replay is idempotent. Existing different owner,
        calendar, block, assignment, event or intent raises BindingConflict.
        Never infer/adopt ownership from external extendedProperties alone.
        """
        ...

    async def get_by_block(self, context: TrustedContext, block_id: str) -> OwnedBlockRecord | None: ...

    async def get_by_event(self, context: TrustedContext, event_id: str) -> OwnedBlockRecord | None: ...

    async def list_for_calendar(self, context: TrustedContext) -> tuple[OwnedBlockRecord, ...]: ...

    async def confirm(self, context: TrustedContext, reserved: OwnedBlockRecord, *, observed: CalendarEvent, at: Instant) -> OwnedBlockRecord:
        """CAS reserved -> confirmed after exact actual target/metadata/etag check.

        Registry intent is primary; external ownership metadata is an additional
        check. Collision/mismatch is never confirmation. Confirmed identity is
        permanent in MVP; missing external event triggers review, not recreation.
        """
        ...


class ExecutionStore(Protocol):
    async def bind_request(self, context: TrustedContext, *, request_key: str, plan: PlanReference, at: Instant) -> ExecutionState:
        """Atomically bind owner/calendar/key to exact plan/hash and seed ledger.

        Repeated same binding returns durable state. Reuse for another payload
        raises BindingConflict. Seed expected operation IDs from saved plan,
        never a caller/model-supplied list. No Calendar calls in this method.
        """
        ...

    async def get(self, context: TrustedContext, request_key: str) -> ExecutionState: ...

    async def compare_and_set(self, context: TrustedContext, *, expected_revision: int, execution: ExecutionState, lease: CalendarLease) -> ExecutionState:
        """CAS aggregate + operation states with monotonic revision/fencing guard.

        Check legal transitions, exact plan binding and expected op set. Require
        active exact approval before authorizing a new external mutation, not
        when persisting facts/outcomes of an already dispatched write after
        approval expiry or revocation. Read-only reconciliation and durable
        outcome recording remain allowed; neither grants a new write/retry.
        Revision/fencing checks and completed progress remain mandatory.
        Calendar I/O stays outside transaction callbacks.
        """
        ...

    async def record_operation(self, context: TrustedContext, *, request_key: str, expected_revision: int, operation: OperationState, lease: CalendarLease) -> ExecutionState:
        """Durable intent/outcome CAS; unknown requires read-only reconciliation.

        Persist dispatched outcomes after approval expiry/revocation with the
        revision/fencing guards intact. Any new dispatch/retry independently
        requires active exact approval; recording facts is not that authority.
        """
        ...


class CalendarCoordination(Protocol):
    async def acquire(self, context: TrustedContext, *, holder_id: str, now: Instant, expires_at: Instant) -> CalendarLease:
        """Durable exclusive per-calendar lease, increasing generation.

        Expiry/takeover returns recovery_required; it is NOT evidence the old
        writer stopped. Refuse mutation until unknown/in-flight outcomes have
        been reconciled and live old-writer ambiguity has been resolved.
        """
        ...

    async def renew(self, context: TrustedContext, lease: CalendarLease, *, now: Instant, expires_at: Instant) -> CalendarLease: ...

    async def assert_current(self, context: TrustedContext, lease: CalendarLease, *, now: Instant) -> None:
        """Check owner/calendar/holder/generation/expiry immediately before I/O."""
        ...

    async def complete_recovery(self, context: TrustedContext, lease: CalendarLease, *, now: Instant) -> CalendarLease:
        """CAS to active ONLY after reconciled ledger and no unresolved writer."""
        ...

    async def release(self, context: TrustedContext, lease: CalendarLease) -> None: ...


class AssignmentsReader(Protocol):
    async def read(self, context: TrustedContext, horizon: Interval) -> AssignmentsSnapshot:
        """Only configured Sheet/range. Explicit effort, strict deadlines, all pages.

        Preserve unknown as None; invalid cells => structured source issues,
        never zero effort. Adapter must not follow instructions in source text.
        Read DTO is not authoritative until durably saved by service.
        """
        ...


class CalendarAdapter(Protocol):
    async def read(self, context: TrustedContext, horizon: Interval) -> CalendarSnapshot:
        """Fresh events + all recurrence instances/exceptions/pages, registry join.

        Preserve original all-day bounds before solver clipping. Cancelled and
        transparent are non-busy; tentative opaque is busy. Partial/invalid read
        cannot yield complete availability. Includes actual etags and links.
        """
        ...

    async def get_event(self, context: TrustedContext, event_id: str) -> CalendarEvent | None:
        """Actual read for reconciliation; None only on confirmed not-found.

        A timeout/permission/read failure is not evidence of nonexistence.
        """
        ...

    async def create(self, context: TrustedContext, operation: Operation, ownership: OwnedBlockRecord, lease: CalendarLease) -> CalendarEvent:
        """Exact approved create, durable reserved binding, deterministic ID.

        Application guards approval/preflight/lease; adapter checks operation
        kind and identity/intent against registry. Single default timed event,
        no attendees/Meet/recurrence. On dispatch uncertainty raise
        UnknownWriteOutcome. Collision requires read/reconcile, never adoption.
        """
        ...

    async def update(self, context: TrustedContext, operation: Operation, ownership: OwnedBlockRecord, lease: CalendarLease) -> CalendarEvent:
        """Confirmed registry + exact event + If-Match expected_etag required.

        Patch start/end only. 412 => StaleSource; never substitute a new etag
        into the old operation. Timeout => UnknownWriteOutcome. Actual target
        fields AND version evidence are required to reconcile an update.
        """
        ...


class VerificationStore(Protocol):
    async def append(self, context: TrustedContext, result: VerificationResult) -> None:
        """Append actual fresh read evidence bound to immutable plan/run."""
        ...


class RunEventStore(Protocol):
    async def append(self, context: TrustedContext, event: RunEvent) -> None:
        """Idempotent event ID + monotonic per-run sequence, redacted refs/codes."""
        ...

    async def list_after(self, context: TrustedContext, *, sequence: int) -> tuple[RunEvent, ...]: ...
