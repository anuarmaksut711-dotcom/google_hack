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
        return tool_definitions()

    async def call_tool(self, name, arguments):
        request = self.get_context().request_context.request
        return await dispatch(name, arguments, request=request, contexts=self.contexts,
                              assignments=self.assignments, calendar=self.calendar)


def create_app(*, contexts: RequestContextFactory, assignments, calendar,
               allowed_hosts: list[str], allowed_origins: list[str], port: int = 8080,
               close=None):
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
    # Pinned FastMCP registers this override with validate_input=False itself;
    # validate exact DTOs in dispatch to keep invalid input out of error text.

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

    class AuthenticateMCP:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"] != "/healthz":
                try:
                    # Gate initialize/list as well as calls. End this scope
                    # before dispatch; each tool reauthenticates its invocation.
                    with contexts.request(Request(scope)):
                        pass
                except AccessDenied:
                    await JSONResponse({"error": "access_denied"}, status_code=401)(scope, receive, send)
                    return
            await app(scope, receive, send)

    return AuthenticateMCP()


def create_google_app(*, policy, environment, authenticator, bindings,
                      allowed_hosts, allowed_origins, port=8080):
    """Actual Google/Firestore wiring, with no fake/anonymous fallback."""
    from google.cloud.firestore import AsyncClient
    from ..integrations.assignments import AssignmentsReader
    from ..integrations.calendar_read import CalendarReader
    from ..integrations.google_clients import GoogleWorkspaceClients
    from ..storage.plans import FirestoreDocuments
    from ..storage.snapshots import FirestoreSnapshotStore
    from ..storage.owned_blocks import FirestoreOwnedBlockRegistry

    policy.config.require_environment(environment)
    contexts = RequestContextFactory(policy, boundary="mcp", authenticator=authenticator, bindings=bindings)
    client = AsyncClient(project=policy.config.project_id, database=policy.config.firestore_database)
    documents = FirestoreDocuments(client, project_id=policy.config.project_id,
                                   database_id=policy.config.firestore_database)
    snapshots = FirestoreSnapshotStore(documents, policy)
    registry = FirestoreOwnedBlockRegistry(documents, policy)
    clients = GoogleWorkspaceClients(policy, environment=environment, read_only=True)
    return create_app(contexts=contexts, assignments=AssignmentsReader(clients, snapshots),
                      calendar=CalendarReader(clients, snapshots, registry),
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
    except Exception:
        raise SystemExit("mcp_startup_configuration_required") from None
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, log_level="error")


if __name__ == "__main__":
    main()
