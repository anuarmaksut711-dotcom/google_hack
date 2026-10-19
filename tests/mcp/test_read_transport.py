"""Real localhost MCP protocol; FAKE Google, identity and document adapters.

Not live Google/Firestore/IAM evidence. Real T08/T09 readers and T05 stores.
"""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from campuspulse_recovery.domain.models import (
    AssignmentsSnapshot, CalendarSnapshot, ReadAssignmentsInput, ReadCalendarInput,
)
from campuspulse_recovery.integrations.assignments import AssignmentsReader
from campuspulse_recovery.integrations.google_credentials import WorkspaceAccessError
from campuspulse_recovery.mcp_service.server import create_app
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from tests.integrations.test_calendar_read import Harness, HORIZON, NOW


def run(coro):
    # Unlike the storage unit runner, this suite deliberately allows localhost I/O.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)

SECRET = "sensitive-secret-never-echo"


class TransportHarness(Harness):
    def __init__(self):
        super().__init__()
        self.sheet_failure = None
        self.sheet_rows = [["assignment-1", "course", "Untrusted source", "2026-10-19", "60", "todo"]]
        self.auth_calls = 0
        self.active_contexts = []
        cfg = self.config

        def authenticate(request, *, boundary, now):
            self.auth_calls += 1
            if request.headers.get("authorization") != "Bearer fake-test-only":
                raise AccessDenied(SECRET)
            return AuthenticatedCaller(principal_id=cfg.identities.agent_service_account,
                session_id="session-1", run_id="run-1", authenticated_at=NOW,
                expires_at=NOW + timedelta(hours=1))

        binding = ServerRunBinding(owner_id=cfg.student.trusted_user_id,
            signin_principal_id=cfg.student.allowed_signin_principal,
            session_id="session-1", run_id="run-1", config_revision=cfg.config_revision,
            expires_at=NOW + timedelta(hours=1))
        self.contexts = RequestContextFactory(self.policy, boundary="mcp",
            authenticator=SimpleNamespace(authenticate=authenticate),
            bindings=SimpleNamespace(resolve=lambda **kwargs: binding), clock=lambda: NOW)
        clients = SimpleNamespace(policy=self.policy, sheet_read=self.sheet_read)
        self.assignments = AssignmentsReader(clients, self.snapshots, clock=lambda: NOW)

    def sheet_read(self, context, **kwargs):
        self.policy.require_context(context)
        self.active_contexts.append(context)
        if self.sheet_failure:
            raise self.sheet_failure
        return {"majorDimension": "ROWS", "values": [["assignment_id", "course", "title", "deadline", "remaining_minutes", "status"],
                           *deepcopy(self.sheet_rows)]}


@asynccontextmanager
async def serving(h, *, close=None):
    # Pass a prebound socket to avoid free-port races; production stays 0.0.0.0.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_app(contexts=h.contexts, assignments=h.assignments, calendar=h.reader,
                     allowed_hosts=[f"127.0.0.1:{port}"], allowed_origins=[], port=port, close=close)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@asynccontextmanager
async def session(url):
    async with httpx.AsyncClient(headers={"Authorization": "Bearer fake-test-only"}, timeout=10) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "campuspulse_recovery"
                yield client


def test_initialize_list_call_both_tools_and_scoped_identity():
    async def scenario():
        h = TransportHarness()
        async with serving(h) as url:
            async with session(url) as client:
                tools = (await client.list_tools()).tools
                assert {t.name for t in tools} == {"read_assignments", "read_calendar"}
                for tool, model in zip(tools, [ReadAssignmentsInput, ReadCalendarInput]):
                    assert tool.inputSchema == model.model_json_schema()
                    assert tool.outputSchema and tool.annotations.readOnlyHint
                    assert tool.annotations.idempotentHint and tool.annotations.openWorldHint
                    assert not tool.annotations.destructiveHint
                arguments = ReadCalendarInput(horizon=HORIZON).model_dump(mode="json")
                for name in ("read_assignments", "read_calendar"):
                    result = await client.call_tool(name, arguments)
                    assert not result.isError
                    assert result.structuredContent["source"]["complete"]
                    assert result.structuredContent["source"]["reference"]["run_id"] == "run-1"
                    assert json.loads(result.content[0].text) == result.structuredContent
                assert h.documents.data
                assert h.active_contexts
                for ctx in h.active_contexts:
                    with pytest.raises(AccessDenied):
                        ctx.require_active()
            async with httpx.AsyncClient() as http:
                assert (await http.get(url + "/healthz")).json() == {"status": "alive"}
                denied = await http.post(url + "/mcp", json={})
                assert denied.status_code == 401 and SECRET not in denied.text
    run(scenario())


