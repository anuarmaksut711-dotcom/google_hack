"""Internal CalendarAdapter write/reconciliation primitives (no public tools).

T19 loads approved immutable operations, performs fresh availability preflight
and claims the durable dispatch ledger BEFORE calling these methods. A lease,
operation DTO or mutation=True alone is never consent. Calendar I/O is outside
Firestore transactions. Responses are observations, not plan verification.
Compose with CalendarReader via CalendarAdapter below for the frozen T03 port.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable

from pydantic import TypeAdapter, ValidationError

from ..domain.models import (
    CalendarEvent, EventId, Interval, Operation, OwnedBlockRecord, OwnershipMarker,
    PlanVersion, ResourceId, calendar_event_id, canonical_hash,
)
from ..domain.ports import (
    BindingConflict, CalendarCoordination, CalendarLease, IncompleteRead,
    OwnedBlockRegistry, PlanStore, PortError, StaleSource, TrustedContext, UnknownWriteOutcome,
)
from ..domain.time_rules import iana_zone, instant
from ..security.context import AccessDenied, TrustedRequestContext
from .calendar_read import CalendarReader, normalize_event, ownership_properties
from .google_clients import GoogleWorkspaceClients
from .google_credentials import WorkspaceAccessError

SINGLE_EVENT_FIELDS = (
    "id,etag,status,transparency,start,end,recurrence,recurringEventId,"
    "originalStartTime,htmlLink,extendedProperties/private,eventType,endTimeUnspecified"
)


class TargetComparison(StrEnum):
    """Read evidence only; changed/unknown both prohibit blind retry.

    missing means confirmed absence (never an ambiguous 404); it does not
    authorize recreating an event whose registry is already confirmed.
    """

    MATCHED = "matched"
    NO_EFFECT = "no_effect"
    MISSING = "missing"
    CHANGED = "changed"
    UNKNOWN = "unknown"


def target_fingerprint(operation: Operation) -> str:
    """Exact T06 durable fingerprint: entire immutable intent, including etag."""
    return canonical_hash(Operation.model_validate(operation))


def _marker(record: OwnedBlockRecord) -> OwnershipMarker:
    return OwnershipMarker(owner_id=record.owner_id, calendar_id=record.calendar_id,
        block_id=record.block_id, assignment_id=record.assignment_id,
        create_operation_id=record.intent_operation_id)


def deterministic_event_id(owner_id: str, calendar_id: str, block_id: str) -> str:
    """Reuse the frozen logical identity; validate Calendar's base32hex syntax."""
    return TypeAdapter(EventId).validate_python(calendar_event_id(owner_id, calendar_id, block_id))


def _bound(operation: Operation, record: OwnedBlockRecord) -> None:
    if (operation.event_id, operation.target.block_id, operation.target.assignment_id) != (
            record.event_id, record.block_id, record.assignment_id):
        raise BindingConflict("calendar_operation_binding_mismatch")
    if operation.kind == "create" and operation.operation_id != record.intent_operation_id:
        raise BindingConflict("calendar_create_intent_mismatch")


def compare_target(operation: Operation, ownership: OwnedBlockRecord,
                   observed: CalendarEvent | None, *, read_complete: bool = True) -> TargetComparison:
    """Compare an ACTUAL read, never a synthesized DTO or ID/existence alone.

    None requires confirmed not-found from the caller. Failed reads use
    read_complete=False. Update target match needs a new etag; exact before AND
    original etag proves no effect. Any other state needs review/reconciliation.
    This cannot establish causality or detect conflicts elsewhere in a calendar.
    """
    operation = Operation.model_validate(operation)
    ownership = OwnedBlockRecord.model_validate(ownership)
    _bound(operation, ownership)
    if not read_complete:
        return TargetComparison.UNKNOWN
    if observed is None:
        return TargetComparison.MISSING
    observed = CalendarEvent.model_validate(observed)
    if (observed.event_id != operation.event_id or observed.campuspulse_marker != _marker(ownership)
            or observed.status != "confirmed" or observed.transparency != "opaque"
            or observed.recurrence is not None or observed.all_day_start is not None):
        return TargetComparison.CHANGED
    if observed.etag is None:
        return TargetComparison.UNKNOWN
    if operation.kind == "create":
        return (TargetComparison.MATCHED if observed.interval == operation.target.interval
                else TargetComparison.CHANGED)
    if ownership.state != "confirmed":
        return TargetComparison.CHANGED
    if observed.interval == operation.target.interval and observed.etag != operation.expected_etag:
        return TargetComparison.MATCHED
    if observed.interval == operation.before and observed.etag == operation.expected_etag:
        return TargetComparison.NO_EFFECT
    return TargetComparison.CHANGED


def _times(interval: Interval) -> dict:
    return {"start": {"dateTime": interval.start.isoformat()},
            "end": {"dateTime": interval.end.isoformat()}}


