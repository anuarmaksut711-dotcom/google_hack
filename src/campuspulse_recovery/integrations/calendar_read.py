"""Bounded configured Calendar reads; no mutation or public MCP registration.

T03 snapshots remain the wire contract. T10 can wrap ``read(context, horizon)``.
Calendar data is untrusted; ownership requires the durable T05 registry AND the
exact private marker. No upstream helper code is copied: T03 time rules provide
strict normalization, including exclusive all-day ends and offset-aware times.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from ..domain.models import (
    CalendarEvent, CalendarSnapshot, Contract, Interval, OwnedBlock, OwnershipMarker,
    ReadCalendarInput, RecurrenceInstance, ResourceId, SourceIssue, SourceReference,
    SourceSnapshot, calendar_content_hash, canonical_hash,
)
from ..domain.ports import IncompleteRead, OwnedBlockRegistry, SnapshotStore, TrustedContext
from ..domain.time_rules import all_day_bounds, clip, iana_zone, instant, iso_date
from ..security.context import AccessDenied, TrustedRequestContext
from .google_clients import GoogleWorkspaceClients
from .google_credentials import WorkspaceAccessError

PAGE_SIZE = 250
MAX_PAGES = 100
MAX_EVENTS = 500
_RESOURCE = TypeAdapter(ResourceId)
_MARKER_PREFIX = "campuspulse."
_MARKER_FIELDS = tuple(OwnershipMarker.model_fields)
# Intentionally omit title/description/attendees. They cannot instruct this tool.
EVENT_FIELDS = ("timeZone,nextPageToken,items(id,etag,status,transparency,start,end,"
                "recurrence,recurringEventId,originalStartTime,htmlLink,"
                "extendedProperties/private,eventType,endTimeUnspecified)")


def _event_ref(event_id: str) -> str:
    # T03 resource IDs may be longer than issue Identifier (512 vs 256).
    return event_id if len(event_id) <= 256 else "event_" + canonical_hash([event_id])


def ownership_properties(marker: OwnershipMarker) -> dict[str, str]:
    """Canonical private properties for future write adapter interoperability.

    Pure encoding, not registration/ownership authority. All values are strings;
    schema_version is required. Other apps' private properties may coexist.
    """
    marker = OwnershipMarker.model_validate(marker)
    return {_MARKER_PREFIX + key: value for key, value in marker.model_dump().items()}


def _marker(raw: dict) -> OwnershipMarker | None:
    properties = raw.get("extendedProperties", {})
    if not isinstance(properties, dict):
        raise ValueError("calendar_marker_invalid")
    private = properties.get("private", {})
    if not isinstance(private, dict):
        raise ValueError("calendar_marker_invalid")
    fields = {key[len(_MARKER_PREFIX):]: value for key, value in private.items()
              if isinstance(key, str) and key.startswith(_MARKER_PREFIX)}
    if not fields:
        return None
    if set(fields) != set(_MARKER_FIELDS):
        raise ValueError("calendar_marker_invalid")
    return OwnershipMarker.model_validate(fields)


def _boundary(raw: object) -> tuple[str, object]:
    if not isinstance(raw, dict) or ("date" in raw) == ("dateTime" in raw):
        raise ValueError("calendar_boundary_invalid")
    if "timeZone" in raw:
        iana_zone(raw["timeZone"])
    # The offset is authoritative for a timed instant. Calendar may render it
    # in its response zone while preserving a different authored timeZone.
    return ("time", instant(raw["dateTime"])) if "dateTime" in raw else ("date", iso_date(raw["date"]))


def normalize_event(raw: dict, timezone: str) -> tuple[CalendarEvent, tuple[SourceIssue, ...]]:
    """Normalize one expanded instance, preserving original/full boundaries.

    Invalid identity/boundaries/recurrence raises ValueError; the reader records
    an incomplete source. Bad optional marker/link/etag retains known busy time
    with an issue and never grants mutation authority.
    """
    iana_zone(timezone)
    if not isinstance(raw, dict) or "recurrence" in raw or raw.get("endTimeUnspecified", False):
        raise ValueError("calendar_event_not_expanded")
    event_id = _RESOURCE.validate_python(raw.get("id"))
    issues = []
    interval = None
    dates = {}
    if raw.get("status", "confirmed") != "cancelled" or "start" in raw or "end" in raw:
        first_kind, first = _boundary(raw.get("start"))
        last_kind, last = _boundary(raw.get("end"))
        if first_kind != last_kind:
            raise ValueError("calendar_mixed_boundaries")
        if first_kind == "date":
            dates = {"all_day_start": first, "all_day_end": last}
            first, last = all_day_bounds(first, last, timezone)
        interval = Interval(start=first, end=last)
    recurrence = None
    if "recurringEventId" in raw or "originalStartTime" in raw:
        kind, original = _boundary(raw.get("originalStartTime"))
        if interval is not None and (kind == "date") != bool(dates):
            raise ValueError("calendar_recurrence_boundary_mismatch")
        recurrence = RecurrenceInstance(series_event_id=raw.get("recurringEventId"),
            **{"original_start_time" if kind == "time" else "original_start_date": original})
    try:
        marker = _marker(raw)
    except (ValueError, ValidationError):
        marker = None
        issues.append(SourceIssue(code="calendar_marker_invalid", item_ref=_event_ref(event_id)))
    # Optional metadata is independently checked without losing availability.
    metadata = {}
    for source_key, target_key in (("etag", "etag"), ("htmlLink", "event_link")):
        value = raw.get(source_key)
        if value is not None:
            try:
                # Validate selected field through the actual constrained DTO.
                CalendarEvent(event_id=event_id, interval=interval, status=raw.get("status", "confirmed"),
                              **{target_key: value})
            except (ValueError, ValidationError):
                issues.append(SourceIssue(code="calendar_" + target_key + "_invalid", item_ref=_event_ref(event_id)))
            else:
                metadata[target_key] = value
    # Deleted tombstones are only guaranteed to carry id (plus original
    # instance identity when provided). They are nonbusy and never mutable.
    if "etag" not in metadata and raw.get("status") != "cancelled":
        issues.append(SourceIssue(code="calendar_etag_missing", item_ref=_event_ref(event_id)))
    event = CalendarEvent(event_id=event_id, interval=interval,
        status=raw.get("status", "confirmed"), transparency=raw.get("transparency", "opaque"),
        recurrence=recurrence, campuspulse_marker=marker, **metadata, **dates)
    return event, tuple(issues)


def busy_intervals(snapshot: CalendarSnapshot, *, include_owned: bool = True) -> tuple[Interval, ...]:
    """Clipped busy union of a saved observation; incomplete is never free time.

    Reject incomplete snapshots instead of giving a misleading empty union.
    Adjacent intervals may share a union boundary, but do not conflict in T03.
    Original event/all-day boundaries remain intact in the canonical snapshot.
    """
    if not snapshot.source.complete:
        raise IncompleteRead("calendar_availability_incomplete")
    horizon = snapshot.source.covered_interval
    owned = {block.event_id for block in snapshot.owned_blocks}
    intervals = []
    for event in snapshot.events:
        if event.busy and event.interval is not None and (include_owned or event.event_id not in owned):
            bounds = clip(event.interval.start, event.interval.end, horizon.start, horizon.end)
            if bounds is not None:
                intervals.append(bounds)
    merged = []
    for first, last in sorted(intervals):
        if merged and first <= merged[-1].end:
            merged[-1] = Interval(start=merged[-1].start, end=max(last, merged[-1].end))
        else:
            merged.append(Interval(start=first, end=last))
    return tuple(merged)


class CalendarReadResult(Contract):
    """Derived local view, not a new wire schema or a source of authority.

    Backend first loads the snapshot through SnapshotStore.get_calendar.
    ``external`` includes spoofed/unconfirmed markers; only owned_blocks is the
    mutable candidate set (apply must independently revalidate all guards).
    """
    snapshot: CalendarSnapshot

    @property
    def status(self) -> Literal["complete", "error"]:
        return "complete" if self.snapshot.source.complete else "error"

    @property
    def fetched_at(self) -> datetime:
        return self.snapshot.source.reference.read_at

    @property
    def ownership(self) -> dict[str, Literal["owned", "external"]]:
        own = {block.event_id for block in self.snapshot.owned_blocks}
        return {event.event_id: "owned" if event.event_id in own else "external"
                for event in self.snapshot.events}

    @property
    def busy_intervals(self) -> tuple[Interval, ...]:
        return busy_intervals(self.snapshot)


class CalendarReader:
    """Server-created read service. Synchronous Google calls stay outside DB tx.

    Limits are server policy; hitting a limit is incomplete, never success. No
    resource IDs, timezone, pagination tokens or identity are model arguments.
    """

    def __init__(self, clients: GoogleWorkspaceClients, snapshots: SnapshotStore,
                 registry: OwnedBlockRegistry, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 request_context: Callable[[], TrustedRequestContext] | None = None,
                 max_pages: int = MAX_PAGES, max_events: int = MAX_EVENTS) -> None:
        if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
            raise ValueError("calendar_page_limit_invalid")
        if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
            raise ValueError("calendar_event_limit_invalid")
        self.clients, self.snapshots, self.registry = clients, snapshots, registry
        self.clock, self._request_context = clock, request_context
        self.max_pages, self.max_events = max_pages, max_events

    async def read(self, context: TrustedContext | TrustedRequestContext,
                   horizon: Interval) -> CalendarSnapshot:
        if isinstance(context, TrustedRequestContext):
            self.clients.policy.require_context(context)
            request = context
        else:
            if not isinstance(context, TrustedContext) or self._request_context is None:
                raise AccessDenied("server_request_context_required")
            request = self._request_context()
            self.clients.policy.require_context(request)
            if context is not request.as_port_context():
                raise AccessDenied("foreign_port_context")
        return await self.read_calendar(request, ReadCalendarInput(horizon=horizon))

    async def read_calendar(self, context: TrustedRequestContext,
                            request: ReadCalendarInput) -> CalendarSnapshot:
        request = ReadCalendarInput.model_validate(request)
        trusted = self.clients.policy.require_context(context)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        # Google ignores fractional seconds on the query bounds. Widen to whole
        # seconds, then filter actual intervals against the exact T03 horizon.
        lower = request.horizon.start.replace(microsecond=0)
        upper = request.horizon.end
        if upper.microsecond:
            try:
                upper = upper.replace(microsecond=0) + timedelta(seconds=1)
            except OverflowError:
                raise IncompleteRead("calendar_query_boundary_overflow") from None
        issues: list[SourceIssue] = []
        events: dict[str, CalendarEvent] = {}
        unsupported_owned: set[str] = set()
        duplicate_ids: set[str] = set()
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        token = None
        timezone = None
        expanded = False
        count = 0
        for page in range(self.max_pages):
            options = dict(timeMin=lower.isoformat(), timeMax=upper.isoformat(),
                           singleEvents=True, showDeleted=True, showHiddenInvitations=True,
                           maxResults=PAGE_SIZE, fields=EVENT_FIELDS)
            if token is not None:
                options["pageToken"] = token
            try:
                data = await asyncio.to_thread(self.clients.calendar_request, context,
                    calendar_id=trusted.calendar_id,
                    request_factory=lambda service, calendar: service.events().list(
                        calendarId=calendar, **options), mutation=False)
            except WorkspaceAccessError as error:
                issues.append(SourceIssue(code=error.code))
                break
            if not isinstance(data, dict):
                issues.append(SourceIssue(code="calendar_page_invalid"))
                break
            try:
                native_zone = iana_zone(data.get("timeZone"))
            except ValueError:
                issues.append(SourceIssue(code="calendar_timezone_invalid"))
                break
            if timezone is not None and timezone != native_zone:
                issues.append(SourceIssue(code="calendar_timezone_changed"))
                break
            timezone = native_zone
            rows = data.get("items", [])
            if not isinstance(rows, list):
                issues.append(SourceIssue(code="calendar_page_invalid"))
                break
            for offset, raw in enumerate(rows):
                count += 1
                item_ref = f"page-{page + 1}-item-{offset + 1}"
                if count > self.max_events:
                    issues.append(SourceIssue(code="calendar_events_truncated"))
                    break
                # Register identity even when the second copy is malformed.
                try:
                    event_id = _RESOURCE.validate_python(raw.get("id") if isinstance(raw, dict) else None)
                except ValidationError:
                    event_id = None
                if event_id is not None:
                    if event_id in seen_ids:
                        duplicate_ids.add(event_id)
                        issues.append(SourceIssue(code="calendar_duplicate_event", item_ref=item_ref))
                    seen_ids.add(event_id)
                try:
                    event, findings = normalize_event(raw, timezone)
                except (ValueError, ValidationError, TypeError):
                    issues.append(SourceIssue(code="calendar_event_invalid", item_ref=item_ref))
                    continue
                issues.extend(findings)
                # A moved exception uses actual bounds, not originalStartTime.
                if event.interval is not None and clip(event.interval.start, event.interval.end,
                        request.horizon.start, request.horizon.end) is None:
                    continue
                events[event.event_id] = event
                if raw.get("eventType", "default") != "default":
                    unsupported_owned.add(event.event_id)
            if count > self.max_events:
                break
            if "nextPageToken" not in data:
                expanded = True
                break
            token = data["nextPageToken"]
            if not isinstance(token, str) or not token or len(token) > 8192:
                issues.append(SourceIssue(code="calendar_page_token_invalid"))
                break
            if token in seen_tokens:
                issues.append(SourceIssue(code="calendar_page_token_cycle"))
                break
            seen_tokens.add(token)
        else:
            issues.append(SourceIssue(code="calendar_pages_truncated"))
        if timezone is None:
            # T03 requires the actual native IANA zone even for an error snapshot.
            # There is no safe calendar-zone fallback to the student's zone.
            # No snapshot/reference is returned when metadata could not be read.
            raise IncompleteRead(issues[-1].code) from None
        for event_id in duplicate_ids:
            events.pop(event_id, None)
        owned = []
        for event in sorted(events.values(), key=lambda item: item.event_id):
            record = await self.registry.get_by_event(context, event.event_id)
            marker = event.campuspulse_marker
            if record is None:
                if marker is not None:
                    issues.append(SourceIssue(code="calendar_marker_unregistered", item_ref=_event_ref(event.event_id)))
                continue
            expected = OwnershipMarker(owner_id=trusted.owner_id, calendar_id=trusted.calendar_id,
                block_id=record.block_id, assignment_id=record.assignment_id,
                create_operation_id=record.intent_operation_id)
            if ((record.owner_id, record.calendar_id, record.event_id) !=
                    (trusted.owner_id, trusted.calendar_id, event.event_id)
                    or marker != expected or record.state != "confirmed"):
                issues.append(SourceIssue(code="calendar_ownership_mismatch", item_ref=_event_ref(event.event_id)))
                continue
            if (event.etag is None or event.interval is None or not event.busy
                    or event.status != "confirmed" or event.recurrence is not None
                    or event.all_day_start is not None or event.event_id in unsupported_owned):
                issues.append(SourceIssue(code="calendar_owned_event_unsupported", item_ref=_event_ref(event.event_id)))
                continue
            owned.append(OwnedBlock(owner_id=record.owner_id, calendar_id=record.calendar_id,
                block_id=record.block_id, assignment_id=record.assignment_id, event_id=record.event_id,
                etag=event.etag, interval=event.interval, event_link=event.event_link))
        observed = tuple(sorted(events.values(), key=lambda item: item.event_id))
        blocks = tuple(sorted(owned, key=lambda item: item.block_id))
        snapshot = CalendarSnapshot(events=observed, owned_blocks=blocks, recurrence_expanded=expanded,
            source=SourceSnapshot(reference=SourceReference(snapshot_id="cs_" + uuid4().hex,
                kind="calendar", owner_id=trusted.owner_id, resource_id=trusted.calendar_id,
                run_id=trusted.run_id, config_revision=trusted.config_revision,
                read_at=instant(self.clock()), content_hash=calendar_content_hash(observed, blocks, expanded)),
                timezone=timezone, covered_interval=request.horizon, complete=not issues, issues=tuple(issues)))
        # Repository independently joins the registry inside its create-only tx.
        # Failure/expired scope means no authoritative result/reference is returned.
        await self.snapshots.put_calendar(context, snapshot)
        return snapshot