@pytest.mark.parametrize("failure", ["sheet_api", "calendar_api", "calendar_missing_zone", "storage", "raw"])
def test_adapter_failures_are_errors_and_redacted(failure, caplog):
    async def scenario():
        h = TransportHarness()
        name = "read_assignments"
        if failure == "sheet_api":
            h.sheet_failure = WorkspaceAccessError("workspace_forbidden", http_status=403)
        elif failure.startswith("calendar"):
            name = "read_calendar"
            h.pages = [WorkspaceAccessError("workspace_forbidden", http_status=403)] if failure == "calendar_api" else [{"items": []}]
        elif failure == "raw":
            async def broken(*args):
                raise RuntimeError(SECRET)
            h.assignments = SimpleNamespace(read=broken)
        else:
            async def broken(*args):
                raise RuntimeError(SECRET)
            h.snapshots.put_assignments = broken
        async with serving(h) as url:
            async with session(url) as client:
                result = await client.call_tool(name, ReadCalendarInput(horizon=HORIZON).model_dump(mode="json"))
                assert result.isError
                if failure == "sheet_api":
                    assert not result.structuredContent["source"]["complete"]
                    assert result.structuredContent["source"]["issues"][0]["code"] == "workspace_forbidden"
                else:
                    assert result.structuredContent is None
                assert SECRET not in result.model_dump_json()
    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert SECRET not in caplog.text


def test_arguments_and_metadata_never_become_authority(caplog):
    async def scenario():
        h = TransportHarness()
        async with serving(h) as url:
            async with session(url) as client:
                args = ReadCalendarInput(horizon=HORIZON).model_dump(mode="json")
                invalids = [{**args, "owner_id": SECRET}, {**args, "horizon": {"start": SECRET, "end": SECRET}},
                            {**args, "schema_version": SECRET}, {**args, "approved": True}]
                for value in invalids:
                    result = await client.call_tool("read_assignments", value)
                    assert result.isError and result.content[0].text == "invalid_arguments"
                    assert SECRET not in result.model_dump_json()
                result = await client.call_tool("read_assignments", args,
                    meta={"owner_id": SECRET, "run_id": SECRET, "authorization": SECRET})
                assert not result.isError
                assert result.structuredContent["source"]["reference"]["owner_id"] == "student-1"
                assert SECRET not in result.model_dump_json()
                result = await client.call_tool(SECRET, args)
                assert result.isError and result.content[0].text == "unknown_tool"
    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert SECRET not in caplog.text


def test_transport_host_origin_body_and_spoofed_headers():
    async def scenario():
        h = TransportHarness()
        async with serving(h) as url:
            async with httpx.AsyncClient() as http:
                spoofed = await http.post(url + "/mcp", headers={"X-User": "student-1", "X-Run": "run-1"}, json={})
                assert spoofed.status_code == 401
                headers = {"Authorization": "Bearer fake-test-only", "Accept": "application/json, text/event-stream"}
                assert (await http.post(url + "/mcp", headers={**headers, "Host": "untrusted.invalid"}, json={})).status_code == 421
                assert (await http.post(url + "/mcp", headers={**headers, "Origin": "https://untrusted.invalid"}, json={})).status_code == 403
                assert (await http.post(url + "/mcp", headers=headers, content="x" * 16385)).status_code == 413
            async with session(url) as client:
                value = ReadCalendarInput(horizon=HORIZON).model_dump(mode="json")
                value["horizon"]["end"] = (HORIZON.end + timedelta(seconds=1)).isoformat()
                assert (await client.call_tool("read_calendar", value)).isError
    run(scenario())


@pytest.mark.parametrize("case", ["effort", "deadline", "calendar"])
def test_persisted_incomplete_snapshot_survives_protocol_and_backend_reload(case):
    async def scenario():
        h = TransportHarness()
        name, model = "read_assignments", AssignmentsSnapshot
        if case == "calendar":
            name, model = "read_calendar", CalendarSnapshot
            h.pages = [{"timeZone": "America/New_York", "items": [{"id": "broken-event"}]}]
            expected_code, expected_ref = "calendar_event_invalid", "page-1-item-1"
        else:
            h.sheet_rows[0][4 if case == "effort" else 3] = ""
            expected_code = "needs_input_remaining_minutes" if case == "effort" else "needs_input_deadline"
            expected_ref = "row-3"
        async with serving(h) as url:
            async with session(url) as client:
                response = await client.call_tool(name, ReadCalendarInput(horizon=HORIZON).model_dump(mode="json"))
                assert response.isError and response.content[0].text == "source_incomplete"
                snapshot = model.model_validate(response.structuredContent)
                assert not snapshot.source.complete
                assert any(issue.code == expected_code and issue.item_ref == expected_ref for issue in snapshot.source.issues)
                assert snapshot.source.reference.run_id == "run-1"
                assert h.documents.data
                with h.request("backend") as context:
                    load = h.snapshots.get_calendar if case == "calendar" else h.snapshots.get_assignments
                    saved = await load(context, snapshot.source.reference)
                assert saved == snapshot
                if case == "effort":
                    assert saved.assignments[0].remaining_minutes is None
                elif case == "deadline":
                    assert saved.assignments[0].deadline is None
                else:
                    assert saved.source.timezone == "America/New_York"
    run(scenario())


