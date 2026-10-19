"""Local negative auth/resource checks; no Google auth, secrets or API calls.

The authenticator and binding store below are explicit test doubles, never
production authentication or evidence of real user identity/cloud IAM.
"""

from contextvars import copy_context
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from campuspulse_recovery.config import AppConfig, CLOUD_MARKERS, ConfigurationError, load_config
from campuspulse_recovery.domain.models import (
    TOOL_CONTRACTS, Interval, PlanPayload, PlanVersion, PlanningConstraints,
    SourceKind, SourceReference,
)
from campuspulse_recovery.domain.ports import TrustedContext
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
    TrustedRequestContext,
)
from campuspulse_recovery.security.resources import ResourcePolicy

NOW = datetime(2026, 10, 17, 16, tzinfo=UTC)


def config_data():
    return {
        "mode": "local_development",
        "project_id": "campus-test-42",
        "agent_region": "europe-west4",
        "model_location": "global",
        "web_cloud_run_region": "europe-west2",
        "mcp_cloud_run_region": "europe-west1",
        "firestore_location": "eur3",
        "firestore_database": "(default)",
        "workspace_access_mode": "explicit_resource_sharing",
        "student": {
            "trusted_user_id": "student-test-1",
            "allowed_signin_principal": "https://accounts.google.com#synthetic-subject-1",
            "spreadsheet_id": "synthetic-sheet-1",
            "assignments_a1_range": "'Assignments 2026'!A2:F100",
            "calendar_id": "synthetic-calendar-1@group.calendar.google.com",
            "timezone": "Asia/Qyzylorda",
        },
        "identities": {
            "backend_service_account": "backend-test@campus-test-42.iam.gserviceaccount.com",
            "agent_service_account": "agent-test@campus-test-42.iam.gserviceaccount.com",
            "mcp_service_account": "mcp-test@campus-test-42.iam.gserviceaccount.com",
        },
        "secrets": {},
    }


def put(data, dotted, value):
    parts = dotted.split(".")
    target = data
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


class FakeAuthenticator:
    def __init__(self, caller, credential):
        self.caller = caller
        self.credential = credential
        self.calls = 0

    def authenticate(self, credential, *, boundary, now):
        self.calls += 1
        if credential is not self.credential:
            raise ValueError("test credential rejected")
        return self.caller


class FakeBindings:
    def __init__(self, binding):
        self.binding = binding
        self.calls = 0

    def resolve(self, *, session_id, run_id):
        self.calls += 1
        return self.binding


def system(boundary="backend", *, config=None):
    config = AppConfig.model_validate(config_data()) if config is None else config
    policy = ResourcePolicy(config, environment="local")
    principal = {"backend": config.student.allowed_signin_principal,
                 "agent": config.identities.backend_service_account,
                 "mcp": config.identities.agent_service_account}[boundary]
    caller = AuthenticatedCaller(principal_id=principal, session_id="session-1", run_id="run-1",
        authenticated_at=NOW, expires_at=NOW + timedelta(hours=1))
    binding = ServerRunBinding(owner_id=config.student.trusted_user_id,
        signin_principal_id=config.student.allowed_signin_principal, session_id="session-1", run_id="run-1",
        config_revision=config.config_revision, expires_at=NOW + timedelta(hours=1))
    credential = object()
    auth = FakeAuthenticator(caller, credential)
    store = FakeBindings(binding)
    clock = [NOW]
    factory = RequestContextFactory(policy, boundary=boundary, authenticator=auth, bindings=store, clock=lambda: clock[0])
    return SimpleNamespace(config=config, policy=policy, auth=auth, bindings=store,
                           clock=clock, factory=factory, credential=credential)


def source(config, *, kind="assignments", owner=None, resource=None, revision=None, run="run-1"):
    return SourceReference(snapshot_id="snapshot-" + kind, kind=kind,
        owner_id=owner or config.student.trusted_user_id,
        resource_id=resource or (config.student.spreadsheet_id if kind == "assignments" else config.student.calendar_id),
        run_id=run, content_hash="0" * 64, read_at=NOW,
        config_revision=revision or config.config_revision)


