"""Internal Google client boundary, NOT extra MCP tools or approval authority.

Request builders are trusted Python adapters, never model-provided callables.
T08/T09 own normalization/pagination; the execution layer owns approval,
ownership, preflight, conditional updates, durable leases and reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import time
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import Environment
from ..domain.time_rules import iana_zone
from ..security.context import AccessDenied, TrustedRequestContext
from ..security.resources import ResourcePolicy
from .google_credentials import WorkspaceAccessError, create_workspace_credentials


class _RequestsHttp:
    """httplib2-compatible interface with zero hidden transport/auth retries.

    httplib2 can resend on dropped connections. requests' default HTTPAdapter
    has zero retries; use that transport with discovery's request generation.
    Disable redirects and sanitize error bodies before SDK logging/parsing.
    """

    def __init__(self, credentials, guard: Callable) -> None:
        import requests

        self._credentials = credentials
        self._guard = guard
        self._session = requests.Session()
        self._session.trust_env = False

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        import httplib2

        headers = dict(headers or {})
        self._credentials.before_request(None, method, uri, headers)
        self._guard()  # Refresh may outlive the request capability.
        response = self._session.request(method, uri, data=body, headers=headers,
                                        timeout=(3.0, 10.0), allow_redirects=False)
        status = int(response.status_code)
        content = response.content
        if status >= 300:
            # googleapiclient logs raw 403 reasons/content even with retries=0.
            content = b'{"error":{"message":"workspace_request_failed","errors":[{"reason":"forbidden"}]}}'
        return httplib2.Response({**response.headers, "status": str(status)}), content

    def close(self) -> None:
        self._session.close()


class GoogleWorkspaceClients:
    """Request-scoped client runner; no service/credential crosses public tools.

    Each call checks active MCP authority BEFORE building a client or request.
    Read operations have at most one retry (429/5xx); writes have none, including
    401. A timeout/5xx during a write is unknown and must be reconciled by the
    execution layer. Socket timeouts are 3s connect/10s read per HTTP attempt.
    """

    def __init__(self, policy: ResourcePolicy, *, environment: Environment,
                 read_only: bool = False) -> None:
        policy.config.require_environment(environment)
        self.policy = policy
        self.environment = environment
        self.read_only = read_only

    def _guard(self, context: TrustedRequestContext, kind: str, resource_id: str,
               a1_range: str | None = None) -> None:
        if kind == "sheets":
            self.policy.require_sheet(context, resource_id, a1_range)
        else:
            self.policy.require_calendar(context, resource_id)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")

    def sheet_read(self, context: TrustedRequestContext, *, spreadsheet_id: str,
                   a1_range: str, request_factory: Callable) -> dict:
        """Factory(service, configured_sheet_id, configured_range) -> GET request."""
        return self._run(context, "sheets", spreadsheet_id, a1_range, request_factory, mutation=False)

    def calendar_request(self, context: TrustedRequestContext, *, calendar_id: str,
                         request_factory: Callable, mutation: bool = False) -> dict:
        """Factory(service, configured_calendar_id) -> events GET/insert/patch.

        mutation=True is a transport setting, NEVER evidence of user approval.
        Execution callers must finish durable guards before calling this method.
        """
        return self._run(context, "calendar", calendar_id, None, request_factory, mutation=mutation)

    @staticmethod
    def _check_request(request, kind: str, resource_id: str, a1_range: str | None,
                       *, mutation: bool) -> None:
        parsed = urlsplit(request.uri)
        path = unquote(parsed.path)
        method = request.method.upper()
        if kind == "sheets":
            prefix = f"/v4/spreadsheets/{resource_id}"
            query = parse_qs(parsed.query)
            valid = (parsed.hostname == "sheets.googleapis.com" and method == "GET"
                     and (path == prefix + "/values/" + a1_range
                          or (path == prefix and query.get("ranges") == [a1_range])))
        else:
            prefix = f"/calendar/v3/calendars/{resource_id}/events"
            tail = path.removeprefix(prefix + "/")
            single_event = path.startswith(prefix + "/") and bool(tail) and "/" not in tail
            valid = (parsed.hostname == "www.googleapis.com" and
                     ((method == "GET" and (path == prefix or single_event))
                      or (mutation and method == "POST" and path == prefix)
                      or (mutation and method == "PATCH" and single_event)))
        if (not valid or parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise AccessDenied("workspace_request_not_allowed")
        if not mutation and method != "GET":
            raise AccessDenied("workspace_read_required")

    def _run(self, context, kind, resource_id, a1_range, request_factory, *, mutation):
        self._guard(context, kind, resource_id, a1_range)
        if mutation and self.read_only:
            raise AccessDenied("workspace_read_only_client")
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        import requests

        http = None
        failure = None
        try:
            credentials = create_workspace_credentials(self.policy.config,
                environment=self.environment, read_only=self.read_only)
            http = _RequestsHttp(credentials,
                lambda: self._guard(context, kind, resource_id, a1_range))
            service = build(kind, "v4" if kind == "sheets" else "v3", http=http,
                            static_discovery=True, cache_discovery=False)
            # Resource policy already ran; no untrusted API request exists yet.
            request = (request_factory(service, resource_id, a1_range) if kind == "sheets"
                       else request_factory(service, resource_id))
            self._check_request(request, kind, resource_id, a1_range, mutation=mutation)
            for attempt in range(1 if mutation else 2):
                self._guard(context, kind, resource_id, a1_range)
                try:
                    result = request.execute(num_retries=0)
                    # All supported endpoints return JSON objects. The pinned
                    # SDK returns raw str for invalid JSON instead of raising.
                    if not isinstance(result, dict):
                        failure = WorkspaceAccessError("workspace_client_failed", outcome_unknown=mutation)
                        break
                    return result
                except HttpError as error:
                    status = int(error.resp.status)
                    if not mutation and attempt == 0 and (status == 429 or 500 <= status <= 599):
                        time.sleep(0.25)
                        continue
                    code = {401: "workspace_unauthorized", 403: "workspace_forbidden",
                            404: "workspace_not_found_or_unshared", 409: "workspace_conflict",
                            412: "workspace_stale_version", 429: "workspace_rate_limited"}.get(
                                status, "workspace_http_error")
                    failure = WorkspaceAccessError(code, http_status=status,
                        outcome_unknown=mutation and (status == 408 or status >= 500))
                    break
        except (AccessDenied, WorkspaceAccessError):
            raise
        except (requests.RequestException, TimeoutError, ConnectionError, OSError):
            failure = WorkspaceAccessError("workspace_transport_failed", outcome_unknown=mutation)
        except Exception:
            failure = WorkspaceAccessError("workspace_client_failed", outcome_unknown=mutation)
        finally:
            if http is not None:
                try:
                    http.close()
                except Exception:
                    pass  # Closing a local transport must not leak raw errors.
        # Raise only after all handlers and transport cleanup have ended.
        # `from None` inside except would retain a raw HTTP/parse exception,
        # including PreparedRequest authorization/body, in __context__.
        if failure is not None:
            raise failure from None

    def diagnose_access(self, context: TrustedRequestContext, *, now: datetime) -> dict:
        """Real metadata reads; emit only codes, booleans and UTC timing.

        Requires actual server-authenticated context, no fabricated CLI identity.
        Calendar metadata comes from events.list's collection fields so neither
        calendar.readonly nor calendar.calendars.readonly is needed. No items,
        values, titles, emails, IDs or event contents are requested/returned.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("aware_diagnostic_time_required")
        if not self.read_only:
            raise AccessDenied("diagnostic_read_only_client_required")
        config = self.policy.config
        self._guard(context, "sheets", config.student.spreadsheet_id, config.student.assignments_a1_range)
        self._guard(context, "calendar", config.student.calendar_id)
        started = time.monotonic()
        checks = {}
        for kind in ("sheets", "calendar"):
            try:
                if kind == "sheets":
                    data = self.sheet_read(context, spreadsheet_id=config.student.spreadsheet_id,
                        a1_range=config.student.assignments_a1_range,
                        request_factory=lambda service, sheet, area: service.spreadsheets().get(
                            spreadsheetId=sheet, ranges=[area], includeGridData=False,
                            fields="properties(timeZone)"))
                    okay = bool(data.get("properties", {}).get("timeZone"))
                    code = "metadata_read" if okay else "metadata_incomplete"
                else:
                    lower = now.astimezone(UTC).replace(microsecond=0)
                    data = self.calendar_request(context, calendar_id=config.student.calendar_id,
                        request_factory=lambda service, calendar: service.events().list(
                            calendarId=calendar, timeMin=lower.isoformat(),
                            timeMax=(lower + timedelta(seconds=1)).isoformat(),
                            maxResults=1, fields="timeZone,accessRole"))
                    try:
                        iana_zone(data.get("timeZone"))
                        valid_zone = True
                    except ValueError:
                        valid_zone = False
                    writer = data.get("accessRole") in ("writer", "owner")
                    okay = valid_zone and writer
                    code = ("calendar_timezone_invalid" if not valid_zone else
                            "calendar_writer_required" if not writer else "metadata_read_writer")
                checks[kind] = {"ok": okay, "code": code}
            except WorkspaceAccessError as error:
                checks[kind] = {"ok": False, "code": error.code}
        return {"status": "passed" if all(c["ok"] for c in checks.values()) else "blocked",
                "evidence": "live_metadata_reads", "checks": checks,
                "checked_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
