"""Private Streamable HTTP MCP. IAM/run adapters are mandatory startup inputs.

T12/T13 supply RequestAuthenticator (credential is a Starlette Request) and
RunBindingResolver from verified transport/server policy. No header is trusted
by this module. Cloud Run IAM must also restrict ingress to the agent identity.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

import mcp
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..security.context import AccessDenied, RequestContextFactory
from .read_tools import dispatch, tool_definitions
from . import write_tools


def _install_sdk_log_redaction():
    """Redact before handlers, including SDK calls to the root logger.

    Logger-name filters miss logging.warning() in mcp.shared.session. Attribute
    records to the installed SDK source directory instead. Chain the existing
    record factory and preserve unrelated application records and log levels.
    This service-wide guard also covers SDK loggers imported after startup.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_campuspulse_sdk_redaction", False):
        return
    sdk_root = Path(mcp.__file__).resolve().parent

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if Path(record.pathname).resolve().is_relative_to(sdk_root):
            record.msg, record.args = "mcp_transport_event", ()
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return record

    factory._campuspulse_sdk_redaction = True
    logging.setLogRecordFactory(factory)


class ReadMCP(FastMCP):
    async def list_tools(self):
        return tool_definitions() + (write_tools.tool_definitions() if self.complete else [])

    async def call_tool(self, name, arguments):
        request = self.get_context().request_context.request
        if self.complete and name in write_tools.WRITE_CONTRACTS:
            return await write_tools.dispatch(name, arguments, request=request, contexts=self.contexts,
                                             apply=self.apply, verification=self.verification)
        return await dispatch(name, arguments, request=request, contexts=self.contexts,
                              assignments=self.assignments, calendar=self.calendar)


def create_app(*, contexts: RequestContextFactory, assignments, calendar,
               allowed_hosts: list[str], allowed_origins: list[str], port: int = 8080,
               close=None):
    """Explicit local read-stage compatibility/smoke composition (two tools)."""
    return _create_transport(contexts=contexts, assignments=assignments, calendar=calendar,
        allowed_hosts=allowed_hosts, allowed_origins=allowed_origins, port=port, close=close)


class _MCPApplication:
    def __init__(self, app, contexts, complete):
        self.app, self.contexts, self.complete = app, contexts, complete

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            try:
                with self.contexts.request(Request(scope)):
                    pass
            except AccessDenied:
                await JSONResponse({"error": "access_denied"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_complete_app(*, contexts, assignments, calendar, apply, verification,
                        allowed_hosts, allowed_origins, port=8080, close=None):
    """Mandatory full production composition, using actual accepted services.

    Missing dependencies/guards or split authority fail before serving traffic.
    This startup API is trusted code, never an MCP/browser input.
    """
    from ..application.apply import ApplyService
    from ..application.verify import VerificationService
    from ..application.preflight import PreflightService
    from ..integrations.assignments import AssignmentsReader
    from ..integrations.calendar_read import CalendarReader
    from ..integrations.calendar_write import CalendarWriter

    if (not isinstance(contexts, RequestContextFactory)
            or not isinstance(apply, ApplyService) or not isinstance(verification, VerificationService)
            or not isinstance(apply.preflight, PreflightService)
            or not isinstance(assignments, AssignmentsReader) or not isinstance(calendar, CalendarReader)
            or not isinstance(apply.writer, CalendarWriter)):
        raise ValueError("complete_mcp_domain_services_required")
    preflight = apply.preflight
    documents, policy = apply.plans.documents, contexts.policy
    if (apply.policy is not policy or verification.policy is not policy
            or preflight.assignments is not assignments or preflight.calendar is not calendar
            or verification.assignments is not assignments or verification.calendar is not calendar
            or verification.executions is not apply.executions
            or verification.plans is not apply.plans or verification.registry is not apply.registry
            or verification.snapshots is not preflight.snapshots
            or apply.verification_reader is not verification
            or getattr(apply.executions.verification_guard, "store", None) is not verification
            or getattr(apply.executions.approval_guard, "store", None) is not apply.approvals
            or apply.coordination.recovery_guard is None
            or not callable(getattr(apply.coordination.recovery_guard, "require_stopped", None))
            or apply.writer.clients.policy is not policy or apply.writer.clients.read_only
            or any(s.documents is not documents or s.policy is not policy for s in (
                verification, preflight.snapshots, apply.registry, apply.approvals,
                apply.executions, apply.coordination))):
        raise ValueError("complete_mcp_authority_guards_required")
    return _create_transport(contexts=contexts, assignments=assignments, calendar=calendar,
        apply=apply, verification=verification, allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins, port=port, close=close)


def _create_transport(*, contexts, assignments, calendar, allowed_hosts, allowed_origins,
                      port=8080, close=None, apply=None, verification=None):
    """Inject trusted server objects, not import paths or mappings from clients.

    Read ports receive the exact active non-wire capability. They independently
    enforce the same policy, resource bindings, persistence and MCP boundary.
    SDK session_manager owns the ASGI lifespan; optional close releases clients.
    """
    if contexts.boundary != "mcp" or not allowed_hosts or not 1 <= port <= 65535:
        raise ValueError("invalid_mcp_server_configuration")
    server = ReadMCP("campuspulse_recovery", host="0.0.0.0", port=port,
                     stateless_http=True, json_response=True, log_level="ERROR",
                     max_request_body_size=16384,
                     transport_security=TransportSecuritySettings(
                         enable_dns_rebinding_protection=True,
                         allowed_hosts=allowed_hosts, allowed_origins=allowed_origins))
    server.contexts, server.assignments, server.calendar = contexts, assignments, calendar
    server.complete = apply is not None and verification is not None
    server.apply, server.verification = apply, verification



    @server.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "alive"})

    app = server.streamable_http_app()
    _install_sdk_log_redaction()

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with server.session_manager.run():
                yield
        finally:
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    app.router.lifespan_context = lifespan

    return _MCPApplication(app, contexts, server.complete)