def plan(config, *, owner=None, calendar=None, sheet=None, revision=None, run="run-1", timezone=None):
    owner = owner or config.student.trusted_user_id
    calendar = calendar or config.student.calendar_id
    revision = revision or config.config_revision
    return PlanVersion.seal(PlanPayload(owner_id=owner, calendar_id=calendar,
        plan_id="plan-test-1", version=1, created_at=NOW,
        source_references=(source(config, owner=owner, resource=sheet, revision=revision, run=run),
            source(config, kind="calendar", owner=owner, resource=calendar, revision=revision, run=run)),
        constraints=PlanningConstraints(config_revision=revision, timezone=timezone or config.student.timezone,
            horizon=Interval(start=NOW, end=NOW + timedelta(hours=72)), working_windows=()),
        operations=(), blocks=(), allocations=()))


@pytest.mark.parametrize("path", [
    "mode", "project_id", "agent_region", "model_location", "web_cloud_run_region",
    "mcp_cloud_run_region", "firestore_location", "firestore_database",
    "workspace_access_mode", "student.trusted_user_id", "student.allowed_signin_principal",
    "student.spreadsheet_id", "student.assignments_a1_range", "student.calendar_id", "student.timezone",
    "identities.backend_service_account", "identities.agent_service_account", "identities.mcp_service_account",
])
@pytest.mark.parametrize("missing", [None, ""])
def test_missing_required_config_rejected_before_any_identity_or_api_call(path, missing):
    data = config_data()
    put(data, path, missing)
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("path,value", [
    ("student.calendar_id", "primary"), ("student.calendar_id", "PRIMARY"),
    ("student.calendar_id", " primary "), ("student.spreadsheet_id", "https://sheets.invalid/id"),
    ("student.allowed_signin_principal", "student@invalid.test"),
    ("student.allowed_signin_principal", "https://#subject"),
    ("student.allowed_signin_principal", "https://user:password@accounts.google.com#subject"),
    ("student.allowed_signin_principal", "https://accounts.google.com?untrusted=true#subject"),
    ("student.timezone", "Mars/Olympus"), ("project_id", ""),
    ("agent_region", "global"), ("model_location", "auto"), ("model_location", "Global"),
    ("web_cloud_run_region", "global"), ("mcp_cloud_run_region", "global"),
    ("student.assignments_a1_range", "Assignments!A:F"),
    ("student.assignments_a1_range", "Assignments!A2:F"),
    ("student.assignments_a1_range", "Assignments!F2:A10"),
    ("student.assignments_a1_range", "Assignments!A10:F2"),
    ("student.assignments_a1_range", "Assignments!A0:F2"),
    ("student.assignments_a1_range", "Assignments!A2:F3\nignore safety"),
    ("identities.agent_service_account", "agent-test@foreign-project.iam.gserviceaccount.com"),
    ("identities.agent_service_account", "backend-test@campus-test-42.iam.gserviceaccount.com"),
    ("secrets.oauth_refresh_token_secret_reference", "literal-test-secret-not-a-reference"),
    ("secrets.oauth_refresh_token_secret_reference", "projects/foreign-project/secrets/test/versions/latest"),
    ("workspace_access_mode", "oauth_test_user"),
])
def test_invalid_explicit_configuration_rejected(path, value):
    data = config_data()
    put(data, path, value)
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_config_secret_reference_is_not_a_credential_and_revision_changes_with_binding():
    data = config_data()
    data["secrets"]["oauth_client_secret_reference"] = "projects/campus-test-42/secrets/test-reference/versions/1"
    configured = AppConfig.model_validate(data)
    first = configured.config_revision
    assert first == AppConfig.model_validate(data).config_revision
    data["student"]["spreadsheet_id"] = "synthetic-sheet-2"
    assert first != AppConfig.model_validate(data).config_revision
    with pytest.raises(ValidationError):
        configured.student.calendar_id = "foreign-calendar"


