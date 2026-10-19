"""Keyless Workspace identity; never user OAuth, DWD, or ADC project inference.

Cloud Run uses its attached MCP service account. Explicit local development
uses ADC only as the source for impersonating that SAME configured account.
No key/token file is created and no raw auth exception crosses this boundary.
Create credentials at runtime, never in a serialized Agent Engine artifact.
"""

from __future__ import annotations

import time
from typing import Callable

from google.auth import credentials as auth_credentials
from google.auth import exceptions as auth_errors
from google.auth import transport as auth_transport

from ..config import AppConfig, Environment

SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class WorkspaceAccessError(RuntimeError):
    """Allowlisted code only: safe to put in UI/trace without repr(raw_error)."""

    def __init__(self, code: str, *, http_status: int | None = None,
                 outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.outcome_unknown = outcome_unknown


class _AuthResponse(auth_transport.Response):
    """The public google-auth response interface, with no logging hooks."""

    def __init__(self, response) -> None:
        self._status = response.status_code
        self._headers = response.headers
        self._data = response.content

    @property
    def status(self):
        return self._status

    @property
    def headers(self):
        return self._headers

    @property
    def data(self):
        return self._data


class _AuthRequest(auth_transport.Request):
    """Auth-only requests adapter; never logs headers, bodies or token replies.

    google.auth.transport.requests.Request in pinned 2.62.0 emits unredacted
    Authorization in structured DEBUG records. Implement the public transport
    interface directly instead, without changing any process logging settings.
    The supplied plain requests session has zero adapter retries. This path
    does not use AuthorizedSession, SDK request_log/response_log, or redirects.
    """

    def __init__(self, session) -> None:
        self._session = session

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        failed = False
        try:
            kwargs["allow_redirects"] = False
            response = self._session.request(method, url, data=body, headers=headers,
                                             timeout=timeout, **kwargs)
            return _AuthResponse(response)
        except Exception:
            failed = True
        if failed:
            # Metadata SDK logs TransportError objects during its retries. Do
            # not give it a raw requests exception or even a chained context.
            raise auth_errors.TransportError("workspace_auth_transport_failed")


class _BoundedAuthRequest:
    """Bound each auth HTTP call and refuse further calls after the budget.

    SDK-internal metadata/token backoff may run between calls. This budget
    limits I/O and attempt count, not preemptive cancellation of SDK sleeps.
    """

    def __init__(self, request: Callable, *, clock: Callable = time.monotonic) -> None:
        self._request = request
        self._clock = clock
        self._until = clock() + 15.0
        self._remaining_calls = 6

    def __call__(self, *args, timeout=None, **kwargs):
        remaining = self._until - self._clock()
        if remaining <= 0 or self._remaining_calls <= 0:
            raise auth_errors.TransportError("credential_refresh_budget_exhausted")
        self._remaining_calls -= 1
        return self._request(*args, timeout=min(timeout or 5.0, 5.0, remaining), **kwargs)


class WorkspaceCredentials(auth_credentials.Credentials):
    """Refresh wrapper verifies the target identity before releasing its token.

    Request-scoped, synchronous use. Tokens exist in memory only. The wrapper
    intentionally ignores an HTTP client's unbounded auth request transport.
    """

    def __init__(self, raw, *, expected_service_account: str, scopes: tuple[str, ...]) -> None:
        super().__init__()
        self._raw = raw
        self._expected = expected_service_account
        self.scopes = scopes

    def refresh(self, request) -> None:
        import requests

        # Invalidate the wrapper first, so failure cannot leave a usable token.
        self.token = None
        self.expiry = None
        failure = None
        try:
            with requests.Session() as session:
                session.trust_env = False
                self._raw.refresh(_BoundedAuthRequest(_AuthRequest(session)))
            if self._raw.service_account_email != self._expected:
                failure = "workspace_identity_mismatch"
            elif not self._raw.valid or self._raw.expiry is None:
                failure = "workspace_token_invalid"
            else:
                self.token, self.expiry = self._raw.token, self._raw.expiry
            # Scopes are fixed at construction. In google-auth 2.62 the local
            # impersonated credential's public `scopes` property is None even
            # when target_scopes were supplied; do not infer scopes from it.
        except Exception:
            failure = "workspace_credential_refresh_failed"
        if failure:
            # Raise outside except so structured exception logging cannot walk
            # an attached raw token response through __context__/__cause__.
            raise WorkspaceAccessError(failure) from None


def create_workspace_credentials(config: AppConfig, *, environment: Environment,
                                 read_only: bool = False) -> WorkspaceCredentials:
    """Use only explicit configuration; never use ADC's inferred project.

    The local impersonation source must be user ADC (managed by gcloud outside
    the repo). It is NOT used directly against Calendar/Sheets. Cloud execution
    never reads ADC files or the optional OAuth Secret Manager references.
    """
    config = AppConfig.model_validate(config)
    config.require_environment(environment)
    scopes = (SHEETS_READ_SCOPE, CALENDAR_READ_SCOPE if read_only else CALENDAR_EVENTS_SCOPE)
    target = config.identities.mcp_service_account
    try:
        if environment == "cloud":
            from google.auth.compute_engine import Credentials

            raw = Credentials(service_account_email=target, scopes=scopes,
                              universe_domain="googleapis.com")
        else:
            import google.auth
            from google.auth.impersonated_credentials import Credentials
            from google.oauth2.credentials import Credentials as UserCredentials

            source, _ignored_project = google.auth.default()
            if not isinstance(source, UserCredentials):
                raise WorkspaceAccessError("local_user_adc_required")
            raw = Credentials(source_credentials=source, target_principal=target,
                              target_scopes=scopes, lifetime=600,
                              quota_project_id=config.project_id)
        return WorkspaceCredentials(raw, expected_service_account=target, scopes=scopes)
    except WorkspaceAccessError:
        raise
    except Exception:
        pass
    # `from None` inside except still retains the raw error in __context__.
    # Raise after leaving the handler so structured logs cannot traverse it.
    raise WorkspaceAccessError("workspace_credentials_unavailable") from None