def create_google_app(*, policy, environment, authenticator, bindings,
                      recovery_guard, allowed_hosts, allowed_origins, port=8080):
    """Complete Google production factory; explicit trusted stop provider required."""
    from datetime import UTC, datetime
    from google.cloud.firestore import AsyncClient
    from ..integrations.assignments import AssignmentsReader
    from ..integrations.calendar_read import CalendarReader
    from ..integrations.google_clients import GoogleWorkspaceClients
    from ..storage.plans import FirestoreDocuments
    from ..storage.snapshots import FirestoreSnapshotStore
    from ..storage.owned_blocks import FirestoreOwnedBlockRegistry
    from ..storage.plans import FirestorePlanStore
    from ..storage.leases import FirestoreCalendarCoordination
    from ..storage.operations import FirestoreExecutionStore
    from ..web.approval import FirestoreApprovalStore
    from ..integrations.calendar_write import CalendarWriter
    from ..application.preflight import PreflightService
    from ..application.apply import ApplyService
    from ..application.verify import VerificationService

    policy.config.require_environment(environment)
    if recovery_guard is None or not callable(getattr(recovery_guard, "require_stopped", None)):
        raise ValueError("trusted_stopped_writer_provider_required")
    clock = lambda: datetime.now(UTC)
    contexts = RequestContextFactory(policy, boundary="mcp", authenticator=authenticator, bindings=bindings)
    client = AsyncClient(project=policy.config.project_id, database=policy.config.firestore_database)
    documents = FirestoreDocuments(client, project_id=policy.config.project_id,
                                   database_id=policy.config.firestore_database)
    snapshots = FirestoreSnapshotStore(documents, policy)
    registry = FirestoreOwnedBlockRegistry(documents, policy)
    clients = GoogleWorkspaceClients(policy, environment=environment, read_only=False)
    plans = FirestorePlanStore(documents, policy)
    approvals = FirestoreApprovalStore(documents, policy, clock=clock)
    coordination = FirestoreCalendarCoordination(documents, policy, clock=clock, recovery_guard=recovery_guard)
    executions = FirestoreExecutionStore(documents, policy, coordination=coordination,
                                         approval_guard=approvals.transaction_guard())
    assignments = AssignmentsReader(clients, snapshots, clock=clock)
    calendar = CalendarReader(clients, snapshots, registry, clock=clock)
    preflight = PreflightService(plans, snapshots, approvals, executions, coordination,
                                 registry, assignments, calendar, policy, clock=clock)
    verification = VerificationService(plans, snapshots, executions, registry,
                                       assignments, calendar, policy, clock=clock)
    executions.verification_guard = verification.transaction_guard()
    apply = ApplyService(preflight, CalendarWriter(clients, registry, coordination, plans, clock=clock),
                         clock=clock, verification_reader=verification)
    return create_complete_app(contexts=contexts, assignments=assignments, calendar=calendar,
                      apply=apply, verification=verification,
                      allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
                      port=port, close=client.close)


def main():
    """T12/T13 trusted composition factory; no development identity bypass.

    CAMPUSPULSE_MCP_APP_FACTORY=trusted_module:create_app selects server code,
    never a model argument. Factory receives injected PORT, must load explicit
    cloud AppConfig and provide real IAM authentication/run binding adapters.
    """
    try:
        port = int(os.environ.get("PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError()
        module, name = os.environ["CAMPUSPULSE_MCP_APP_FACTORY"].split(":")
        app = getattr(importlib.import_module(module), name)(port=port)
        if (os.environ.get("K_SERVICE") or os.environ.get("CAMPUSPULSE_MCP_ENVIRONMENT") == "cloud"):
            if not isinstance(app, _MCPApplication) or not app.complete:
                raise ValueError("complete_production_mcp_required")
            app.contexts.policy.config.require_environment("cloud")
    except Exception:
        raise SystemExit("mcp_startup_configuration_required") from None
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, log_level="error")


if __name__ == "__main__":
    main()