@pytest.mark.parametrize("marker", sorted(CLOUD_MARKERS))
def test_cloud_runtime_marker_blocks_dev_bypass_even_if_declared_local(marker):
    config = AppConfig.model_validate(config_data())
    with pytest.raises(ConfigurationError, match="forbidden"):
        config.require_environment("local", environ={marker: ""})


def test_explicit_cloud_environment_always_blocks_local_config(monkeypatch):
    for marker in CLOUD_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    config = AppConfig.model_validate(config_data())
    with pytest.raises(ConfigurationError, match="forbidden"):
        ResourcePolicy(config, environment="cloud")
    with pytest.raises(ConfigurationError):
        config.require_environment("automatic", environ={})
    data = config_data()
    data["mode"] = "cloud"
    cloud = AppConfig.model_validate(data)
    cloud.require_environment("cloud", environ={"K_SERVICE": "test"})
    assert ResourcePolicy(cloud, environment="cloud").config.mode == "cloud"


def test_legal_cloud_identifiers_and_quoted_tab_may_contain_example():
    data = config_data()
    data["mode"] = "cloud"
    data["project_id"] = "campus-example-42"
    data["student"]["assignments_a1_range"] = "'Example Semester'!A1:F50"
    for key, account in data["identities"].items():
        data["identities"][key] = account.replace("campus-test-42", "campus-example-42")
    config = AppConfig.model_validate(data)
    config.require_environment("cloud", environ={})
    assert config.student.assignments_a1_range == "'Example Semester'!A1:F50"
    assert config.project_id == "campus-example-42"


@pytest.mark.parametrize("location", ["global", "us-central1", "europe-west4"])
def test_independent_explicit_locations_and_mcp_port_region(location):
    data = config_data()
    data["model_location"] = location
    app = system(config=AppConfig.model_validate(data))
    with app.factory.request(app.credential) as context:
        assert app.policy.require_context(context).region == data["mcp_cloud_run_region"]
        assert app.config.agent_region == "europe-west4"
        assert app.config.model_location == location
        assert app.config.web_cloud_run_region == "europe-west2"
        assert app.config.mcp_cloud_run_region == "europe-west1"


@pytest.mark.parametrize("field", ["agent_region", "model_location", "web_cloud_run_region", "mcp_cloud_run_region"])
def test_location_has_no_default_and_cannot_inherit_another_location(field):
    data = config_data()
    del data[field]
    with pytest.raises(ValidationError) as error:
        AppConfig.model_validate(data)
    assert (field,) in {item["loc"] for item in error.value.errors()}


def test_legacy_shared_cloud_region_is_not_a_silent_fallback():
    data = config_data()
    data["cloud_run_region"] = data.pop("mcp_cloud_run_region")
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_cloud_components_may_explicitly_share_the_same_region():
    data = config_data()
    data["web_cloud_run_region"] = data["mcp_cloud_run_region"]
    config = AppConfig.model_validate(data)
    assert config.web_cloud_run_region == config.mcp_cloud_run_region


def test_safe_example_has_no_ids_or_credentials_and_cannot_start(tmp_path):
    path = Path(__file__).resolve().parents[2] / "config.example.json"
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert sample["project_id"] is None
    for location in ("agent_region", "model_location", "web_cloud_run_region", "mcp_cloud_run_region"):
        assert sample[location] is None
    assert all(value is None for key, value in sample["student"].items() if key != "timezone")
    assert all(value is None for value in sample["identities"].values())
    assert all(value is None for value in sample["secrets"].values())
    with pytest.raises(ConfigurationError, match="invalid configuration fields"):
        load_config(path, environment="local")
    sample["mode"] = "cloud"
    cloud_path = tmp_path / "null-cloud-sample.json"
    cloud_path.write_text(json.dumps(sample), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid configuration fields"):
        load_config(cloud_path, environment="cloud")


def test_loader_uses_only_explicit_file_and_sanitizes_validation_errors(tmp_path, monkeypatch):
    path = tmp_path / "nonsecret.json"
    data = config_data()
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "foreign-project")
    assert load_config(path, environment="local").project_id == "campus-test-42"
    data["secrets"]["oauth_refresh_token_secret_reference"] = "fake-value-must-not-appear"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        load_config(path, environment="local")
    assert "fake-value-must-not-appear" not in str(error.value)
    assert error.value.__suppress_context__
    del data["secrets"]["oauth_refresh_token_secret_reference"]
    data["fake-sensitive-key"] = "fake-sensitive-value"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        load_config(path, environment="local")
    assert "fake-sensitive" not in str(error.value)


