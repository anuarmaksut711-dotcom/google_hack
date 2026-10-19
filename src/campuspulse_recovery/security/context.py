"""Server-only per-request authority, kept out of model/browser tool schemas.

Transport/auth consumers supply RequestAuthenticator and RunBindingResolver at
server startup. UI identity belongs to T16; service IAM to T12/T13; Workspace
credentials to T07; trace/session integration is refined in T23/T24. T10 wires
the transport boundary. T05 owns plan/snapshot/ownership storage, not verifiers.
They must verify actual transport credentials (issuer/audience/signature/time,
or platform-verified IAM ingress) and load server-owned session/run bindings.
Neither arbitrary claims nor model/request headers implement these protocols.

Across processes this object is NEVER serialized/forwarded as proof. The agent
authenticates the backend service identity; MCP authenticates the agent service
identity, then each rebuilds authority with its own configured single-user map
and server-owned binding. Backend->Agent Engine OAuth, agent->MCP ID token and
MCP->Workspace sharing/scoped credentials remain separate adapter concerns.

Early G1 read smoke does not depend on a finished UI. T13 must supply verified
single-user service transport plus a server-created bootstrap invocation binding
for the configured student. Its server-generated session/run IDs are correlation
only, not evidence of UI sign-in or consent. They never come from the model.
Both adapters remain mandatory; this module implements neither live path.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterator, Literal, Protocol, TYPE_CHECKING
from uuid import uuid4

from ..domain.models import Contract, Identifier, Instant
from ..domain.ports import TrustedContext
from ..domain.time_rules import instant

if TYPE_CHECKING:
    from .resources import ResourcePolicy

Boundary = Literal["backend", "agent", "mcp"]


class AccessDenied(PermissionError):
    """Safe policy code only. Do not attach credentials or raw auth exceptions."""


class AuthenticatedCaller(Contract):
    """Auth adapter output, not request JSON or an LLM-generated DTO.

    Session/run are bound by trusted middleware to the actual authenticated
    invocation. Service IAM alone is insufficient evidence for arbitrary
    X-User/X-Session/X-Run fields. Resolve them via the server-owned invocation
    binding/channel; a browser/model cannot create these records.
    """

    principal_id: Identifier
    session_id: Identifier
    run_id: Identifier
    authenticated_at: Instant
    expires_at: Instant


class ServerRunBinding(Contract):
    """Server-created invocation binding, loaded through a trusted resolver.

    T16 binds actual UI sessions; T13 may bootstrap a service-only read smoke
    without UI. signin_principal_id pins the configured student identity in both
    cases; its presence alone never proves a user signed in or approved a plan.
    T23/T24 refine trace/session integration. No storage implementation here.
    """

    owner_id: Identifier
    signin_principal_id: Identifier
    session_id: Identifier
    run_id: Identifier
    config_revision: Identifier
    expires_at: Instant


class RequestAuthenticator(Protocol):
    def authenticate(self, credential: object, *, boundary: Boundary, now: datetime) -> AuthenticatedCaller:
        """Verify transport and bind invocation; raise on absent/invalid identity.

        Install this implementation in server code, never from input data. Its
        output must come from verified auth + server-owned invocation binding,
        not decoded/unverified claims or a forwarded TrustedContext dictionary.
        """
        ...


class RunBindingResolver(Protocol):
    def resolve(self, *, session_id: str, run_id: str) -> ServerRunBinding | None:
        """Exact server-side lookup; missing/revoked bindings return None."""
        ...


@dataclass(slots=True)
class _Lease:
    active: bool = True


@dataclass(frozen=True, slots=True, init=False)
class TrustedRequestContext:
    """Non-wire request capability. Only a factory's active scope is accepted.

    Use as_port_context immediately before a port dispatch. A structural
    ports.TrustedContext alone does not prove authentication. Python code inside
    the trusted server is not sandboxed by this type; model data never receives
    the factory, verifier, resolver or this object.
    """

    trusted: TrustedContext
    session_id: str
    request_id: str
    boundary: Boundary
    issued_at: datetime
    expires_at: datetime
    _factory: RequestContextFactory = field(repr=False, compare=False)
    _lease: _Lease = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object) -> TrustedRequestContext:
        raise TypeError("TrustedRequestContext is created by server request scope only")

    def require_active(self) -> None:
        self._factory._require_active(self)

    def as_port_context(self) -> TrustedContext:
        self.require_active()
        return self.trusted


class RequestContextFactory:
    """No default verifier or development identity bypass, including locally."""

    def __init__(self, policy: ResourcePolicy, *, boundary: Boundary,
                 authenticator: RequestAuthenticator, bindings: RunBindingResolver,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if boundary not in ("backend", "agent", "mcp"):
            raise ValueError("unknown server boundary")
        self.policy = policy
        self.boundary = boundary
        self._authenticator = authenticator
        self._bindings = bindings
        self._clock = clock
        self._current: ContextVar[TrustedRequestContext | None] = ContextVar(f"request-{uuid4().hex}", default=None)

    @contextmanager
    def request(self, credential: object) -> Iterator[TrustedRequestContext]:
        if self._current.get() is not None:
            raise AccessDenied("nested_request_context")
        now = instant(self._clock())
        # Errors are normalized before reaching request/UI logs; no credential,
        # Google exception message, principal or event content is included.
        try:
            caller = AuthenticatedCaller.model_validate(self._authenticator.authenticate(credential, boundary=self.boundary, now=now))
            if not caller.authenticated_at <= now < caller.expires_at:
                raise AccessDenied("expired_identity")
            self.policy.require_principal(self.boundary, caller.principal_id)
            binding = self._bindings.resolve(session_id=caller.session_id, run_id=caller.run_id)
            if binding is None:
                raise AccessDenied("missing_run_binding")
            binding = ServerRunBinding.model_validate(binding)
            self.policy.require_binding(binding, session_id=caller.session_id, run_id=caller.run_id, now=now)
        except Exception:
            raise AccessDenied("authentication_or_binding_rejected") from None

        config = self.policy.config
        student = config.student
        trusted = TrustedContext(owner_id=student.trusted_user_id, principal_id=caller.principal_id,
            project_id=config.project_id, region=config.mcp_cloud_run_region,
            sheet_id=student.spreadsheet_id, sheet_range=student.assignments_a1_range,
            calendar_id=student.calendar_id, timezone=student.timezone,
            config_revision=config.config_revision, run_id=binding.run_id)
        # All data-service ports target the MCP Cloud Run region. Independent
        # agent/model/web locations remain separately explicit in AppConfig.
        context = object.__new__(TrustedRequestContext)
        values = dict(trusted=trusted, session_id=binding.session_id, request_id="req_" + uuid4().hex,
            boundary=self.boundary, issued_at=now,
            expires_at=min(caller.expires_at, binding.expires_at, now + timedelta(minutes=5)),
            _factory=self, _lease=_Lease())
        for name, value in values.items():
            object.__setattr__(context, name, value)
        reset = self._current.set(context)
        try:
            yield context
        finally:
            # Shared lease also invalidates copied async ContextVars at exit.
            context._lease.active = False
            self._current.reset(reset)

    def _require_active(self, context: TrustedRequestContext) -> None:
        now = instant(self._clock())
        if (context._factory is not self or self._current.get() is not context
                or not context._lease.active or not context.issued_at <= now < context.expires_at):
            raise AccessDenied("inactive_request_context")