class CalendarWriter:
    """Frozen create/update/get_event signatures with trusted startup dependencies.

    No automatic mutation retry. An initial reserved create uses insert's
    create-only ID guard; 409 triggers read-only reconciliation. A timeout must
    return to T19's durable unknown state before any further dispatch. Confirmed
    create replays only read: a missing/changed event is never recreated/adopted.
    """

    def __init__(self, clients: GoogleWorkspaceClients, registry: OwnedBlockRegistry,
                 coordination: CalendarCoordination, plans: PlanStore, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 request_context: Callable[[], TrustedRequestContext] | None = None) -> None:
        self.clients, self.registry, self.coordination, self.plans = clients, registry, coordination, plans
        self.clock, self._request_context = clock, request_context

    def _request(self, context: TrustedContext | TrustedRequestContext) -> TrustedRequestContext:
        if isinstance(context, TrustedRequestContext):
            request = context
        else:
            if not isinstance(context, TrustedContext) or self._request_context is None:
                raise AccessDenied("server_request_context_required")
            request = self._request_context()
            self.clients.policy.require_context(request)
            if context is not request.as_port_context():
                raise AccessDenied("foreign_port_context")
        self.clients.policy.require_context(request)
        if request.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        return request

    async def _registered(self, context, operation, ownership, *, kind):
        operation = Operation.model_validate(operation)
        ownership = OwnedBlockRecord.model_validate(ownership)
        if operation.kind != kind:
            raise BindingConflict("calendar_operation_kind_mismatch")
        trusted = self.clients.policy.require_context(context)
        self.clients.policy.require_owner(context, ownership.owner_id)
        self.clients.policy.require_calendar(context, ownership.calendar_id)
        _bound(operation, ownership)
        if operation.event_id != deterministic_event_id(trusted.owner_id, trusted.calendar_id, ownership.block_id):
            raise BindingConflict("calendar_event_identity_mismatch")
        block = await self.registry.get_by_block(context, ownership.block_id)
        event = await self.registry.get_by_event(context, ownership.event_id)
        if block is None or event is None or block != event:
            raise BindingConflict("calendar_registry_join_mismatch")
        block = OwnedBlockRecord.model_validate(block)
        original = OwnedBlockRecord.model_validate(block.model_dump() | {
            "state": "reserved", "confirmed_at": None, "confirmed_etag": None})
        if ownership != block and not (kind == "create" and ownership == original):
            raise BindingConflict("calendar_registry_record_mismatch")
        if kind == "update" and block.state != "confirmed":
            raise BindingConflict("calendar_confirmed_ownership_required")
        if kind == "create":


            saved = PlanVersion.model_validate(await self.plans.get(context,
                block.intent_plan.plan_id, block.intent_plan.version))
            self.clients.policy.require_plan(context, saved)
            if (saved.payload_hash != block.intent_plan.payload_hash or
                    operation not in saved.payload.operations):
                raise BindingConflict("calendar_saved_create_mismatch")
        return operation, block

    async def _lease(self, context, lease):
        lease = CalendarLease.model_validate(lease)
        trusted = self.clients.policy.require_context(context)
        now = instant(self.clock())
        if ((lease.owner_id, lease.calendar_id) != (trusted.owner_id, trusted.calendar_id)
                or lease.state != "active" or not lease.acquired_at <= now < lease.expires_at):
            raise BindingConflict("calendar_mutation_lease_invalid")
        await self.coordination.assert_current(context, lease, now=now)
        self.clients.policy.require_context(context)

    async def _call(self, context, factory, *, mutation=False):
        trusted = self.clients.policy.require_context(context)

        failure = None
        try:
            managed = getattr(self.clients, 'calendar_async_request', None)
            if managed is not None:
                return await managed(context, calendar_id=trusted.calendar_id,
                                     request_factory=factory, mutation=mutation)
            return await asyncio.to_thread(self.clients.calendar_request, context,
                calendar_id=trusted.calendar_id, request_factory=factory, mutation=mutation)
        except WorkspaceAccessError as error:
            if mutation and error.outcome_unknown:
                failure = UnknownWriteOutcome(error.code)
            elif mutation and error.http_status == 412:
                failure = StaleSource("calendar_stale_version")
            elif not mutation:
                failure = IncompleteRead(error.code)
            else:
                raise
        raise failure from None

    async def _normalize(self, context, raw) -> CalendarEvent:
        try:
            if not isinstance(raw, dict) or raw.get("eventType", "default") != "default":
                raise ValueError()


            zone = "UTC"
            if any(isinstance(raw.get(key), dict) and "date" in raw[key] for key in ("start", "end")):
                now = instant(self.clock()).replace(microsecond=0)
                metadata = await self._call(context, lambda service, calendar: service.events().list(
                    calendarId=calendar, timeMin=now.isoformat(),
                    timeMax=(now + timedelta(seconds=1)).isoformat(), maxResults=1, fields="timeZone"))
                zone = iana_zone(metadata.get("timeZone"))
            event, issues = normalize_event(raw, zone)
            if issues:
                raise ValueError()
            return event
        except (ValueError, TypeError, ValidationError, OverflowError):
            raise IncompleteRead("calendar_event_observation_invalid") from None

    async def get_event(self, context: TrustedContext | TrustedRequestContext,
                        event_id: str) -> CalendarEvent | None:
        """Read-only, independent of mutation lease/approval lifetime.

        T07's current 404 is not_found_or_unshared, so it raises IncompleteRead;
        this implementation never invents confirmed absence/None from it.
        """
        context = self._request(context)
        event_id = TypeAdapter(ResourceId).validate_python(event_id)
        raw = await self._call(context, lambda service, calendar: service.events().get(
            calendarId=calendar, eventId=event_id, fields=SINGLE_EVENT_FIELDS))
        observed = await self._normalize(context, raw)
        if observed.event_id != event_id:
            raise IncompleteRead("calendar_event_identity_changed")
        return observed

    async def _response(self, context, operation, record, raw):
        failure = None
        try:
            observed = await self._normalize(context, raw)
            if compare_target(operation, record, observed) != TargetComparison.MATCHED:
                failure = UnknownWriteOutcome("calendar_write_response_mismatch")
        except IncompleteRead:
            failure = UnknownWriteOutcome("calendar_write_response_invalid")
        if failure is not None:
            raise failure from None
        return observed

    async def create(self, context: TrustedContext | TrustedRequestContext,
                     operation: Operation, ownership: OwnedBlockRecord,
                     lease: CalendarLease) -> CalendarEvent:
        context = self._request(context)
        operation, record = await self._registered(context, operation, ownership, kind="create")
        if record.state == "confirmed":
            observed = await self.get_event(context, operation.event_id)
            if compare_target(operation, record, observed) != TargetComparison.MATCHED:
                raise StaleSource("calendar_confirmed_create_changed")
            return observed
        body = {"id": operation.event_id, "summary": "CampusPulse study block",
                "status": "confirmed", "transparency": "opaque", **_times(operation.target.interval),
                "extendedProperties": {"private": ownership_properties(_marker(record))}}
        await self._lease(context, lease)
        conflict = False
        try:
            raw = await self._call(context, lambda service, calendar: service.events().insert(
                calendarId=calendar, body=body, sendUpdates="none", fields=SINGLE_EVENT_FIELDS), mutation=True)
        except WorkspaceAccessError as error:
            if error.http_status != 409:
                raise
            conflict = True
        if conflict:
            failure = None
            try:
                observed = await self.get_event(context, operation.event_id)
            except IncompleteRead:
                failure = UnknownWriteOutcome("calendar_create_collision_unresolved")
            if failure is not None:
                raise failure from None
            if compare_target(operation, record, observed) != TargetComparison.MATCHED:
                raise BindingConflict("calendar_create_collision")
        else:
            observed = await self._response(context, operation, record, raw)

        failure = None
        try:
            await self.registry.confirm(context, record, observed=observed, at=instant(self.clock()))
        except (PortError, AccessDenied):


            failure = UnknownWriteOutcome("calendar_creation_confirmation_unresolved")
        if failure is not None:
            raise failure from None
        return observed

    async def update(self, context: TrustedContext | TrustedRequestContext,
                     operation: Operation, ownership: OwnedBlockRecord,
                     lease: CalendarLease) -> CalendarEvent:
        context = self._request(context)
        operation, record = await self._registered(context, operation, ownership, kind="update")
        before = await self.get_event(context, operation.event_id)
        if compare_target(operation, record, before) != TargetComparison.NO_EFFECT:
            raise StaleSource("calendar_update_source_changed")
        body = _times(operation.target.interval)

        def patch(service, calendar):
            request = service.events().patch(calendarId=calendar, eventId=operation.event_id,
                body=body, sendUpdates="none", fields=SINGLE_EVENT_FIELDS)
            request.headers["If-Match"] = operation.expected_etag
            return request

        await self._lease(context, lease)
        raw = await self._call(context, patch, mutation=True)
        return await self._response(context, operation, record, raw)


class CalendarAdapter(CalendarWriter):
    """Explicit composition implementing all four methods of T03 CalendarAdapter."""

    def __init__(self, clients: GoogleWorkspaceClients, snapshots, registry: OwnedBlockRegistry,
                 coordination: CalendarCoordination, plans: PlanStore, **options) -> None:
        super().__init__(clients, registry, coordination, plans, **options)
        self.reader = CalendarReader(clients, snapshots, registry, **options)

    async def read(self, context: TrustedContext | TrustedRequestContext,
                   horizon: Interval):
        return await self.reader.read(context, horizon)