@pytest.mark.parametrize("content", ['{"mode":"cloud","mode":"local_development"}', '{"nested":{"a":1,"a":2}}', '{broken', '[]'])
def test_malformed_duplicate_or_non_object_configuration_fails_closed(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path, environment="local")


@pytest.mark.parametrize("boundary", ["backend", "agent", "mcp"])
def test_single_user_mapping_at_each_authenticated_boundary(boundary):
    app = system(boundary)
    with app.factory.request(app.credential) as context:
        trusted = app.policy.require_context(context)
        assert trusted.owner_id == app.config.student.trusted_user_id
        assert trusted.principal_id == app.auth.caller.principal_id
        assert trusted.project_id == app.config.project_id
        assert trusted.region == app.config.mcp_cloud_run_region
        assert trusted.calendar_id == app.config.student.calendar_id
        assert trusted.sheet_id == app.config.student.spreadsheet_id
        app.policy.require_sheet(context, trusted.sheet_id, trusted.sheet_range)
        app.policy.require_calendar(context, trusted.calendar_id)
        app.policy.require_session(context, "session-1")
        app.policy.require_run(context, "run-1")
        assert context.as_port_context() == trusted
    assert app.auth.calls == app.bindings.calls == 1


def test_bootstrap_read_smoke_can_use_service_binding_without_ui_authentication():
    # T13's live adapters are not implemented here. These doubles demonstrate
    # that the mandatory interfaces do not require a finished T16 UI session.
    for boundary in ("agent", "mcp"):
        app = system(boundary)
        app.auth.caller = AuthenticatedCaller.model_validate(app.auth.caller.model_dump() | {
            "session_id": "server-bootstrap-session", "run_id": "server-bootstrap-run"})
        app.bindings.binding = ServerRunBinding.model_validate(app.bindings.binding.model_dump() | {
            "session_id": "server-bootstrap-session", "run_id": "server-bootstrap-run"})
        with app.factory.request(app.credential) as context:
            app.policy.require_run(context, "server-bootstrap-run")
            app.policy.require_calendar(context, app.config.student.calendar_id)
            assert context.trusted.owner_id == app.config.student.trusted_user_id
            assert context.trusted.principal_id != app.config.student.allowed_signin_principal
        assert app.auth.calls == app.bindings.calls == 1


@pytest.mark.parametrize("boundary", ["backend", "agent", "mcp"])
@pytest.mark.parametrize("credential", [None, {}, {"principal_id": "student-test-1", "approved": True},
    {"X-User": "student-test-1", "X-Session": "session-1", "X-Run": "run-1"}])
def test_missing_identity_or_model_headers_never_establish_identity(boundary, credential):
    app = system(boundary)
    api = Mock()
    with pytest.raises(AccessDenied):
        with app.factory.request(credential):
            api()
    api.assert_not_called()
    assert app.bindings.calls == 0