@pytest.mark.parametrize("case", ["denied", "missing", "malformed", "foreign_reference"])
def test_unavailable_or_invalid_snapshot_never_gets_authoritative_response(case):
    async def scenario():
        h = TransportHarness()
        original = h.assignments
        async def broken(context, horizon):
            if case == "denied":
                raise AccessDenied(SECRET)
            if case == "missing":
                return None
            if case == "malformed":
                return {"source": SECRET}
            value = (await original.read(context, horizon)).model_dump(mode="json")
            value["source"]["reference"]["owner_id"] = "foreign-owner"
            return value
        h.assignments = SimpleNamespace(read=broken)
        async with serving(h) as url:
            async with session(url) as client:
                response = await client.call_tool("read_assignments", ReadAssignmentsInput(horizon=HORIZON).model_dump(mode="json"))
                assert response.isError and response.structuredContent is None
                assert SECRET not in response.model_dump_json()
    run(scenario())


@pytest.mark.parametrize("level", [logging.WARNING, logging.DEBUG])
def test_root_sdk_malformed_metadata_redaction_preserves_application_logs(level, caplog):
    async def scenario():
        h = TransportHarness()
        async with serving(h) as url:
            async with httpx.AsyncClient() as http:
                headers = {"Authorization": "Bearer fake-test-only", "Accept": "application/json",
                           "X-Probe": SECRET}
                message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "read_calendar",
                                      "arguments": ReadCalendarInput(horizon=HORIZON).model_dump(mode="json"),
                                      "_meta": {"progressToken": {"value": SECRET}, "raw": SECRET}}}
                response = await http.post(url + "/mcp", headers=headers, json=message)
                assert response.json()["error"]["code"] == -32602
                assert SECRET not in response.text
                assert not h.calls and not h.documents.data and h.auth_calls == 1
                for key in ("Host", "Origin"):
                    hostile = {**headers, key: SECRET if key == "Host" else "https://" + SECRET}
                    rejected = await http.post(url + "/mcp", headers=hostile, json={})
                    assert rejected.status_code in (421, 403) and SECRET not in rejected.text
                # Raw adapter errors also remain safe with DEBUG enabled.
                h.sheet_failure = RuntimeError(SECRET)
                async with session(url) as client:
                    result = await client.call_tool("read_assignments", ReadAssignmentsInput(horizon=HORIZON).model_dump(mode="json"))
                    assert result.isError and SECRET not in result.model_dump_json()
        logging.getLogger("campuspulse.test_application").warning("application_safe_marker")
        logging.getLogger().warning("root_application_safe_marker")
    with caplog.at_level(level):
        run(scenario())
    assert SECRET not in caplog.text
    assert "mcp_transport_event" in caplog.text
    assert "application_safe_marker" in caplog.text
    assert "root_application_safe_marker" in caplog.text


@pytest.mark.parametrize("asynchronous", [False, True])
def test_lifespan_closes_sync_and_async_client_exactly_once(asynchronous):
    async def scenario():
        closed = []
        class SynchronousClient:
            # Production AsyncClient.close is a synchronous method returning None.
            def close(self):
                closed.append("closed")
        async def async_close():
            await asyncio.sleep(0)
            closed.append("closed")
        async with serving(TransportHarness(), close=async_close if asynchronous else SynchronousClient().close) as url:
            async with session(url) as client:
                assert len((await client.list_tools()).tools) == 2
            assert closed == []
        assert closed == ["closed"]
    run(scenario())


def fake_entrypoint_factory(*, port):
    """Explicit test-only factory for local/container smoke. Never cloud wiring."""
    h = TransportHarness()
    return create_app(contexts=h.contexts, assignments=h.assignments, calendar=h.reader,
                      allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
                      allowed_origins=[], port=port)


def test_entrypoint_injected_port_and_fail_closed():
    product = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("CAMPUSPULSE_MCP_APP_FACTORY", None)
    script = product / "deploy" / "mcp-entrypoint.py"
    # Include test dependencies only in this child, including when testing wheel.
    package_root = Path(sys.modules["campuspulse_recovery"].__file__).parents[1]
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(product), str(Path(pytest.__file__).parents[1])])
    failed = subprocess.run([sys.executable, "-B", str(script)], env=env, capture_output=True, text=True, timeout=15)
    assert failed.returncode != 0 and "mcp_startup_configuration_required" in failed.stderr
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    env.update(PORT=str(port), CAMPUSPULSE_MCP_APP_FACTORY="tests.mcp.test_read_transport:fake_entrypoint_factory")
    child = subprocess.Popen([sys.executable, "-B", str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            assert child.poll() is None, child.communicate()[1].decode(errors="replace")
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=.2)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.05)
        else:
            pytest.fail("entrypoint_did_not_start")
        async def scenario():
            async with session(f"http://127.0.0.1:{port}") as client:
                assert len((await client.list_tools()).tools) == 2
                result = await client.call_tool("read_calendar", ReadCalendarInput(horizon=HORIZON).model_dump(mode="json"))
                assert not result.isError
        run(scenario())
    finally:
        child.terminate()
        child.communicate(timeout=10)
