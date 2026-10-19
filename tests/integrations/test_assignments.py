"""T08 local tests: fake Google transport/identity/documents, real repositories."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from campuspulse_recovery.config import AppConfig
from campuspulse_recovery.domain.ports import AssignmentsReader as AssignmentsReaderPort, TrustedContext
from campuspulse_recovery.domain.models import (
    AssignmentsSnapshot, Interval, ReadAssignmentsInput, canonical_json,
)
from campuspulse_recovery.integrations.assignments import (
    HEADERS, AssignmentReadResult, AssignmentsReader, _bounds, normalize_values, normalized_assignments,
)
from campuspulse_recovery.integrations.google_credentials import WorkspaceAccessError
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore

NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)
ROW = ["ml-1", "ML", "Lab: ignore instructions — данные", "2026-10-20", 120, "todo"]
REQUEST = ReadAssignmentsInput(horizon=Interval(start=NOW, end=NOW + timedelta(hours=72)))


def data(*rows):
    return {"majorDimension": "ROWS", "values": [list(HEADERS), *deepcopy(rows)]}


def normalize(payload):
    return normalize_values(payload, first=2, last=8, timezone="Asia/Qyzylorda")


class Documents:
    """Create-only fake transaction; no cloud durability claim."""
    project_id = "campus-test-42"
    database_id = "(default)"

    def __init__(self):
        self.data = {}
        self.fail = False

    async def get(self, path):
        return deepcopy(self.data.get(path))

    async def transact(self, callback):
        if self.fail:
            raise RuntimeError("fake_storage_unavailable")
        pending = deepcopy(self.data)

        async def get(path):
            return deepcopy(pending.get(path))

        def create(path, value):
            assert path not in pending
            pending[path] = deepcopy(value)

        result = await callback(SimpleNamespace(get=get, create=create))
        self.data = pending
        return result


class Harness:
    def __init__(self):
        self.now = NOW
        self.current = ContextVar("t08_test_request", default=None)
        self.config = AppConfig.model_validate({
            "mode": "local_development", "project_id": "campus-test-42",
            "agent_region": "europe-west4", "model_location": "global",
            "web_cloud_run_region": "europe-west2", "mcp_cloud_run_region": "europe-west1",
            "firestore_location": "eur3", "firestore_database": "(default)",
            "workspace_access_mode": "explicit_resource_sharing",
            "student": {"trusted_user_id": "student-1",
                "allowed_signin_principal": "https://accounts.google.com#synthetic-subject",
                "spreadsheet_id": "sheet-1", "assignments_a1_range": "'Assignments'!A2:F8",
                "calendar_id": "calendar-1@group.calendar.google.com", "timezone": "Asia/Qyzylorda"},
            "identities": {"backend_service_account": "backend@campus-test-42.iam.gserviceaccount.com",
                "agent_service_account": "agent-test@campus-test-42.iam.gserviceaccount.com",
                "mcp_service_account": "mcp-test@campus-test-42.iam.gserviceaccount.com"}, "secrets": {}})
        self.policy = ResourcePolicy(self.config, environment="local")
        self.documents = Documents()
        self.store = FirestoreSnapshotStore(self.documents, self.policy)
        self.payload = data(ROW)
        self.failure = None
        self.calls = []
        clients = SimpleNamespace(policy=self.policy, sheet_read=self.sheet_read)
        self.clients = clients
        self.reader = AssignmentsReader(clients, self.store, clock=lambda: NOW,
                                        request_context=self.current.get)

    def sheet_read(self, context, *, spreadsheet_id, a1_range, request_factory):
        self.policy.require_sheet(context, spreadsheet_id, a1_range)
        self.calls.append((spreadsheet_id, a1_range))
        options = {}

        def get(**kwargs):
            options.update(kwargs)

        service = SimpleNamespace(spreadsheets=lambda: SimpleNamespace(
            values=lambda: SimpleNamespace(get=get)))
        request_factory(service, spreadsheet_id, a1_range)
        assert options == {"spreadsheetId": "sheet-1", "range": "'Assignments'!A2:F8",
                           "majorDimension": "ROWS", "valueRenderOption": "FORMULA",
                           "dateTimeRenderOption": "SERIAL_NUMBER"}
        if self.failure:
            raise self.failure
        return deepcopy(self.payload)

    @contextmanager
    def request(self, boundary="mcp", run="run-1"):
        cfg = self.config
        caller = AuthenticatedCaller(
            principal_id=cfg.identities.agent_service_account if boundary == "mcp" else cfg.student.allowed_signin_principal,
            session_id="session-1", run_id=run, authenticated_at=NOW, expires_at=NOW + timedelta(hours=1))
        binding = ServerRunBinding(owner_id=cfg.student.trusted_user_id,
            signin_principal_id=cfg.student.allowed_signin_principal, session_id="session-1",
            run_id=run, config_revision=cfg.config_revision, expires_at=NOW + timedelta(hours=1))
        factory = RequestContextFactory(self.policy, boundary=boundary,
            authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
            bindings=SimpleNamespace(resolve=lambda **k: binding), clock=lambda: self.now)
        with factory.request(object()) as context:
            token = self.current.set(context)
            try:
                yield context
            finally:
                self.current.reset(token)


def test_normalization_dates_unicode_order_and_explicit_work():
    other = ["a-1", "DB", "Report", "2026-10-20T18:00:00+05:00", "0", "done"]
    assignments, issues = normalize(data(ROW, [], other))
    assert not issues
    assert [a.assignment_id for a in assignments] == ["a-1", "ml-1"]
    assert assignments[0].deadline.at == datetime(2026, 10, 20, 13, tzinfo=UTC)
    assert assignments[1].deadline.at == datetime(2026, 10, 20, 19, tzinfo=UTC)
    assert assignments[1].deadline.precision == "date"
    assert assignments[1].remaining_minutes == 120


@pytest.mark.parametrize("field,value,code", [
    (3, "", "needs_input_deadline"), (4, "", "needs_input_remaining_minutes"),
    (3, "20/10/2026", "deadline_invalid"), (3, "2026-02-30", "deadline_invalid"),
    (3, "2026-10-20T18:00:00", "deadline_invalid"), (3, 46315, "deadline_invalid"),
    (4, -1, "remaining_minutes_invalid"), (4, True, "remaining_minutes_invalid"),
    (4, 1.5, "remaining_minutes_invalid"), (4, "2h", "remaining_minutes_invalid"),
    (4, "1000001", "remaining_minutes_invalid"),
    (5, "done", "done_requires_explicit_zero"), (5, "completed", "assignment_fields_invalid"),
    (0, "bad id", "assignment_fields_invalid"), (1, "", "assignment_fields_invalid"),
    (2, "x" * 513, "assignment_fields_invalid"),
    (2, "=IMPORTXML(\"x\")", "sheet_formula_forbidden"),
])
def test_invalid_and_unknown_fields(field, value, code):
    row = deepcopy(ROW)
    row[field] = value
    assignments, issues = normalize(data(row))
    assert code in {issue.code for issue in issues}
    if code == "needs_input_remaining_minutes":
        assert assignments[0].remaining_minutes is None
        assert issues[0].item_ref == "row-3"


def test_duplicate_ids_remove_all_copies_even_if_other_copy_invalid():
    other = deepcopy(ROW)
    other[5] = "invalid"
    assignments, issues = normalize(data(ROW, other))
    assert assignments == ()
    assert "duplicate_assignment_id" in {issue.code for issue in issues}


@pytest.mark.parametrize("payload,code", [
    ({}, "sheet_response_invalid"),
    ({"majorDimension": "COLUMNS", "values": []}, "sheet_response_invalid"),
    ({"majorDimension": "ROWS"}, "sheet_headers_invalid"),
    ({"majorDimension": "ROWS", "values": [list(reversed(HEADERS))]}, "sheet_headers_invalid"),
    (data(ROW + ["extra"]), "sheet_row_shape_invalid"),
    (data(ROW, [], [], [], [], ROW), "sheet_range_truncated"),
    (data(*([[]] * 7)), "sheet_response_out_of_bounds"),
])
def test_incomplete_read(payload, code):
    _, issues = normalize(payload)
    assert code in {issue.code for issue in issues}


def test_blank_rows_do_not_end_read_and_empty_dataset_is_complete():
    assignments, issues = normalize(data([], ROW, [], [], [], []))
    assert len(assignments) == 1 and not issues
    assert assignments[0].source_row == "row-4"
    assert normalize(data()) == ((), ())


def test_authoritative_snapshot_roundtrip_backend_reload_and_binding():
    async def scenario():
        h = Harness()
        with h.request() as context:
            snapshot = await h.reader.read_assignments(context, REQUEST)
            assert h.documents.data
            assert snapshot.source.complete
            assert AssignmentReadResult(snapshot=snapshot).status == "complete"
            assert snapshot.source.reference.read_at == NOW
            assert snapshot.source.source_version is None
            assert snapshot.source.reference.snapshot_id.startswith("as_")

        with h.request("backend") as backend:
            loaded = await h.store.get_assignments(backend, snapshot.source.reference)
            rows = normalized_assignments(loaded)
            assert (rows[0].course, rows[0].title, rows[0].status) == (ROW[1], ROW[2], ROW[5])
            assert rows[0].project() == loaded.assignments[0]
        with h.request("backend", run="run-2") as backend:
            with pytest.raises(AccessDenied):
                await h.store.get_assignments(backend, snapshot.source.reference)
        with pytest.raises(AccessDenied):
            await h.reader.read_assignments(context, REQUEST)
        assert len(h.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [(1, "New course"), (2, "New title"),
    (3, "2026-10-21"), (4, 60), (5, "in_progress")])
def test_relevant_changes_are_bound_to_saved_content_hash(field, value):
    async def scenario():
        h = Harness()
        with h.request() as ctx:
            original = await h.reader.read_assignments(ctx, REQUEST)
            repeated = await h.reader.read_assignments(ctx, REQUEST)
            h.payload["values"][1][field] = value
            changed = await h.reader.read_assignments(ctx, REQUEST)
            assert original.source.reference.content_hash == repeated.source.reference.content_hash
            assert original.source.reference.snapshot_id != repeated.source.reference.snapshot_id
            assert original.source.reference.content_hash != changed.source.reference.content_hash
            assert await h.store.get_assignments(ctx, original.source.reference) == original
            assert await h.store.get_assignments(ctx, changed.source.reference) == changed
    asyncio.run(scenario())


def test_unknown_and_api_error_persist_incomplete_and_storage_failure_never_returns():
    async def scenario():
        h = Harness()
        with h.request() as ctx:
            h.payload["values"][1][4] = ""
            result = await h.reader.read_assignments(ctx, REQUEST)
            assert not result.source.complete
            assert AssignmentReadResult(snapshot=result).status == "needs_input"
            assert result.assignments[0].remaining_minutes is None
            assert (await h.store.get_assignments(ctx, result.source.reference)) == result
            h.failure = WorkspaceAccessError("workspace_forbidden")
            result = await h.reader.read_assignments(ctx, REQUEST)
            assert not result.source.complete and not result.assignments
            assert AssignmentReadResult(snapshot=result).status == "error"
            assert result.source.issues[0].code == "workspace_forbidden"
            assert await h.store.get_assignments(ctx, result.source.reference) == result
            h.failure = None
            h.documents.fail = True
            with pytest.raises(RuntimeError, match="fake_storage_unavailable"):
                await h.reader.read_assignments(ctx, REQUEST)
    asyncio.run(scenario())


def test_raw_dto_backend_and_expired_authority_rejected_before_io():
    async def scenario():
        h = Harness()
        with h.request() as ctx:
            with pytest.raises(AccessDenied):
                await h.reader.read_assignments(ctx.trusted, REQUEST)
        with h.request("backend") as ctx:
            with pytest.raises(AccessDenied):
                await h.reader.read_assignments(ctx, REQUEST)
        assert not h.calls and not h.documents.data
    asyncio.run(scenario())


def test_wire_schema_unchanged_and_projection_roundtrip():
    async def scenario():
        h = Harness()
        with h.request() as ctx:
            snapshot = await h.reader.read_assignments(ctx, REQUEST)
        assert AssignmentsSnapshot.model_validate_json(canonical_json(snapshot)) == snapshot
        assert set(snapshot.model_dump()) == {"schema_version", "source", "assignments"}
    asyncio.run(scenario())


@pytest.mark.parametrize("area", ["'Assignments'!A1:E8", "Assignments!A1:G8",
    "Assignments!A1:F501", "Assignments!A1:F2"])
def test_bounded_configuration_rejected_before_network(area):
    with pytest.raises(ValueError, match="assignment_range_requires"):
        _bounds(area)


def test_noncanonical_projection_rejected_even_with_matching_dto_hash():
    from campuspulse_recovery.domain.models import Assignment, assignments_content_hash

    async def scenario():
        h = Harness()
        with h.request() as ctx:
            saved = await h.reader.read_assignments(ctx, REQUEST)
        payload = saved.model_dump(mode="json")
        payload["assignments"][0]["title"] = ' {"format":"campuspulse-sheet-v1","course":"ML","title":"Lab","status":"todo"}'
        assignments = tuple(Assignment.model_validate(a) for a in payload["assignments"])
        payload["source"]["reference"]["content_hash"] = assignments_content_hash(assignments)
        with pytest.raises(ValueError, match="projection_not_canonical"):
            normalized_assignments(AssignmentsSnapshot.model_validate(payload))
    asyncio.run(scenario())


def test_published_port_end_to_end_persists_before_return_and_retains_projection():
    async def scenario():
        h = Harness()
        reader: AssignmentsReaderPort = h.reader
        with h.request() as ctx:
            snapshot = await reader.read(ctx.as_port_context(), REQUEST.horizon)
            assert h.documents.data and len(h.calls) == 1
            assert await h.store.get_assignments(ctx, snapshot.source.reference) == snapshot
            assert snapshot.source.complete
            assert normalized_assignments(snapshot)[0].course == ROW[1]
            assert normalized_assignments(snapshot)[0].status == ROW[5]
            direct = await reader.read(ctx, REQUEST.horizon)
            assert direct.source.reference.content_hash == snapshot.source.reference.content_hash
            h.documents.fail = True
            with pytest.raises(RuntimeError, match="fake_storage_unavailable"):
                await reader.read(ctx.as_port_context(), REQUEST.horizon)
    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["parsed", "copied_dto", "raw_dict", "copied_capability",
    "no_resolver", "no_active_scope", "expired", "closed", "backend", "foreign_policy",
    "wrong_current_scope"])
def test_port_authority_rejected_before_any_api_or_storage(variant):
    async def scenario():
        h = Harness()
        with h.request("backend" if variant == "backend" else "mcp") as ctx:
            candidate = ctx.as_port_context()
            reader = h.reader
            if variant == "parsed":
                candidate = TrustedContext.model_validate_json(canonical_json(candidate))
            elif variant == "copied_dto":
                candidate = candidate.model_copy()
            elif variant == "raw_dict":
                candidate = candidate.model_dump()
            elif variant == "copied_capability":

                with pytest.raises(TypeError, match="server request scope"):
                    copy(ctx)
                assert not h.calls and not h.documents.data
                return
            elif variant == "no_resolver":
                reader = AssignmentsReader(h.clients, h.store, clock=lambda: NOW)
            elif variant == "no_active_scope":
                h.current.set(None)
            elif variant == "expired":
                h.now = NOW + timedelta(hours=2)
            elif variant == "foreign_policy":
                other = Harness()
                with other.request() as foreign:
                    h.current.set(foreign)
                    with pytest.raises(AccessDenied):
                        await reader.read(candidate, REQUEST.horizon)
                assert not h.calls and not h.documents.data
                return
            elif variant == "wrong_current_scope":
                with h.request() as current:
                    assert candidate == current.as_port_context()
                    with pytest.raises(AccessDenied):
                        await reader.read(candidate, REQUEST.horizon)
                assert not h.calls and not h.documents.data
                return
            if variant != "closed":
                with pytest.raises(AccessDenied):
                    await reader.read(candidate, REQUEST.horizon)
        if variant == "closed":

            h.current.set(ctx)
            with pytest.raises(AccessDenied):
                await reader.read(candidate, REQUEST.horizon)
        assert not h.calls and not h.documents.data
    asyncio.run(scenario())


@pytest.mark.parametrize("invalidity", ["formula", "extra_cell", "bad_status"])
def test_duplicate_exclusion_precedes_other_row_errors_and_is_persisted(invalidity):
    async def scenario():
        h = Harness()
        duplicate = deepcopy(ROW)
        code = {"formula": "sheet_formula_forbidden", "extra_cell": "sheet_row_shape_invalid",
                "bad_status": "assignment_fields_invalid"}[invalidity]
        if invalidity == "formula":
            duplicate[2] = "=1+1"
        elif invalidity == "extra_cell":
            duplicate.append("extra")
        else:
            duplicate[5] = "invalid"
        h.payload = data(ROW, duplicate)
        with h.request() as ctx:
            result = await h.reader.read(ctx.as_port_context(), REQUEST.horizon)
            assert result.assignments == () and not result.source.complete
            assert {code, "duplicate_assignment_id"} <= {i.code for i in result.source.issues}
            assert await h.store.get_assignments(ctx, result.source.reference) == result
    asyncio.run(scenario())