@pytest.mark.parametrize("boundary", ["backend", "agent", "mcp"])
@pytest.mark.parametrize("principal", ["foreign-subject", "", "mcp-test@campus-test-42.iam.gserviceaccount.com"])
def test_foreign_or_wrong_service_identity_rejected_before_binding_or_api(boundary, principal):
    app = system(boundary)
    # Use adapter output JSON here so missing/empty identity also tests its
    # checked reconstruction at the server boundary.
    app.auth.caller = app.auth.caller.model_dump() | {"principal_id": principal}
    api = Mock()
    with pytest.raises(AccessDenied):
        with app.factory.request(app.credential):
            api()
    api.assert_not_called()
    assert app.bindings.calls == 0


@pytest.mark.parametrize("field,value", [
    ("owner_id", "foreign-student"), ("signin_principal_id", "https://accounts.google.com#foreign"),
    ("session_id", "foreign-session"), ("run_id", "foreign-run"),
    ("config_revision", "old-config"), ("expires_at", NOW),
])
def test_spoofed_or_stale_server_binding_rejected_before_api(field, value):
    app = system()
    app.bindings.binding = ServerRunBinding.model_validate(app.bindings.binding.model_dump() | {field: value})
    api = Mock()
    with pytest.raises(AccessDenied):
        with app.factory.request(app.credential):
            api()
    api.assert_not_called()


@pytest.mark.parametrize("field,value", [("expires_at", NOW), ("authenticated_at", NOW + timedelta(seconds=1))])
def test_expired_or_future_authentication_rejected(field, value):
    app = system()
    app.auth.caller = app.auth.caller.model_dump() | {field: value}
    with pytest.raises(AccessDenied):
        with app.factory.request(app.credential):
            pytest.fail("invalid auth accepted")


@pytest.mark.parametrize("field,value", [("session_id", "foreign-session"), ("run_id", "foreign-run")])
def test_authenticated_invocation_cannot_select_another_session_or_run(field, value):
    app = system()
    app.auth.caller = app.auth.caller.model_dump() | {field: value}
    api = Mock()
    with pytest.raises(AccessDenied):
        with app.factory.request(app.credential):
            api()
    api.assert_not_called()


def test_missing_binding_and_auth_errors_are_redacted():
    app = system()
    app.bindings.binding = None
    with pytest.raises(AccessDenied):
        with app.factory.request(app.credential):
            pytest.fail("missing binding accepted")
    app.auth.authenticate = Mock(side_effect=ValueError("fake-secret-exception-content"))
    with pytest.raises(AccessDenied) as error:
        with app.factory.request(app.credential):
            pytest.fail("auth error accepted")
    assert "fake-secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("check,args", [
    ("require_owner", ("foreign-student",)),
    ("require_calendar", ("foreign-calendar",)), ("require_calendar", ("primary",)),
    ("require_calendar", (None,)),
    ("require_sheet", ("foreign-sheet", "'Assignments 2026'!A2:F100")),
    ("require_sheet", ("synthetic-sheet-1", "'Assignments 2026'!A1:Z1000")),
    ("require_session", ("foreign-session",)), ("require_run", ("foreign-run",)),
])
def test_spoofed_resources_and_session_rejected_before_api_dispatch(check, args):
    app = system()
    api = Mock()
    with app.factory.request(app.credential) as context:
        with pytest.raises(AccessDenied):
            getattr(app.policy, check)(context, *args)
            api()
    api.assert_not_called()


@pytest.mark.parametrize("overrides", [dict(owner="foreign-student"), dict(calendar="foreign-calendar"),
    dict(sheet="foreign-sheet"), dict(revision="old-config"), dict(timezone="UTC")])
def test_structurally_valid_plan_cannot_choose_owner_calendar_sheet_or_config(overrides):
    app = system()
    candidate = plan(app.config, **overrides)
    api = Mock()
    with app.factory.request(app.credential) as context:
        with pytest.raises(AccessDenied):
            app.policy.require_plan(context, candidate)
            api()
    api.assert_not_called()


