"""T09 offline tests: fake Google edge/auth/documents, actual T05 repositories.

No live Firestore/Calendar evidence. No socket/credentials required. The T05
fake-only event loop allows actual asyncio.to_thread offloading on Windows.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from campuspulse_recovery.domain.models import (
    CalendarSnapshot, Interval, OwnershipMarker, ReadCalendarInput,
    calendar_content_hash,
)
from campuspulse_recovery.domain.ports import IncompleteRead, TrustedContext
from campuspulse_recovery.integrations.calendar_read import (
    EVENT_FIELDS, CalendarReader, CalendarReadResult, busy_intervals, normalize_event,
    ownership_properties,
)
from campuspulse_recovery.integrations.google_clients import GoogleWorkspaceClients
from campuspulse_recovery.integrations.google_credentials import WorkspaceAccessError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
from tests.storage.test_owned_blocks import observed, reservation
from tests.storage.test_plans import Harness as StorageHarness, NOW, make_plan, run, sources

HORIZON = Interval(start=NOW, end=NOW + timedelta(hours=72))
REQUEST = ReadCalendarInput(horizon=HORIZON)


def event(identity="event-1", *, start=NOW, end=None, **fields):
    first = {"dateTime": start.isoformat()} if isinstance(start, datetime) else start
    end = end if end is not None else (start if isinstance(start, datetime) else NOW) + timedelta(hours=1)
    last = {"dateTime": end.isoformat()} if isinstance(end, datetime) else end
    return {"id": identity, "etag": '"etag-1"', "status": "confirmed",
            "start": first, "end": last,
            "htmlLink": "https://calendar.google.com/calendar/event?eid=synthetic", **fields}


def page(*items, zone="America/New_York", **fields):
    return {"timeZone": zone, "items": list(items), **fields}


class Harness(StorageHarness):
    def __init__(self, **limits):
        super().__init__()
        self.current = ContextVar("t09-current", default=None)
        self.registry = FirestoreOwnedBlockRegistry(self.documents, self.policy)
        self.pages = [page(event())]
        self.calls = []
        self.in_google = False
        clients = SimpleNamespace(policy=self.policy, calendar_request=self.calendar_request)
        self.reader = CalendarReader(clients, self.snapshots, self.registry,
            clock=lambda: NOW + timedelta(seconds=2), request_context=self.current.get, **limits)

    @contextmanager
    def request(self, boundary="mcp", run="run-1"):
        with super().request(boundary, run) as ctx:
            token = self.current.set(ctx)
            try:
                yield ctx
            finally:
                self.current.reset(token)

    def calendar_request(self, context, *, calendar_id, request_factory, mutation=False):
        self.policy.require_calendar(context, calendar_id)
        assert not mutation
        options = {}

        def collect(**kwargs):
            options.update(kwargs)

        request_factory(SimpleNamespace(events=lambda: SimpleNamespace(list=collect)), calendar_id)
        self.calls.append(options)
        self.in_google = True
        try:
            value = self.pages[len(self.calls) - 1]
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)
        finally:
            self.in_google = False

    async def owned_event(self, *, confirmed=True):
        assignments, calendar = sources(self.config)
        with self.request() as ctx:
            await self.snapshots.put_assignments(ctx, assignments)
            await self.snapshots.put_calendar(ctx, calendar)
        plan = make_plan(self.config, assignments, calendar)
        with self.request("backend") as ctx:
            await self.plans.create(ctx, plan)
        record = reservation(plan)
        observation = observed(plan)
        with self.request() as ctx:
            await self.registry.reserve(ctx, record)
            if confirmed:
                await self.registry.confirm(ctx, record, observed=observation, at=NOW + timedelta(seconds=1))
        raw = event(record.event_id, start=observation.interval.start,
                    end=observation.interval.end, etag='"fresh-etag"',
                    extendedProperties={"private": ownership_properties(observation.campuspulse_marker)})
        return raw, record


def codes(snapshot):
    return {issue.code for issue in snapshot.source.issues}


def test_pagination_empty_page_with_token_full_horizon_and_authoritative_backend_reload():
    async def scenario():
        h = Harness()
        h.pages = [page(event("z"), nextPageToken="page-2"),
                   page(nextPageToken="page-3"), page(event("a", start=NOW + timedelta(hours=71)))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx.as_port_context(), HORIZON)
            assert snapshot.source.complete and snapshot.recurrence_expanded
            assert len(h.calls) == 3
            assert [item.event_id for item in snapshot.events] == ["a", "z"]
            assert snapshot.source.covered_interval == HORIZON
            assert snapshot.source.timezone == "America/New_York" != ctx.trusted.timezone
            assert snapshot.source.reference.read_at == NOW + timedelta(seconds=2)
            assert snapshot.source.reference.snapshot_id.startswith("cs_")
            assert snapshot.source.reference.content_hash == calendar_content_hash(snapshot.events, (), True)
            assert h.documents.data
        with h.request("backend") as backend:
            saved = await FirestoreSnapshotStore(h.documents, h.policy).get_calendar(backend, snapshot.source.reference)
            assert saved == snapshot
            view = CalendarReadResult(snapshot=saved)
            assert view.status == "complete" and view.fetched_at == snapshot.source.reference.read_at
            assert view.ownership == {"a": "external", "z": "external"}
            assert view.busy_intervals[-1].end == HORIZON.end
        for index, options in enumerate(h.calls):
            assert options["calendarId"] == h.config.student.calendar_id
            assert options["timeMin"] == NOW.isoformat() and options["timeMax"] == HORIZON.end.isoformat()
            assert options["singleEvents"] and options["showDeleted"] and options["showHiddenInvitations"]
            assert options["fields"] == EVENT_FIELDS and options["maxResults"] == 250
            assert not {"q", "privateExtendedProperty", "eventTypes", "syncToken", "timeZone"} & options.keys()
            assert options.get("pageToken") == (None if index == 0 else f"page-{index + 1}")
        with h.request("backend", run="other-run") as backend:
            with pytest.raises(AccessDenied):
                await h.snapshots.get_calendar(backend, snapshot.source.reference)
    run(scenario())


def test_moved_recurrence_exception_and_cancelled_tombstone():
    async def scenario():
        h = Harness()
        original = {"dateTime": (NOW - timedelta(hours=24)).isoformat()}
        moved = event("series_moved", start=NOW + timedelta(hours=5), recurringEventId="series",
                      originalStartTime=original)
        cancelled = {"id": "series_cancelled", "status": "cancelled", "recurringEventId": "series",
                     "originalStartTime": {"dateTime": (NOW + timedelta(hours=2)).isoformat()}}
        h.pages = [page(moved, cancelled)]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert snapshot.source.complete
        by_id = {e.event_id: e for e in snapshot.events}
        assert by_id["series_moved"].recurrence.original_start_time == NOW - timedelta(hours=24)
        assert by_id["series_moved"].interval.start == NOW + timedelta(hours=5)
        assert by_id["series_cancelled"].interval is None and not by_id["series_cancelled"].busy
        assert busy_intervals(snapshot) == (by_id["series_moved"].interval,)
    run(scenario())


@pytest.mark.parametrize("zone,first,last,start,end", [
    ("America/New_York", "2026-11-01", "2026-11-02", "2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z"),
    ("America/New_York", "2026-03-08", "2026-03-09", "2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z"),
    ("Asia/Almaty", "2026-10-18", "2026-10-20", "2026-10-17T19:00:00Z", "2026-10-19T19:00:00Z"),
])
def test_all_day_exclusive_bounds_native_zone_and_dst(zone, first, last, start, end):
    raw = {
        "id": "all-day", "etag": '"day"', "start": {"date": first}, "end": {"date": last}}
    item, issues = normalize_event(raw, zone)
    assert not issues
    assert item.interval == Interval(start=start, end=end)
    assert item.all_day_start.isoformat() == first and item.all_day_end.isoformat() == last


def test_all_day_instance_original_date_and_unclipped_snapshot():
    async def scenario():
        h = Harness()
        raw = {"id": "all-day-instance", "etag": '"day"', "start": {"date": "2026-10-17"},
               "end": {"date": "2026-10-19"}, "recurringEventId": "series",
               "originalStartTime": {"date": "2026-10-18"}}
        h.pages = [page(raw)]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        item = snapshot.events[0]
        assert snapshot.source.complete
        assert item.recurrence.original_start_date.isoformat() == "2026-10-18"
        assert item.interval.start == datetime(2026, 10, 17, 4, tzinfo=UTC) < HORIZON.start
        assert busy_intervals(snapshot)[0].start == HORIZON.start
    run(scenario())


def test_tentative_opaque_busy_transparent_cancelled_nonbusy_and_adjacent_union():
    async def scenario():
        h = Harness()
        h.pages = [page(event("tentative", status="tentative"),
            event("adjacent", start=NOW + timedelta(hours=1)),
            event("transparent", start=NOW + timedelta(hours=4), transparency="transparent"),
            event("cancelled", start=NOW + timedelta(hours=6), status="cancelled"),
            event("before", start=NOW - timedelta(hours=1)),
            event("after", start=HORIZON.end))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert snapshot.source.complete
        assert {e.event_id for e in snapshot.events} == {"tentative", "adjacent", "transparent", "cancelled"}
        assert busy_intervals(snapshot) == (Interval(start=NOW, end=NOW + timedelta(hours=2)),)
    run(scenario())


def test_offset_is_instant_even_when_authored_zone_differs_from_response_zone():
    raw = event()
    raw["start"] = {"dateTime": "2026-10-18T18:00:00+05:00", "timeZone": "Europe/Amsterdam"}
    raw["end"] = {"dateTime": "2026-10-18T19:00:00+05:00", "timeZone": "Europe/Amsterdam"}
    item, issues = normalize_event(raw, "Asia/Almaty")
    assert not issues and item.interval.start == datetime(2026, 10, 18, 13, tzinfo=UTC)


@pytest.mark.parametrize("patch", [
    {"start": {}}, {"end": {}}, {"start": None}, {"start": {"dateTime": "2026-10-18T10:00:00"}},
    {"start": {"dateTime": "2026-10-18T10:00:00+05:60"}},
    {"start": {"dateTime": "2026-10-18T10:00:00Z", "date": "2026-10-18"}},
    {"start": {"dateTime": "2026-10-18T10:00:00Z", "timeZone": "Not/AZone"}},
    {"start": {"date": "2026-10-18"}},
    {"start": {"date": "2026-10-19"}, "end": {"date": "2026-10-18"}},
    {"start": {"date": "2026-02-30"}, "end": {"date": "2026-03-01"}},
    {"start": {"dateTime": (NOW + timedelta(hours=2)).isoformat()}},
    {"recurrence": ["RRULE:FREQ=DAILY"]}, {"recurrence": []},
    {"recurringEventId": "series"}, {"originalStartTime": {"dateTime": NOW.isoformat()}},
    {"recurringEventId": "series", "originalStartTime": {"date": "2026-10-18"}},
    {"status": "unknown"}, {"transparency": "unknown"}, {"id": "bad id"},
    {"endTimeUnspecified": True},
])
def test_malformed_time_recurrence_and_identity_fail_closed_and_save_error(patch):
    async def scenario():
        h = Harness()
        h.pages = [page(event(**patch))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
            assert not snapshot.source.complete and "calendar_event_invalid" in codes(snapshot)
            assert h.documents.data
            assert await h.snapshots.get_calendar(ctx, snapshot.source.reference) == snapshot
        with pytest.raises(IncompleteRead):
            busy_intervals(snapshot)
    run(scenario())


@pytest.mark.parametrize("later,code,expanded", [
    (page(nextPageToken="repeat"), "calendar_page_token_cycle", False),
    (page(nextPageToken=""), "calendar_page_token_invalid", False),
    (page(nextPageToken=123), "calendar_page_token_invalid", False),
    (page(nextPageToken=None), "calendar_page_token_invalid", False),
    ({"timeZone": "America/New_York", "items": {}}, "calendar_page_invalid", False),
    ({"items": []}, "calendar_timezone_invalid", False),
    (page(zone="Europe/London"), "calendar_timezone_changed", False),
    (WorkspaceAccessError("workspace_transport_failed"), "workspace_transport_failed", False),
    (WorkspaceAccessError("workspace_forbidden"), "workspace_forbidden", False),
])
def test_failed_or_partial_page_never_becomes_free_time(later, code, expanded):
    async def scenario():
        h = Harness()
        h.pages = [page(event(), nextPageToken="repeat"), later]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
            assert not snapshot.source.complete and code in codes(snapshot)
            assert snapshot.recurrence_expanded is expanded
            assert len(snapshot.events) == 1
            assert await h.snapshots.get_calendar(ctx, snapshot.source.reference) == snapshot
        assert len(h.calls) == 2
    run(scenario())


@pytest.mark.parametrize("limits,payloads,code", [
    ({"max_pages": 1}, [page(event(), nextPageToken="more")], "calendar_pages_truncated"),
    ({"max_events": 1}, [page(event("a"), event("b"))], "calendar_events_truncated"),
])
def test_server_budget_truncation_is_explicit(limits, payloads, code):
    async def scenario():
        h = Harness(**limits)
        h.pages = payloads
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert not snapshot.source.complete and code in codes(snapshot)
    run(scenario())


@pytest.mark.parametrize("value", [None, "Invalid/Zone", 4])
def test_no_native_timezone_cannot_manufacture_student_zone_or_snapshot(value):
    async def scenario():
        h = Harness()
        h.pages = [page(zone=value)]
        with h.request() as ctx:
            with pytest.raises(IncompleteRead, match="calendar_timezone_invalid"):
                await h.reader.read(ctx, HORIZON)
        assert not h.documents.data
    run(scenario())


def test_first_api_failure_returns_no_fabricated_snapshot():
    async def scenario():
        h = Harness()
        h.pages = [WorkspaceAccessError("workspace_transport_failed")]
        with h.request() as ctx:
            with pytest.raises(IncompleteRead, match="workspace_transport_failed"):
                await h.reader.read(ctx, HORIZON)
        assert not h.documents.data
    run(scenario())


def test_confirmed_registry_exact_marker_and_fresh_etag_define_own_blocks():
    async def scenario():
        h = Harness()
        raw, record = await h.owned_event()
        h.pages = [page(raw, event("foreign", summary="CampusPulse: this title is not ownership"))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert snapshot.source.complete
        assert snapshot.owned_blocks[0].event_id == record.event_id
        assert snapshot.owned_blocks[0].etag == '"fresh-etag"'
        assert CalendarReadResult(snapshot=snapshot).ownership == {record.event_id: "owned", "foreign": "external"}
        assert busy_intervals(snapshot, include_owned=False) == (Interval(start=NOW, end=NOW + timedelta(hours=1)),)
        with h.request("backend") as ctx:
            assert await h.snapshots.get_calendar(ctx, snapshot.source.reference) == snapshot
    run(scenario())


@pytest.mark.parametrize("field,value", [
    ("owner_id", "other"), ("calendar_id", "other-calendar"), ("block_id", "other-block"),
    ("assignment_id", "other-assignment"), ("create_operation_id", "other-create"),
    ("schema_version", "2.0"),
])
def test_marker_mismatch_never_makes_mutable_block(field, value):
    async def scenario():
        h = Harness()
        raw, record = await h.owned_event()
        raw["extendedProperties"]["private"]["campuspulse." + field] = value
        h.pages = [page(raw)]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert not snapshot.owned_blocks and not snapshot.source.complete
        assert snapshot.events[0].busy
        assert CalendarReadResult(snapshot=snapshot).ownership[record.event_id] == "external"
    run(scenario())


@pytest.mark.parametrize("variant", ["unregistered", "reserved", "missing_marker", "missing_etag",
                                     "transparent", "cancelled", "tentative", "recurring", "all_day", "special_type"])
def test_ownership_requires_registry_and_supported_single_event(variant):
    async def scenario():
        h = Harness()
        raw, record = await h.owned_event(confirmed=variant != "reserved")
        if variant == "unregistered":
            h.documents.data = {}
        if variant == "missing_marker":
            raw.pop("extendedProperties")
        if variant == "missing_etag":
            raw.pop("etag")
        if variant == "transparent":
            raw["transparency"] = "transparent"
        if variant in ("cancelled", "tentative"):
            raw["status"] = variant
        if variant == "recurring":
            raw.update(recurringEventId="series", originalStartTime=raw["start"])
        if variant == "all_day":
            raw.update(start={"date": "2026-10-17"}, end={"date": "2026-10-19"})
        if variant == "special_type":
            raw["eventType"] = "focusTime"
        h.pages = [page(raw)]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
            assert not snapshot.owned_blocks and not snapshot.source.complete
            assert await h.snapshots.get_calendar(ctx, snapshot.source.reference) == snapshot
        assert CalendarReadResult(snapshot=snapshot).ownership[record.event_id] == "external"
    run(scenario())


@pytest.mark.parametrize("patch", [{"etag": None}, {"etag": "bad etag"}, {"htmlLink": "javascript:ignore"},
    {"extendedProperties": {"private": {"campuspulse.owner_id": "fake"}}}])
def test_bad_optional_metadata_preserves_busy_event_with_incomplete_status(patch):
    async def scenario():
        h = Harness()
        h.pages = [page(event(**patch))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert not snapshot.source.complete and snapshot.events[0].busy
        assert snapshot.events[0].interval == Interval(start=NOW, end=NOW + timedelta(hours=1))
        assert snapshot.owned_blocks == ()
    run(scenario())


def test_duplicate_including_malformed_second_copy_removes_all_instances():
    async def scenario():
        h = Harness()
        h.pages = [page(event(), nextPageToken="next"), page(event(start={}))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert not snapshot.source.complete and snapshot.events == ()
        assert {"calendar_duplicate_event", "calendar_event_invalid"} <= codes(snapshot)
    run(scenario())


@pytest.mark.parametrize("kind", ["raw", "parsed", "copied", "no_resolver", "foreign_scope", "expired"])
def test_port_dto_cannot_supply_authority(kind):
    async def scenario():
        h = Harness()
        with h.request() as previous:
            old = previous.as_port_context()
        with h.request() as ctx:
            dto = ctx.as_port_context()
            supplied = {"raw": dto.model_dump(), "parsed": TrustedContext.model_validate(dto.model_dump()),
                "copied": copy(dto), "no_resolver": dto, "foreign_scope": old, "expired": old}[kind]
            if kind == "no_resolver":
                h.reader._request_context = None
            with pytest.raises(AccessDenied):
                await h.reader.read(supplied, HORIZON)
        assert not h.calls and not h.documents.data
    run(scenario())


def test_backend_expired_capability_and_overlong_horizon_are_rejected_before_google():
    async def scenario():
        h = Harness()
        with h.request("backend") as ctx:
            with pytest.raises(AccessDenied):
                await h.reader.read(ctx, HORIZON)
        with h.request() as ctx:
            with pytest.raises(ValidationError):
                await h.reader.read(ctx, Interval(start=NOW, end=NOW + timedelta(hours=73)))
        with pytest.raises(AccessDenied):
            await h.reader.read(ctx, HORIZON)
        assert not h.calls
    run(scenario())


def test_snapshot_persistence_failure_does_not_return_authoritative_reference():
    async def scenario():
        h = Harness()

        async def fail(callback):
            assert not h.in_google
            raise RuntimeError("fake_storage_unavailable")

        h.documents.transact = fail
        with h.request() as ctx:
            with pytest.raises(RuntimeError, match="fake_storage_unavailable"):
                await h.reader.read(ctx, HORIZON)
        assert len(h.calls) == 1 and not h.documents.data
    run(scenario())


def test_fractional_second_query_widens_then_locally_filters():
    async def scenario():
        h = Harness()
        horizon = Interval(start=NOW + timedelta(microseconds=500_000), end=NOW + timedelta(hours=1, microseconds=500_000))
        h.pages = [page(event("inside", start=horizon.start, end=horizon.end),
                        event("before", start=NOW - timedelta(hours=1), end=horizon.start),
                        event("after", start=horizon.end))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, horizon)
        assert snapshot.source.complete and [item.event_id for item in snapshot.events] == ["inside"]
        assert h.calls[0]["timeMin"] == NOW.isoformat()
        assert h.calls[0]["timeMax"] == (NOW + timedelta(hours=1, seconds=1)).isoformat()
    run(scenario())


def test_repeated_reads_have_immutable_new_references_and_canonical_stable_hash():
    async def scenario():
        h = Harness()
        h.pages = [page(event("b"), event("a")), page(event("a"), event("b"))]
        with h.request() as ctx:
            first = await h.reader.read(ctx, HORIZON)
            second = await h.reader.read(ctx, HORIZON)
            assert first.source.reference.snapshot_id != second.source.reference.snapshot_id
            assert first.source.reference.content_hash == second.source.reference.content_hash
            assert await h.snapshots.get_calendar(ctx, first.source.reference) == first
    run(scenario())


def test_pinned_calendar_sdk_builds_exact_read_request_without_network():
    from googleapiclient.discovery import build
    service = build("calendar", "v3", http=SimpleNamespace(), static_discovery=True, cache_discovery=False)
    calendar = "calendar-1@group.calendar.google.com"
    request = service.events().list(calendarId=calendar, timeMin=NOW.isoformat(), timeMax=HORIZON.end.isoformat(),
        singleEvents=True, showDeleted=True, showHiddenInvitations=True, maxResults=250, fields=EVENT_FIELDS)
    GoogleWorkspaceClients._check_request(request, "calendar", calendar, None, mutation=False)
    assert request.method == "GET" and "singleEvents=true" in request.uri


def test_empty_complete_calendar_is_distinct_from_failed_read():
    async def scenario():
        h = Harness(max_pages=1)
        h.pages = [{"timeZone": "UTC"}]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
            assert snapshot.source.complete and snapshot.recurrence_expanded
            assert not snapshot.events and not snapshot.owned_blocks
            assert busy_intervals(snapshot) == ()
            assert await h.snapshots.get_calendar(ctx, snapshot.source.reference) == snapshot
    run(scenario())


def test_long_external_id_can_have_safe_bounded_issue_reference():
    async def scenario():
        h = Harness()
        h.pages = [page(event("e" * 300, etag=None))]
        with h.request() as ctx:
            snapshot = await h.reader.read(ctx, HORIZON)
        assert not snapshot.source.complete and len(snapshot.events) == 1
        assert snapshot.source.issues[0].item_ref.startswith("event_")
    run(scenario())


def test_equal_dto_from_another_active_request_is_not_current_authority():
    async def scenario():
        h = Harness()
        with h.request() as earlier:
            dto = earlier.as_port_context()
            with h.request() as current:
                assert dto == current.as_port_context() and dto is not current.as_port_context()
                with pytest.raises(AccessDenied, match="foreign_port_context"):
                    await h.reader.read(dto, HORIZON)
        assert not h.calls
    run(scenario())


def test_authority_expiring_during_google_read_prevents_snapshot_return():
    async def scenario():
        h = Harness()
        before = h.reader.clients.calendar_request

        def expire(context, **kwargs):
            result = before(context, **kwargs)
            context._factory._clock = lambda: NOW + timedelta(minutes=6)
            return result

        h.reader.clients.calendar_request = expire
        with h.request() as ctx:
            with pytest.raises(AccessDenied, match="inactive_request_context"):
                await h.reader.read(ctx, HORIZON)
        assert len(h.calls) == 1 and not h.documents.data
    run(scenario())
