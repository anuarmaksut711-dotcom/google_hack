"""T17 offline integration: real T07 SDK runner + T05/T06 repositories.

Only HTTP/identity/transaction documents are fake. No cloud writes or consent
claims. The SDK builds actual Calendar requests from pinned static discovery.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from datetime import timedelta
import json
import re
import socket
from urllib.parse import parse_qs, unquote, urlsplit

import httplib2
import pytest
import googleapiclient.discovery

from campuspulse_recovery.domain.models import (
    CalendarEvent, Interval, Operation, OwnedBlockRecord,
    RecurrenceInstance, canonical_hash,
)
from campuspulse_recovery.domain.ports import BindingConflict, IncompleteRead, StaleSource, UnknownWriteOutcome
from campuspulse_recovery.integrations import google_clients
from campuspulse_recovery.integrations.calendar_read import ownership_properties
from campuspulse_recovery.integrations.calendar_write import (
    CalendarAdapter, TargetComparison, compare_target,
    deterministic_event_id, target_fingerprint,
)
from campuspulse_recovery.integrations.google_clients import GoogleWorkspaceClients
from campuspulse_recovery.integrations.google_credentials import WorkspaceAccessError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
from campuspulse_recovery.storage.operations import target_fingerprint as durable_fingerprint
from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
from campuspulse_recovery.storage.plans import _encode, _key
from tests.storage.test_owned_blocks import observed, reservation
from tests.storage.test_plans import Harness as StorageHarness, NOW, make_plan, run, sources


class FakeAPI:
    """In-memory Calendar HTTP edge, intentionally no auto retry or adoption."""

    def __init__(self):
        self.events = {}
        self.calls = []
        self.failures = {}
        self.after_write = None
        self.before_patch = None
        self.native_zone = "America/New_York"
        self.override_get = None

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        parsed = urlsplit(uri)
        path = unquote(parsed.path)
        collection = path.endswith("/events")
        identity = path.rsplit("/", 1)[-1]
        query = parse_qs(parsed.query)
        body = json.loads(body) if body else None
        self.calls.append((method, path, query, deepcopy(body), dict(headers or {})))

        def response(status, value):
            return httplib2.Response({"status": str(status), "content-type": "application/json"}), json.dumps(value).encode()

        if method in self.failures:
            failure = self.failures.pop(method)
            if isinstance(failure, Exception):
                raise failure
            return response(failure, {"error": {"message": "synthetic"}})
        if method == "GET":
            if collection:
                return response(200, {"timeZone": self.native_zone, "items": list(self.events.values())})
            if self.override_get is not None:
                return response(200, self.override_get)
            return response(200, self.events[identity]) if identity in self.events else response(404, {})
        if method == "POST":
            assert collection
            identity = body["id"]
            if identity in self.events:
                return response(409, {})
            self.events[identity] = {**body, "etag": '"created"',
                "htmlLink": "https://calendar.google.com/calendar/event?eid=synthetic"}
        elif method == "PATCH":
            if self.before_patch:
                self.before_patch(self.events[identity])
            if (headers or {}).get("If-Match") != self.events[identity]["etag"]:
                return response(412, {})
            self.events[identity].update(deepcopy(body))
            self.events[identity]["etag"] = '"updated"'
        else:
            raise AssertionError("Unexpected API method")
        if self.after_write:
            return self.after_write(self.events[identity], response)
        return response(200, self.events[identity])

    def close(self):
        pass

    @property
    def mutations(self):
        return [c for c in self.calls if c[0] != "GET"]


class Harness(StorageHarness):
    def __init__(self, monkeypatch, *, bridge=True):
        super().__init__()
        self.current = ContextVar("t17-current", default=None)
        self.now = NOW + timedelta(seconds=2)
        self.api = FakeAPI()
        monkeypatch.setattr(google_clients, "create_workspace_credentials", lambda *a, **k: None)
        monkeypatch.setattr(google_clients, "_RequestsHttp", lambda *a, **k: self.api)
        self.clients = GoogleWorkspaceClients(self.policy, environment="local")
        self.registry = FirestoreOwnedBlockRegistry(self.documents, self.policy)
        self.coordination = FirestoreCalendarCoordination(self.documents, self.policy, clock=lambda: self.now)
        self.writer = CalendarAdapter(self.clients, self.snapshots, self.registry, self.coordination,
            self.plans, clock=lambda: self.now, request_context=self.current.get if bridge else None)

    @contextmanager
    def request(self, boundary="mcp", run="run-1"):
        with super().request(boundary, run) as context:
            token = self.current.set(context)
            try:
                yield context
            finally:
                self.current.reset(token)

    async def prepare(self, *, confirmed=False):
        assignments, calendar = sources(self.config)
        with self.request() as ctx:
            await self.snapshots.put_assignments(ctx, assignments)
            await self.snapshots.put_calendar(ctx, calendar)
        self.plan = make_plan(self.config, assignments, calendar)
        with self.request("backend") as ctx:
            await self.plans.create(ctx, self.plan)
        self.create = self.plan.payload.operations[0]
        self.record = reservation(self.plan)
        with self.request() as ctx:
            await self.registry.reserve(ctx, self.record)
            if confirmed:
                observation = observed(self.plan)
                self.record = await self.registry.confirm(ctx, self.record, observed=observation, at=self.now)
                self.api.events[self.record.event_id] = raw_event(observation)
            self.lease = await self.coordination.acquire(ctx, holder_id="writer-1", now=self.now,
                expires_at=self.now + timedelta(seconds=60))
        self.update = Operation(operation_id="saved-update-op", kind="update", event_id=self.create.event_id,
            target=self.create.target.model_dump() | {"interval": Interval(
                start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4))},
            expected_etag='"created-etag"', before=self.create.target.interval)
        return self


def raw_event(event):
    return {"id": event.event_id, "etag": event.etag, "status": event.status,
        "transparency": event.transparency,
        "start": {"dateTime": event.interval.start.isoformat()},
        "end": {"dateTime": event.interval.end.isoformat()},
        "extendedProperties": {"private": ownership_properties(event.campuspulse_marker)}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network is forbidden in T17 synthetic tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_deterministic_ids_and_canonical_fingerprint(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        identities = [deterministic_event_id("student-1", "calendar-1", f"block-{i}") for i in range(300)]
        assert len(set(identities)) == 300
        assert all(re.fullmatch(r"[0-9a-v]{5,1024}", x) for x in identities)
        assert deterministic_event_id("student-1", "calendar-1", "block-0") == identities[0]
        assert deterministic_event_id("student-2", "calendar-1", "block-0") not in identities
        assert deterministic_event_id("student-1", "calendar-2", "block-0") not in identities
        assert target_fingerprint(h.update) == durable_fingerprint(h.update) == canonical_hash(h.update)
        changed = Operation.model_validate(h.update.model_dump() | {"expected_etag": '"another"'})
        assert target_fingerprint(changed) != target_fingerprint(h.update)
        assert h.create.event_id == deterministic_event_id(h.record.owner_id, h.record.calendar_id, h.record.block_id)
    run(scenario())


def test_create_confirm_retry_and_frozen_adapter_read_composition(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        with h.request() as ctx:
            first = await h.writer.create(ctx.as_port_context(), h.create, h.record, h.lease)
            confirmed = await h.registry.get_by_block(ctx, h.record.block_id)
            assert confirmed == await h.registry.get_by_event(ctx, h.record.event_id)
            assert confirmed.state == "confirmed" and confirmed.confirmed_etag == first.etag

            replay = await h.writer.create(ctx, h.create, h.record, h.lease)
            assert replay == first
            snapshot = await h.writer.read(ctx, Interval(start=NOW, end=NOW + timedelta(hours=72)))
            assert snapshot.source.complete and snapshot.owned_blocks[0].event_id == first.event_id
        assert len(h.api.mutations) == 1
        method, path, query, body, headers = h.api.mutations[0]
        assert method == "POST" and query["sendUpdates"] == ["none"]
        assert set(body) == {"id", "summary", "status", "transparency", "start", "end", "extendedProperties"}
        assert len(body["extendedProperties"]["private"]) == 6
        assert body["id"] == h.create.event_id
        assert all(h.config.student.calendar_id in c[1] for c in h.api.calls)
    run(scenario())


def test_create_409_reconciles_reserved_same_logical_event_without_duplicate(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        h.api.events[h.record.event_id] = raw_event(observed(h.plan))
        with h.request() as ctx:
            result = await h.writer.create(ctx, h.create, h.record, h.lease)
            assert result.event_id == h.record.event_id
            assert (await h.registry.get_by_block(ctx, h.record.block_id)).state == "confirmed"
        assert [c[0] for c in h.api.calls] == ["POST", "GET"]
        assert len(h.api.events) == 1
    run(scenario())


@pytest.mark.parametrize("change", ["marker", "target", "schema", "cancelled", "foreign_event"])
def test_create_collision_never_adopts_or_changes_an_existing_event(monkeypatch, change):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        raw = raw_event(observed(h.plan))
        if change == "marker":
            raw["extendedProperties"]["private"]["campuspulse.block_id"] = "other-block"
        elif change == "target":
            raw["start"]["dateTime"] = NOW.isoformat()
        elif change == "schema":
            raw["extendedProperties"]["private"].pop("campuspulse.schema_version")
        elif change == "cancelled":
            raw["status"] = "cancelled"
        else:
            raw.pop("extendedProperties")
        h.api.events[h.record.event_id] = raw
        before = deepcopy(h.api.events)
        with h.request() as ctx:
            with pytest.raises((BindingConflict, UnknownWriteOutcome)):
                await h.writer.create(ctx, h.create, h.record, h.lease)
            assert (await h.registry.get_by_block(ctx, h.record.block_id)).state == "reserved"
        assert before == h.api.events and len(h.api.mutations) == 1
    run(scenario())


def test_update_uses_if_match_preserves_unrelated_fields_and_original_confirmation(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        unrelated = {"summary": "Manual title", "description": "private user text",
            "attendees": [{"email": "existing@example.invalid"}], "colorId": "5",
            "reminders": {"useDefault": False, "overrides": []}}
        h.api.events[h.record.event_id].update(deepcopy(unrelated))
        with h.request() as ctx:
            result = await h.writer.update(ctx, h.update, h.record, h.lease)
            assert compare_target(h.update, h.record, result) == TargetComparison.MATCHED
            assert await h.registry.get_by_block(ctx, h.record.block_id) == h.record
            assert await h.registry.get_by_event(ctx, h.record.event_id) == h.record
        assert len(h.api.mutations) == 1
        method, _, query, body, headers = h.api.mutations[0]
        assert method == "PATCH" and set(body) == {"start", "end"}
        assert headers["If-Match"] == h.update.expected_etag and query["sendUpdates"] == ["none"]
        assert all(h.api.events[h.record.event_id][k] == v for k, v in unrelated.items())
    run(scenario())


@pytest.mark.parametrize("when", ["before_get", "between_get_and_patch"])
def test_stale_manual_update_is_preserved_and_never_retried(monkeypatch, when):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        def edit(raw):
            raw["summary"] = "User edit"
            raw["etag"] = '"user-edited"'
        if when == "before_get":
            edit(h.api.events[h.record.event_id])
        else:
            h.api.before_patch = edit
        with h.request() as ctx:
            with pytest.raises(StaleSource):
                await h.writer.update(ctx, h.update, h.record, h.lease)
        assert h.api.events[h.record.event_id]["summary"] == "User edit"
        assert h.api.events[h.record.event_id]["start"]["dateTime"] == h.update.before.start.isoformat()
        assert len(h.api.mutations) == (when == "between_get_and_patch")
        if h.api.mutations:
            assert h.api.mutations[0][4]["If-Match"] == h.update.expected_etag
    run(scenario())


@pytest.mark.parametrize("guard", ["kind", "assignment", "unregistered", "reserved_update", "calendar", "owner", "index_join", "saved_body", "saved_hash"])
def test_operation_resource_and_both_registry_guards_before_api(monkeypatch, guard):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=guard != "reserved_update")
        operation, record = h.update, h.record
        method = h.writer.update
        if guard == "kind":
            operation = h.create
        elif guard == "assignment":
            operation = Operation.model_validate(operation.model_dump() | {
                "target": operation.target.model_dump() | {"assignment_id": "foreign"}})
        elif guard == "unregistered":
            h.documents.data = {k: v for k, v in h.documents.data.items() if "/blocks/" not in k and "/events/" not in k}
        elif guard in ("calendar", "owner"):
            values = record.model_dump() | {"calendar_id" if guard == "calendar" else "owner_id": "foreign"}
            values["event_id"] = deterministic_event_id(values["owner_id"], values["calendar_id"], values["block_id"])
            record = OwnedBlockRecord.model_validate(values)
        elif guard == "index_join":
            path = f"cp_calendars/{_key(record.calendar_id)}/events/{_key(record.event_id)}"
            other = OwnedBlockRecord.model_validate(record.model_dump() | {"confirmed_etag": '"different-index"'})
            h.documents.data[path] = _encode("owned_block", other)
        elif guard == "saved_body":
            method, operation = h.writer.create, Operation.model_validate(h.create.model_dump() | {
                "target": h.update.target})
        elif guard == "saved_hash":
            method, operation = h.writer.create, h.create
            record = OwnedBlockRecord.model_validate(record.model_dump() | {
                "intent_plan": record.intent_plan.model_dump() | {"payload_hash": "0" * 64}})
            for kind, identity in (("blocks", record.block_id), ("events", record.event_id)):
                path = f"cp_calendars/{_key(record.calendar_id)}/{kind}/{_key(identity)}"
                h.documents.data[path] = _encode("owned_block", record)
        with h.request() as ctx:
            with pytest.raises((BindingConflict, AccessDenied)):
                await method(ctx, operation, record, h.lease)
        assert not h.api.calls
    run(scenario())


@pytest.mark.parametrize("guard", ["expired", "released", "generation", "recovery", "holder", "calendar", "acquired_future"])
def test_durable_lease_guard_blocks_mutation(monkeypatch, guard):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        lease = h.lease
        with h.request() as ctx:
            if guard == "expired":
                h.now = lease.expires_at
            elif guard == "released":
                await h.coordination.release(ctx, lease)
            else:
                changes = {"generation": {"generation": 2}, "holder": {"holder_id": "other-writer"},
                    "recovery": {"state": "recovery_required"}, "calendar": {"calendar_id": "foreign"},
                    "acquired_future": {"acquired_at": h.now + timedelta(seconds=1)}}
                lease = type(lease).model_validate(lease.model_dump() | changes[guard])
            with pytest.raises(BindingConflict):
                await h.writer.create(ctx, h.create, h.record, lease)
        assert not h.api.calls
    run(scenario())


@pytest.mark.parametrize("guard", ["copied", "parsed", "raw", "no_bridge", "backend", "closed", "foreign_policy", "other_current_dto"])
def test_active_mcp_capability_and_exact_dto_bridge(monkeypatch, guard):
    async def scenario():
        h = await Harness(monkeypatch, bridge=guard != "no_bridge").prepare()
        with h.request("backend" if guard == "backend" else "mcp") as ctx:
            context = ctx
            if guard == "copied":
                context = copy(ctx.as_port_context())
            elif guard == "parsed":
                context = type(ctx.trusted).model_validate_json(ctx.trusted.model_dump_json())
            elif guard == "raw":
                context = ctx.trusted.model_dump()
            elif guard == "no_bridge":
                context = ctx.as_port_context()
            elif guard == "foreign_policy":
                from campuspulse_recovery.security.resources import ResourcePolicy
                h.clients.policy = ResourcePolicy(h.config, environment="local")
            elif guard == "other_current_dto":
                with h.request() as other:
                    context = other.as_port_context()
            if guard != "closed":
                with pytest.raises(AccessDenied):
                    await h.writer.create(context, h.create, h.record, h.lease)
        if guard == "closed":
            with pytest.raises(AccessDenied):
                await h.writer.get_event(context, h.create.event_id)
        assert not h.api.calls
    run(scenario())


@pytest.mark.parametrize("kind", ["create", "update"])
@pytest.mark.parametrize("failure", ["timeout_after_success", "5xx", "parse", "408"])
def test_dispatched_ambiguity_is_unknown_and_no_hidden_retries(monkeypatch, kind, failure):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=kind == "update")
        operation = h.create if kind == "create" else h.update
        def after(raw, response):
            if failure == "timeout_after_success":
                raise TimeoutError("synthetic edge timeout")
            if failure == "parse":
                return httplib2.Response({"status": "200", "content-type": "application/json"}), b"invalid-json"
            return response(503 if failure == "5xx" else 408, {})
        h.api.after_write = after
        with h.request() as ctx:
            with pytest.raises(UnknownWriteOutcome):
                await getattr(h.writer, kind)(ctx, operation, h.record, h.lease)
            assert len(h.api.mutations) == 1

            h.now = h.lease.expires_at
            actual = await h.writer.get_event(ctx, operation.event_id)
            assert compare_target(operation, h.record, actual) == TargetComparison.MATCHED
            assert len(h.api.mutations) == 1
            if kind == "create":
                assert (await h.registry.get_by_block(ctx, h.record.block_id)).state == "reserved"
    run(scenario())


@pytest.mark.parametrize("kind", ["create", "update"])
@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_known_rejection_is_not_unknown_or_success_and_not_retried(monkeypatch, kind, status):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=kind == "update")
        h.api.failures["POST" if kind == "create" else "PATCH"] = status
        with h.request() as ctx:
            with pytest.raises(WorkspaceAccessError) as error:
                await getattr(h.writer, kind)(ctx, getattr(h, kind), h.record, h.lease)
            assert error.value.http_status == status and not error.value.outcome_unknown
        assert len(h.api.mutations) == 1
    run(scenario())


@pytest.mark.parametrize("status", [401, 403, 404, 503, "timeout"])
def test_get_failure_never_proves_absence(monkeypatch, status):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        h.api.failures["GET"] = TimeoutError() if status == "timeout" else status

        if status == 503:
            original = h.api.request
            def request(*a, **k):
                h.api.failures["GET"] = 503
                return original(*a, **k)
            monkeypatch.setattr(h.api, "request", request)
        with h.request() as ctx:
            with pytest.raises(IncompleteRead):
                await h.writer.get_event(ctx, h.record.event_id)
        assert not h.api.mutations
    run(scenario())


def test_confirmed_missing_event_is_never_recreated(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        h.api.events.clear()
        with h.request() as ctx:
            with pytest.raises(IncompleteRead, match="not_found_or_unshared"):
                await h.writer.create(ctx, h.create, h.record, h.lease)
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("bad", ["eventType", "recurrence", "all_day", "foreign_marker", "missing_etag", "changed_target", "same_etag_target"])
def test_update_refuses_unsupported_or_changed_actual_state(monkeypatch, bad):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        raw = h.api.events[h.record.event_id]
        if bad == "eventType":
            raw["eventType"] = "focusTime"
        elif bad == "recurrence":
            raw["recurrence"] = ["RRULE:FREQ=DAILY"]
        elif bad == "all_day":
            raw["start"], raw["end"] = {"date": "2026-11-01"}, {"date": "2026-11-02"}
        elif bad == "foreign_marker":
            raw["extendedProperties"]["private"]["campuspulse.owner_id"] = "foreign"
        elif bad == "missing_etag":
            raw.pop("etag")
        else:
            raw["start"] = {"dateTime": h.update.target.interval.start.isoformat()}
            raw["end"] = {"dateTime": h.update.target.interval.end.isoformat()}
            if bad == "changed_target":
                raw["etag"] = '"new"'
        before = deepcopy(h.api.events)
        with h.request() as ctx:
            with pytest.raises((StaleSource, IncompleteRead)):
                await h.writer.update(ctx, h.update, h.record, h.lease)
        assert not h.api.mutations and before == h.api.events
    run(scenario())


def test_all_day_read_uses_actual_native_zone_not_student_zone(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        raw = h.api.events[h.record.event_id]
        raw["start"], raw["end"] = {"date": "2026-11-01"}, {"date": "2026-11-02"}
        with h.request() as ctx:
            event = await h.writer.get_event(ctx, h.record.event_id)
        assert event.interval == Interval(start="2026-11-01T04:00:00Z", end="2026-11-02T05:00:00Z")
        assert event.all_day_start.isoformat() == "2026-11-01"
        assert len(h.api.calls) == 2 and h.api.calls[-1][2]["fields"] == ["timeZone"]
    run(scenario())


@pytest.mark.parametrize("change,expected", [
    ("target_new", "matched"), ("target_old", "changed"), ("before_old", "no_effect"),
    ("before_new", "changed"), ("other_new", "changed"), ("missing_etag", "unknown"),
    ("foreign", "changed"), ("all_day", "changed"), ("recurring", "changed"),
    ("cancelled", "changed"), ("transparent", "changed"), ("tentative", "changed"),
    ("missing", "missing"), ("read_failed", "unknown"),
])
def test_readback_requires_actual_target_and_version_evidence(monkeypatch, change, expected):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        event = observed(h.plan)
        fields = event.model_dump()
        if change.startswith("target"):
            fields["interval"] = h.update.target.interval
        elif change == "other_new":
            fields["interval"] = Interval(start=NOW + timedelta(hours=5), end=NOW + timedelta(hours=6))
        if change.endswith("new"):
            fields["etag"] = '"new"'
        elif change == "missing_etag":
            fields["etag"] = None
        elif change == "foreign":
            fields["campuspulse_marker"] = None
        elif change == "all_day":
            fields.update(all_day_start="2026-10-18", all_day_end="2026-10-19")
        elif change == "recurring":
            fields["recurrence"] = RecurrenceInstance(series_event_id="series", original_start_time=NOW)
        elif change in ("cancelled", "tentative"):
            fields["status"] = change
        elif change == "transparent":
            fields["transparency"] = "transparent"
        event = None if change in ("missing", "read_failed") else CalendarEvent.model_validate(fields)
        assert compare_target(h.update, h.record, event, read_complete=change != "read_failed") == expected
    run(scenario())


@pytest.mark.parametrize("bad_response", ["wrong_id", "missing_etag", "unchanged_etag", "foreign_marker"])
def test_success_http_with_unproven_response_stays_unknown(monkeypatch, bad_response):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        def after(raw, response):
            raw = deepcopy(raw)
            if bad_response == "wrong_id":
                raw["id"] = "wrong-id"
            elif bad_response == "missing_etag":
                raw.pop("etag")
            elif bad_response == "unchanged_etag":
                raw["etag"] = h.update.expected_etag
            else:
                raw.pop("extendedProperties")
            return response(200, raw)
        h.api.after_write = after
        with h.request() as ctx:
            with pytest.raises(UnknownWriteOutcome):
                await h.writer.update(ctx, h.update, h.record, h.lease)
        assert len(h.api.mutations) == 1
    run(scenario())


def test_failed_registry_confirmation_preserves_unknown_external_effect(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        async def fail(*args, **kwargs):
            raise BindingConflict("synthetic_storage_failure")
        monkeypatch.setattr(h.registry, "confirm", fail)
        with h.request() as ctx:
            with pytest.raises(UnknownWriteOutcome, match="confirmation_unresolved"):
                await h.writer.create(ctx, h.create, h.record, h.lease)
            actual = await h.writer.get_event(ctx, h.record.event_id)
            assert compare_target(h.create, h.record, actual) == TargetComparison.MATCHED
        assert len(h.api.mutations) == 1
    run(scenario())


def test_409_with_failed_read_is_unknown_not_absence_or_adoption(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare()
        h.api.events[h.record.event_id] = raw_event(observed(h.plan))
        h.api.failures["GET"] = 404
        with h.request() as ctx:
            with pytest.raises(UnknownWriteOutcome, match="collision_unresolved"):
                await h.writer.create(ctx, h.create, h.record, h.lease)
            assert (await h.registry.get_by_block(ctx, h.record.block_id)).state == "reserved"
        assert len(h.api.mutations) == 1
    run(scenario())


@pytest.mark.parametrize("zone", [None, "invalid-zone"])
def test_all_day_missing_native_zone_fails_without_fallback(monkeypatch, zone):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        h.api.native_zone = zone
        raw = h.api.events[h.record.event_id]
        raw["start"], raw["end"] = {"date": "2026-11-01"}, {"date": "2026-11-02"}
        with h.request() as ctx:
            with pytest.raises(IncompleteRead):
                await h.writer.get_event(ctx, h.record.event_id)
        assert not h.api.mutations
    run(scenario())


def test_get_event_rejects_mismatched_actual_identity(monkeypatch):
    async def scenario():
        h = await Harness(monkeypatch).prepare(confirmed=True)
        h.api.override_get = deepcopy(h.api.events[h.record.event_id]) | {"id": "other-event"}
        with h.request() as ctx:
            with pytest.raises(IncompleteRead, match="identity_changed"):
                await h.writer.get_event(ctx, h.record.event_id)
    run(scenario())
