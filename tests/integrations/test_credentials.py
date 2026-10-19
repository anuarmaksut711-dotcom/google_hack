"""Synthetic local evidence. No live resource, IAM, sharing or consent claim."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import logging
import socket
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# Core/build environments intentionally do not install MCP's Google clients.
# Execute this suite with the pinned MCP environment, as documented.
pytest.importorskip("google.auth", reason="T07 requires the MCP SDK environment")
pytest.importorskip("googleapiclient.discovery", reason="T07 requires the MCP SDK environment")
import requests
from google.auth import exceptions as auth_errors
from google.oauth2.credentials import Credentials as UserCredentials

from campuspulse_recovery.config import AppConfig, ConfigurationError, load_config
from campuspulse_recovery.integrations import google_clients, google_credentials
from campuspulse_recovery.integrations.google_clients import GoogleWorkspaceClients
from campuspulse_recovery.integrations.google_credentials import (
    CALENDAR_EVENTS_SCOPE, CALENDAR_READ_SCOPE, SHEETS_READ_SCOPE,
    WorkspaceAccessError, WorkspaceCredentials, _BoundedAuthRequest,
    create_workspace_credentials,
)
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from campuspulse_recovery.security.resources import ResourcePolicy

NOW = datetime(2026, 10, 17, 16, tzinfo=UTC)
SENSITIVE = "sensitive-token-event-cell-content"


@pytest.fixture
def no_network(monkeypatch):
    """Fail closed if a synthetic redaction regression reaches a real socket."""
    connect = Mock(side_effect=AssertionError("network_forbidden"))
    create_connection = Mock(side_effect=AssertionError("network_forbidden"))
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    yield
    connect.assert_not_called()
    create_connection.assert_not_called()


def assert_safe_error_logging(error, caplog):
    """Structured collectors must not reach raw requests, responses or bodies."""
    assert error.__cause__ is None and error.__context__ is None
    logger = logging.getLogger("campuspulse.test.client_logging_probe")
    logger.error("safe_workspace_failure", exc_info=(type(error), error, error.__traceback__))
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.args) + repr(record.__dict__)
        if record.exc_info:
            exception = record.exc_info[1]
            rendered += repr(exception.args) + repr(vars(exception))
            rendered += "".join(traceback.format_exception(*record.exc_info))
            assert exception.__cause__ is None and exception.__context__ is None
        assert SENSITIVE not in rendered


@pytest.fixture
def config():
    return AppConfig.model_validate({
        "mode": "local_development", "project_id": "campus-test-42",
        "agent_region": "europe-west4", "model_location": "global",
        "web_cloud_run_region": "europe-west2", "mcp_cloud_run_region": "europe-west1",
        "firestore_location": "eur3", "firestore_database": "(default)",
        "workspace_access_mode": "explicit_resource_sharing",
        "student": {"trusted_user_id": "student-test-1",
            "allowed_signin_principal": "https://accounts.google.com#synthetic-subject-1",
            "spreadsheet_id": "synthetic-sheet-1",
            "assignments_a1_range": "'Assignments 2026'!A2:F100",
            "calendar_id": "synthetic-calendar-1@group.calendar.google.com",
            "timezone": "Asia/Qyzylorda"},
        "identities": {
            "backend_service_account": "backend-test@campus-test-42.iam.gserviceaccount.com",
            "agent_service_account": "agent-test@campus-test-42.iam.gserviceaccount.com",
            "mcp_service_account": "mcp-test@campus-test-42.iam.gserviceaccount.com"},
        "secrets": {},
    })


@contextmanager
def request_scope(config, *, boundary="mcp", clock=lambda: NOW):
    """Explicit mock authentication/binding, not a production smoke runner."""
    policy = ResourcePolicy(config, environment="local")
    principal = (config.identities.agent_service_account if boundary == "mcp"
                 else config.student.allowed_signin_principal)
    auth = Mock()
    auth.authenticate.return_value = AuthenticatedCaller(principal_id=principal,
        session_id="test-session", run_id="test-run", authenticated_at=NOW,
        expires_at=NOW + timedelta(hours=1))
    store = Mock()
    store.resolve.return_value = ServerRunBinding(owner_id=config.student.trusted_user_id,
        signin_principal_id=config.student.allowed_signin_principal,
        session_id="test-session", run_id="test-run", config_revision=config.config_revision,
        expires_at=NOW + timedelta(hours=1))
    factory = RequestContextFactory(policy, boundary=boundary, authenticator=auth,
                                    bindings=store, clock=clock)
    with factory.request(object()) as context:
        yield policy, context


def cloud_config(config):
    return config.model_copy(update={"mode": "cloud"})


@pytest.mark.parametrize("read_only,scope", [(False, CALENDAR_EVENTS_SCOPE), (True, CALENDAR_READ_SCOPE)])
def test_cloud_uses_configured_identity_and_exact_scopes_without_adc(config, monkeypatch, read_only, scope):
    import google.auth

    adc = Mock(side_effect=AssertionError("cloud must not load ADC files"))
    monkeypatch.setattr(google.auth, "default", adc)
    credentials = create_workspace_credentials(cloud_config(config), environment="cloud", read_only=read_only)
    assert credentials.scopes == (SHEETS_READ_SCOPE, scope)
    assert credentials._raw.service_account_email == config.identities.mcp_service_account
    assert credentials._raw.scopes == (SHEETS_READ_SCOPE, scope)
    assert credentials.token is None
    adc.assert_not_called()


def test_local_adc_is_only_impersonation_source_and_its_project_is_ignored(config, monkeypatch):
    import google.auth

    source = UserCredentials("source-token")
    monkeypatch.setattr(google.auth, "default", Mock(return_value=(source, "wrong-default-project")))
    credentials = create_workspace_credentials(config, environment="local", read_only=True)
    assert credentials._raw.service_account_email == config.identities.mcp_service_account
    assert credentials._raw._target_scopes == (SHEETS_READ_SCOPE, CALENDAR_READ_SCOPE)
    assert credentials._raw.quota_project_id == config.project_id
    assert credentials._raw is not source


def test_local_service_account_key_or_implicit_metadata_source_is_rejected(config, monkeypatch):
    import google.auth

    monkeypatch.setattr(google.auth, "default", Mock(return_value=(object(), "ignored")))
    with pytest.raises(WorkspaceAccessError, match="^local_user_adc_required$"):
        create_workspace_credentials(config, environment="local")


def test_unavailable_adc_is_redacted(config, monkeypatch, caplog, no_network):
    import google.auth

    monkeypatch.setattr(google.auth, "default", Mock(side_effect=RuntimeError(SENSITIVE)))
    with pytest.raises(WorkspaceAccessError, match="^workspace_credentials_unavailable$") as result:
        create_workspace_credentials(config, environment="local")
    assert result.value.__suppress_context__
    assert SENSITIVE not in str(result.value)
    assert_safe_error_logging(result.value, caplog)


def test_cloud_cannot_use_local_configuration(config):
    with pytest.raises(ConfigurationError):
        create_workspace_credentials(config, environment="cloud")


def fake_raw(config):
    raw = SimpleNamespace(service_account_email=config.identities.mcp_service_account,
        token=SENSITIVE, expiry=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1), valid=True)
    raw.refresh = Mock()
    return raw


def test_refresh_and_automatic_expiry_refresh_keep_token_in_memory(config):
    raw = fake_raw(config)
    credentials = WorkspaceCredentials(raw, expected_service_account=raw.service_account_email,
                                        scopes=(SHEETS_READ_SCOPE, CALENDAR_READ_SCOPE))
    headers = {}
    credentials.before_request(None, "GET", "https://sheets.googleapis.com", headers)
    assert headers["authorization"] == "Bearer " + SENSITIVE
    assert raw.refresh.call_count == 1
    credentials.before_request(None, "GET", "https://sheets.googleapis.com", {})
    assert raw.refresh.call_count == 1
    credentials.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    credentials.before_request(None, "GET", "https://sheets.googleapis.com", {})
    assert raw.refresh.call_count == 2
    assert isinstance(raw.refresh.call_args.args[0], _BoundedAuthRequest)


@pytest.mark.parametrize("error", [auth_errors.RefreshError(SENSITIVE),
    auth_errors.TransportError(SENSITIVE), requests.Timeout(SENSITIVE), RuntimeError(SENSITIVE)])
def test_refresh_errors_are_sanitized_and_old_token_invalidated(config, error, caplog):
    raw = fake_raw(config)
    credentials = WorkspaceCredentials(raw, expected_service_account=raw.service_account_email, scopes=())
    credentials.token = "old"
    raw.refresh.side_effect = error
    with pytest.raises(WorkspaceAccessError, match="^workspace_credential_refresh_failed$") as result:
        credentials.refresh(None)
    assert credentials.token is None and credentials.expiry is None
    assert result.value.__suppress_context__
    assert SENSITIVE not in str(result.value) + caplog.text


def test_identity_mismatch_blocks_token_release(config):
    raw = fake_raw(config)
    credentials = WorkspaceCredentials(raw, expected_service_account="other@wrong.invalid", scopes=())
    with pytest.raises(WorkspaceAccessError, match="^workspace_identity_mismatch$"):
        credentials.refresh(None)
    assert credentials.token is None


@pytest.mark.parametrize("valid,expiry", [(False, NOW), (True, None)])
def test_invalid_or_non_expiring_token_rejected(config, valid, expiry):
    raw = fake_raw(config)
    raw.valid, raw.expiry = valid, expiry
    credentials = WorkspaceCredentials(raw, expected_service_account=raw.service_account_email, scopes=())
    with pytest.raises(WorkspaceAccessError, match="^workspace_token_invalid$"):
        credentials.refresh(None)


def test_auth_request_has_timeout_attempt_and_elapsed_budgets():
    now = [0.0]
    transport = Mock()
    request = _BoundedAuthRequest(transport, clock=lambda: now[0])
    request(url="https://oauth2.googleapis.com", timeout=120)
    assert transport.call_args.kwargs["timeout"] == 5
    now[0] = 14
    request(url="https://oauth2.googleapis.com")
    assert transport.call_args.kwargs["timeout"] == 1
    now[0] = 15
    with pytest.raises(auth_errors.TransportError, match="budget_exhausted"):
        request(url="https://oauth2.googleapis.com")
    another = _BoundedAuthRequest(transport, clock=lambda: 0)
    for _ in range(6):
        another(url="https://oauth2.googleapis.com")
    with pytest.raises(auth_errors.TransportError, match="budget_exhausted"):
        another(url="https://oauth2.googleapis.com")


@pytest.mark.parametrize("environment", ["cloud", "local"])
def test_real_pinned_credentials_refresh_with_mock_token_endpoint(config, monkeypatch, environment):
    """Exercise actual google-auth refresh logic; the HTTP response is a double."""
    import google.auth

    source = UserCredentials("synthetic-source-token")
    monkeypatch.setattr(google.auth, "default", Mock(return_value=(source, "ignored-project")))
    calls = []

    def transport(self, method, url, *, data, headers, timeout, **kwargs):
        calls.append({"url": url, "method": method, "timeout": timeout})
        assert kwargs["allow_redirects"] is False
        if ":generateAccessToken" in url:
            payload = {"accessToken": SENSITIVE, "expireTime": "2099-01-01T00:00:00Z"}
            assert json.loads(data)["scope"] == [SHEETS_READ_SCOPE, CALENDAR_READ_SCOPE]
        elif "/token" in url:
            payload = {"access_token": SENSITIVE, "expires_in": 3600, "token_type": "Bearer"}
            assert "scopes=" in url
        else:
            payload = {"email": config.identities.mcp_service_account, "scopes": []}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.headers["content-type"] = "application/json"
        return response

    monkeypatch.setattr(requests.Session, "request", transport)
    actual = cloud_config(config) if environment == "cloud" else config
    credentials = create_workspace_credentials(actual, environment=environment, read_only=True)
    credentials.refresh(None)
    assert credentials.token == SENSITIVE
    assert calls and all(0 < c["timeout"] <= 5 for c in calls)


@pytest.mark.parametrize("scenario", ["success", "iam_error", "refresh_error", "transport_error", "malformed_token"])
def test_actual_auth_transport_never_logs_tokens_even_at_debug(config, monkeypatch, caplog, scenario):
    """Actual SDK source refresh + impersonation, actual auth transport, no I/O.

    Scan every record's message/args/structured extras and exception data, not
    only rendered log text. An unrelated DEBUG marker proves logging stayed on.
    """
    import google.auth
    from google.auth import _helpers
    from urllib.parse import parse_qs

    source_token = "SYNTHETIC_DEBUG_SOURCE_BEARER"
    target_token = "SYNTHETIC_DEBUG_TARGET_BEARER"
    refresh_token = "SYNTHETIC_DEBUG_REFRESH_TOKEN"
    client_secret = "SYNTHETIC_DEBUG_CLIENT_SECRET"
    sensitive = (source_token, target_token, refresh_token, client_secret)
    source = UserCredentials(None, refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token", client_id="synthetic-client",
        client_secret=client_secret)
    monkeypatch.setattr(google.auth, "default", Mock(return_value=(source, "ignored")))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("network_forbidden")))
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network_forbidden")))
    # A transport regression must not even hand raw data to SDK logging hooks.
    request_log = Mock(side_effect=AssertionError("sdk_auth_request_logging_forbidden"))
    response_log = Mock(side_effect=AssertionError("sdk_auth_response_logging_forbidden"))
    monkeypatch.setattr(_helpers, "request_log", request_log)
    monkeypatch.setattr(_helpers, "response_log", response_log)
    seen = []

    def send(self, method, url, *, data, headers, timeout, **kwargs):
        seen.append(url)
        assert method == "POST" and kwargs["allow_redirects"] is False and 0 < timeout <= 5
        status = 200
        if url == "https://oauth2.googleapis.com/token":
            assert parse_qs(data.decode())["refresh_token"] == [refresh_token]
            if scenario == "refresh_error":
                status, payload = 400, {"error": "invalid_grant", "error_description": " ".join(sensitive)}
            else:
                payload = {"access_token": source_token, "expires_in": 3600, "token_type": "Bearer",
                           "refresh_token": refresh_token}
        else:
            assert ":generateAccessToken" in url
            assert headers["authorization"] == "Bearer " + source_token
            assert json.loads(data)["scope"] == [SHEETS_READ_SCOPE, CALENDAR_READ_SCOPE]
            if scenario == "transport_error":
                raise requests.ConnectionError(" ".join(sensitive))
            if scenario == "iam_error":
                status, payload = 403, {"error": {"message": " ".join(sensitive)}}
            else:
                payload = {"accessToken": target_token,
                    "expireTime": "invalid" if scenario == "malformed_token" else "2099-01-01T00:00:00Z"}
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", send)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="google")
    caplog.set_level(logging.DEBUG, logger="google.auth.transport.requests")
    probe = logging.getLogger("campuspulse.test.auth_logging_probe")
    levels = {name: logging.getLogger(name).level for name in ("", "google", "google.auth.transport.requests")}
    credentials = create_workspace_credentials(config, environment="local", read_only=True)
    if scenario == "success":
        credentials.refresh(None)
        assert credentials.token == target_token
    else:
        with pytest.raises(WorkspaceAccessError) as caught:
            credentials.refresh(None)
        assert credentials.token is None
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        # Simulate normal exception logging by a caller, including exc_info.
        probe.error("safe_auth_failure", exc_info=(type(caught.value), caught.value, caught.value.__traceback__))
    probe.debug("auth_logging_still_enabled")
    assert any(r.getMessage() == "auth_logging_still_enabled" for r in caplog.records)
    assert levels == {name: logging.getLogger(name).level for name in levels}
    assert len(seen) == (1 if scenario == "refresh_error" else 2)
    request_log.assert_not_called()
    response_log.assert_not_called()
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.args) + repr(record.__dict__)
        if record.exc_info:
            exception = record.exc_info[1]
            rendered += repr(exception.args) + repr(vars(exception))
            rendered += "".join(traceback.format_exception(*record.exc_info))
            assert exception.__cause__ is None and exception.__context__ is None
        assert not any(secret in rendered for secret in sensitive)
        assert not hasattr(record, "httpRequest") and not hasattr(record, "httpResponse")
    socket.create_connection.assert_not_called()
    socket.socket.connect.assert_not_called()


@pytest.fixture
def wire(monkeypatch):
    """Actual pinned discovery/request construction; mock only token and HTTP."""
    responses = []
    session = Mock()

    def send(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        status, payload = result
        return SimpleNamespace(status_code=status, headers={"content-type": "application/json"},
                               content=json.dumps(payload).encode())

    session.request.side_effect = send
    monkeypatch.setattr(requests, "Session", Mock(return_value=session))
    provider = Mock(return_value=UserCredentials("synthetic-access-token"))
    monkeypatch.setattr(google_clients, "create_workspace_credentials", provider)
    return SimpleNamespace(responses=responses, session=session, provider=provider)


def sheet_call(clients, context, config, *, area=None, sheet=None):
    return clients.sheet_read(context, spreadsheet_id=sheet or config.student.spreadsheet_id,
        a1_range=area or config.student.assignments_a1_range,
        request_factory=lambda service, resource, area: service.spreadsheets().values().get(
            spreadsheetId=resource, range=area))


def calendar_call(clients, context, config, *, mutation=False, method="list"):
    def factory(service, resource):
        if method == "list":
            return service.events().list(calendarId=resource)
        if method == "patch":
            request = service.events().patch(calendarId=resource, eventId="cp1234",
                body={"start": {"dateTime": NOW.isoformat()}})
            request.headers["If-Match"] = '"synthetic-etag"'
            return request
        return service.events().insert(calendarId=resource,
            body={"id": "cp1234", "summary": SENSITIVE})
    return clients.calendar_request(context, calendar_id=config.student.calendar_id,
        request_factory=factory, mutation=mutation)


@pytest.mark.parametrize("kind", ["sheet", "calendar", "backend", "expired", "dict", "range"])
def test_policy_rejects_before_client_or_request_build(config, wire, monkeypatch, kind):
    import googleapiclient.discovery

    build = Mock(side_effect=AssertionError("must not build"))
    monkeypatch.setattr(googleapiclient.discovery, "build", build)
    clock = [NOW]
    with request_scope(config, boundary="backend" if kind == "backend" else "mcp",
                       clock=lambda: clock[0]) as (policy, context):
        clients = GoogleWorkspaceClients(policy, environment="local")
        if kind == "expired":
            clock[0] += timedelta(minutes=6)
        with pytest.raises(AccessDenied):
            if kind == "calendar":
                clients.calendar_request(context, calendar_id="primary", request_factory=Mock())
            else:
                sheet_call(clients, {} if kind == "dict" else context, config,
                    sheet="other-sheet" if kind == "sheet" else None,
                    area="'Assignments 2026'!A1:F999" if kind == "range" else None)
    build.assert_not_called()
    wire.provider.assert_not_called()
    wire.session.request.assert_not_called()


def test_actual_discovery_uses_configured_resource_range_and_transport_timeouts(config, wire):
    wire.responses.append((200, {"values": [["example"]]}))
    with request_scope(config) as (policy, context):
        result = sheet_call(GoogleWorkspaceClients(policy, environment="local"), context, config)
    assert result == {"values": [["example"]]}
    args, kwargs = wire.session.request.call_args
    assert args[0] == "GET" and config.student.spreadsheet_id in args[1]
    assert kwargs["timeout"] == (3.0, 10.0)
    assert kwargs["allow_redirects"] is False
    wire.session.close.assert_called_once()


@pytest.mark.parametrize("path", ["other_sheet", "other_range", "acl", "delete", "host", "write_as_read"])
def test_request_builder_cannot_escape_target_or_methods(config, wire, path):
    with request_scope(config) as (policy, context):
        clients = GoogleWorkspaceClients(policy, environment="local")
        with pytest.raises(AccessDenied):
            if path.startswith("other_"):
                clients.sheet_read(context, spreadsheet_id=config.student.spreadsheet_id,
                    a1_range=config.student.assignments_a1_range,
                    request_factory=lambda service, resource, area: service.spreadsheets().values().get(
                        spreadsheetId="other" if path == "other_sheet" else resource,
                        range="'Other'!A1:F20" if path == "other_range" else area))
            elif path == "write_as_read":
                calendar_call(clients, context, config, method="insert")
            else:
                def factory(service, resource):
                    if path == "acl":
                        return service.acl().list(calendarId=resource)
                    if path == "delete":
                        return service.events().delete(calendarId=resource, eventId="cp1234")
                    request = service.events().list(calendarId=resource)
                    request.uri = "https://attacker.invalid/calendar"
                    return request
                clients.calendar_request(context, calendar_id=config.student.calendar_id,
                    request_factory=factory, mutation=path == "delete")
    wire.session.request.assert_not_called()


def test_read_only_client_rejects_writes_before_credentials(config, wire):
    with request_scope(config) as (policy, context):
        with pytest.raises(AccessDenied, match="workspace_read_only_client"):
            calendar_call(GoogleWorkspaceClients(policy, environment="local", read_only=True),
                          context, config, mutation=True, method="insert")
    wire.provider.assert_not_called()


@pytest.mark.parametrize("status,attempts", [(401, 1), (403, 1), (404, 1), (429, 2), (503, 2)])
def test_reads_retry_only_bounded_transient_http_errors(config, wire, status, attempts, caplog):
    wire.responses.extend([(status, {"error": {"message": SENSITIVE}})] * attempts)
    with request_scope(config) as (policy, context):
        with pytest.raises(WorkspaceAccessError) as result:
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config)
    assert wire.session.request.call_count == attempts
    assert result.value.http_status == status
    assert not result.value.outcome_unknown
    assert SENSITIVE not in str(result.value) + caplog.text


@pytest.mark.parametrize("method", ["insert", "patch"])
@pytest.mark.parametrize("outcome,unknown", [(401, False), (412, False), (429, False), (503, True), ("timeout", True)])
def test_writes_are_never_replayed_and_ambiguous_outcomes_are_unknown(config, wire, method, outcome, unknown):
    wire.responses.append(requests.Timeout(SENSITIVE) if outcome == "timeout"
                          else (outcome, {"error": {"message": SENSITIVE}}))
    with request_scope(config) as (policy, context):
        with pytest.raises(WorkspaceAccessError) as result:
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config,
                          mutation=True, method=method)
    assert wire.session.request.call_count == 1
    assert result.value.outcome_unknown is unknown
    assert result.value.__suppress_context__
    assert SENSITIVE not in str(result.value)
    if method == "patch":
        assert wire.session.request.call_args.kwargs["headers"]["If-Match"] == '"synthetic-etag"'


@pytest.mark.parametrize("mutation", [False, True])
def test_transport_error_drops_prepared_request_with_authorization_and_body(
        config, wire, caplog, no_network, mutation):
    prepared = requests.Request("POST", "https://www.googleapis.com/synthetic",
        headers={"Authorization": "Bearer " + SENSITIVE}, data=SENSITIVE).prepare()
    raw = requests.Timeout("synthetic timeout", request=prepared)
    wire.responses.append(raw)
    with request_scope(config) as (policy, context):
        with pytest.raises(WorkspaceAccessError) as caught:
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config,
                mutation=mutation, method="insert" if mutation else "list")
    assert caught.value.code == "workspace_transport_failed"
    assert caught.value.outcome_unknown is mutation
    assert_safe_error_logging(caught.value, caplog)
    assert wire.session.request.call_count == 1
    wire.session.close.assert_called_once()


@pytest.mark.parametrize("mutation,status,code,unknown", [
    (False, 401, "workspace_unauthorized", False),
    (False, 403, "workspace_forbidden", False),
    (True, 412, "workspace_stale_version", False),
    (True, 503, "workspace_http_error", True),
])
def test_http_error_drops_sdk_response_headers_and_sanitized_body(
        config, wire, caplog, no_network, mutation, status, code, unknown):
    wire.session.request.side_effect = None
    wire.session.request.return_value = SimpleNamespace(status_code=status,
        headers={"content-type": "application/json", "x-synthetic-private": SENSITIVE},
        content=json.dumps({"error": {"message": SENSITIVE}}).encode())
    with request_scope(config) as (policy, context):
        with pytest.raises(WorkspaceAccessError) as caught:
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config,
                mutation=mutation, method="insert" if mutation else "list")
    assert caught.value.code == code and caught.value.http_status == status
    assert caught.value.outcome_unknown is unknown
    assert_safe_error_logging(caught.value, caplog)
    assert wire.session.request.call_count == 1
    wire.session.close.assert_called_once()


@pytest.mark.parametrize("mutation", [False, True])
@pytest.mark.parametrize("content", [
    b"\xff" + SENSITIVE.encode(), SENSITIVE.encode(),
    json.dumps([SENSITIVE]).encode(), b"null",
], ids=["invalid_utf8", "invalid_json", "json_list", "json_null"])
def test_successful_malformed_or_nonobject_payload_is_safe_client_failure(
        config, wire, caplog, no_network, mutation, content):
    """Use actual SDK parsing: invalid JSON returns str in the pinned SDK."""
    wire.session.request.side_effect = None
    wire.session.request.return_value = SimpleNamespace(status_code=200,
        headers={"content-type": "application/json"}, content=content)
    with request_scope(config) as (policy, context):
        with pytest.raises(WorkspaceAccessError) as caught:
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config,
                mutation=mutation, method="insert" if mutation else "list")
    assert caught.value.code == "workspace_client_failed"
    assert caught.value.outcome_unknown is mutation
    assert_safe_error_logging(caught.value, caplog)
    assert wire.session.request.call_count == 1
    wire.session.close.assert_called_once()


@pytest.mark.parametrize("mutation", [False, True])
def test_valid_empty_json_object_is_accepted(config, wire, no_network, mutation):
    wire.responses.append((200, {}))
    with request_scope(config) as (policy, context):
        assert calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config,
            mutation=mutation, method="insert" if mutation else "list") == {}
    assert wire.session.request.call_count == 1
    wire.session.close.assert_called_once()


@pytest.mark.parametrize("mutation", [False, True])
def test_client_construction_failure_is_safely_normalized(
        config, wire, monkeypatch, caplog, no_network, mutation):
    import googleapiclient.discovery

    monkeypatch.setattr(googleapiclient.discovery, "build", Mock(side_effect=ValueError(SENSITIVE)))
    with request_scope(config) as (policy, context):
        with pytest.raises(WorkspaceAccessError) as caught:
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config,
                mutation=mutation, method="insert" if mutation else "list")
    assert caught.value.code == "workspace_client_failed"
    assert caught.value.outcome_unknown is mutation
    assert_safe_error_logging(caught.value, caplog)
    wire.session.request.assert_not_called()
    wire.session.close.assert_called_once()


def test_request_scope_expiring_during_refresh_prevents_api_call(config, wire):
    clock = [NOW]
    credentials = Mock()
    credentials.before_request.side_effect = lambda *args: clock.__setitem__(0, NOW + timedelta(minutes=6))
    wire.provider.return_value = credentials
    with request_scope(config, clock=lambda: clock[0]) as (policy, context):
        with pytest.raises(AccessDenied, match="inactive_request_context"):
            calendar_call(GoogleWorkspaceClients(policy, environment="local"), context, config)
    wire.session.request.assert_not_called()


@pytest.mark.parametrize("role,zone,expected,code", [
    ("writer", "Asia/Qyzylorda", "passed", "metadata_read_writer"),
    ("reader", "Asia/Qyzylorda", "blocked", "calendar_writer_required"),
    ("writer", "UTC", "passed", "metadata_read_writer"),
    ("owner", "America/New_York", "passed", "metadata_read_writer"),
    ("writer", "Europe/London", "passed", "metadata_read_writer"),
    ("freeBusyReader", "UTC", "blocked", "calendar_writer_required"),
    ("writer", "Asia/Unknown", "blocked", "calendar_timezone_invalid"),
    ("writer", " UTC", "blocked", "calendar_timezone_invalid"),
    ("writer", "", "blocked", "calendar_timezone_invalid"),
    ("writer", None, "blocked", "calendar_timezone_invalid"),
    ("writer", 17, "blocked", "calendar_timezone_invalid"),
    ("reader", "Asia/Unknown", "blocked", "calendar_timezone_invalid"),
])
def test_diagnostic_reads_only_metadata_and_outputs_redacted_result(config, wire, role, zone, expected, code):
    metadata = {"accessRole": role, "ignored": SENSITIVE}
    if zone is not None:
        metadata["timeZone"] = zone
    wire.responses.extend([(200, {"properties": {"timeZone": "Asia/Qyzylorda"}}),
                           (200, metadata)])
    with request_scope(config) as (policy, context):
        result = GoogleWorkspaceClients(policy, environment="local", read_only=True).diagnose_access(context, now=NOW)
    assert result["status"] == expected
    assert result["checks"]["calendar"]["code"] == code
    assert result["evidence"] == "live_metadata_reads"  # Transport is explicitly mocked in this test.
    rendered = json.dumps(result)
    assert SENSITIVE not in rendered and config.student.calendar_id not in rendered
    assert config.student.spreadsheet_id not in rendered and config.identities.mcp_service_account not in rendered
    calls = wire.session.request.call_args_list
    assert len(calls) == 2 and all(c.args[0] == "GET" for c in calls)
    assert "properties%28timeZone%29" in calls[0].args[1]
    assert "includeGridData=false" in calls[0].args[1]
    assert "fields=timeZone%2CaccessRole" in calls[1].args[1]
    assert "timeMin=" in calls[1].args[1] and "timeMax=" in calls[1].args[1]
    assert "items" not in calls[1].args[1]
    assert all(call.kwargs["read_only"] for call in wire.provider.call_args_list)


def test_diagnostic_reports_sharing_error_without_printing_response(config, wire):
    wire.responses.extend([(404, {"error": SENSITIVE}),
                           (200, {"timeZone": "Asia/Qyzylorda", "accessRole": "writer"})])
    with request_scope(config) as (policy, context):
        result = GoogleWorkspaceClients(policy, environment="local", read_only=True).diagnose_access(context, now=NOW)
    assert result["status"] == "blocked"
    assert result["checks"]["sheets"]["code"] == "workspace_not_found_or_unshared"
    assert SENSITIVE not in json.dumps(result)


def test_diagnostic_rejects_write_scoped_client_before_network(config, wire):
    with request_scope(config) as (policy, context):
        with pytest.raises(AccessDenied, match="diagnostic_read_only_client_required"):
            GoogleWorkspaceClients(policy, environment="local").diagnose_access(context, now=NOW)
    wire.provider.assert_not_called()


def test_null_example_blocks_before_any_auth_or_api(wire):
    from pathlib import Path

    sample = Path(__file__).resolve().parents[2] / "config.example.json"
    with pytest.raises(ConfigurationError):
        load_config(sample, environment="local")
    wire.provider.assert_not_called()
    wire.session.request.assert_not_called()