def test_saved_plan_preserves_old_run_but_proposal_snapshot_must_use_current_run():
    app = system()
    saved = plan(app.config, run="earlier-run")
    with app.factory.request(app.credential) as context:
        app.policy.require_plan(context, saved)
        for reference in saved.payload.source_references:
            with pytest.raises(AccessDenied):
                app.policy.require_source(context, reference)
        app.policy.require_source(context, source(app.config))
        app.policy.require_source(context, source(app.config, kind=SourceKind.CALENDAR))


@pytest.mark.parametrize("overrides", [dict(owner="foreign-student"), dict(resource="foreign-sheet"),
    dict(revision="old-config"), dict(run="foreign-run")])
def test_foreign_snapshot_binding_cannot_reach_api(overrides):
    app = system()
    api = Mock()
    with app.factory.request(app.credential) as context:
        with pytest.raises(AccessDenied):
            app.policy.require_source(context, source(app.config, **overrides))
            api()
    api.assert_not_called()


def test_raw_dto_dictionary_and_direct_context_construction_are_not_authority():
    app = system()
    with app.factory.request(app.credential) as context:
        for raw in (context.trusted, context.trusted.model_dump()):
            with pytest.raises(AccessDenied, match="server_request_context_required"):
                app.policy.require_context(raw)
        assert isinstance(context.as_port_context(), TrustedContext)
    with pytest.raises(TypeError):
        TrustedRequestContext()
    other = system()
    with other.factory.request(other.credential) as context:
        with pytest.raises(AccessDenied, match="foreign_context_authority"):
            app.policy.require_context(context)


def test_context_expires_during_request_and_cannot_be_reused_after_exit_or_in_next_request():
    app = system()
    with app.factory.request(app.credential) as previous:
        captured = copy_context()
        app.clock[0] = NOW + timedelta(minutes=5)
        with pytest.raises(AccessDenied, match="inactive"):
            previous.as_port_context()
    app.clock[0] = NOW
    with pytest.raises(AccessDenied):
        app.policy.require_context(previous)
    # Copied async/thread context must not retain authority after parent exits.
    with pytest.raises(AccessDenied):
        captured.run(previous.as_port_context)
    with app.factory.request(app.credential) as current:
        assert current.request_id != previous.request_id
        with pytest.raises(AccessDenied):
            app.policy.require_context(previous)
        app.policy.require_context(current)


def test_exception_cleanup_and_missing_identity_cannot_inherit_previous_request():
    app = system()
    with pytest.raises(RuntimeError):
        with app.factory.request(app.credential) as previous:
            with pytest.raises(AccessDenied, match="nested"):
                with app.factory.request(app.credential):
                    pytest.fail("nested identity scope accepted")
            raise RuntimeError("test request failed")
    with pytest.raises(AccessDenied):
        previous.as_port_context()
    with pytest.raises(AccessDenied):
        with app.factory.request(None):
            pytest.fail("previous request identity reused")
    with app.factory.request(app.credential) as fresh:
        app.policy.require_context(fresh)


def test_independent_requests_do_not_share_contextvars():
    app = system()
    from contextvars import Context
    first_scope = app.factory.request(app.credential)
    second_scope = app.factory.request(app.credential)
    first_execution, second_execution = Context(), Context()
    first = first_execution.run(first_scope.__enter__)
    second = second_execution.run(second_scope.__enter__)
    try:
        first_execution.run(app.policy.require_context, first)
        second_execution.run(app.policy.require_context, second)
        with pytest.raises(AccessDenied):
            second_execution.run(app.policy.require_context, first)
    finally:
        first_execution.run(first_scope.__exit__, None, None, None)
        second_execution.run(second_scope.__exit__, None, None, None)


def test_auth_config_does_not_add_public_tools_or_identity_arguments():
    assert set(TOOL_CONTRACTS) == {"read_assignments", "read_calendar", "solve_repair", "apply_plan", "verify_plan"}
    for name, (input_type, _) in TOOL_CONTRACTS.items():
        fields = input_type.model_fields
        assert not {"principal_id", "owner_id", "calendar_id", "sheet_id", "session_id", "approved"}.intersection(fields)
