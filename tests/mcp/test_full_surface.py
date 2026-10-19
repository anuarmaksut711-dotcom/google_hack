"""Actual full MCP protocol/domain composition; synthetic auth/UI/Google/documents.

Local fakes prove no live consent/IAM/distributed isolation. Optional emulator
uses only explicit loopback and AnonymousCredentials. No production side effects.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import copy, deepcopy
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

from campuspulse_recovery.application.apply import ApplyService
from campuspulse_recovery.application.verify import VerificationService
from campuspulse_recovery.domain.models import (
    ApplyPlanInput, ApplyResult, AssignmentsSnapshot, CalendarSnapshot,
    ReadAssignmentsInput, ReadCalendarInput, VerificationResult, VerifyPlanInput,
)
from campuspulse_recovery.mcp_service.server import create_complete_app, create_app
from campuspulse_recovery.mcp_service import write_tools
from campuspulse_recovery.security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from campuspulse_recovery.storage.plans import _encode, plan_reference
from tests.application.test_apply import Harness
from tests.integrations.test_calendar_read import event
from tests.storage.test_apply_coordination import FakeStopGuard

SECRET = "t21-secret-event-token-never-echo"
CONTRACTS = {
    "read_assignments": (ReadAssignmentsInput, AssignmentsSnapshot),
    "read_calendar": (ReadCalendarInput, CalendarSnapshot),
    "apply_plan": (ApplyPlanInput, ApplyResult),
    "verify_plan": (VerifyPlanInput, VerificationResult),
}


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError("T21 forbids external network")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)


async def prepared(monkeypatch, *, documents=None, **options):
    h = Harness(monkeypatch)
    if documents is not None:
        replace_documents(h, documents)
    await h.prepare(**options)
    h.run_id = "t21-transport-run"
    h.auth_case = None
    h.active_contexts = []
    h.coordination.recovery_guard = FakeStopGuard()
    verification = VerificationService(h.plans, h.snapshots, h.executions, h.registry,
        h.assignment_reader, h.reader, h.policy, clock=h.clock)
    h.executions.verification_guard = verification.transaction_guard()
    h.verification = verification
    h.executor = ApplyService(h.service, h.writer, clock=h.clock, verification_reader=verification,
                             write_timeout_seconds=.15, deadline_seconds=10)

    def authenticate(request, *, boundary, now):
        if request.headers.get("authorization") != "Bearer fake-test-only":
            raise AccessDenied(SECRET)
        return AuthenticatedCaller(principal_id=("foreign" if h.auth_case == "principal"
            else h.config.identities.agent_service_account), session_id="t21-test-session",
            run_id=h.run_id, authenticated_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(seconds=-.5 if h.auth_case == "expired" else 300))
    def resolve(**kwargs):
        if h.auth_case == "missing":
            return None
        return ServerRunBinding(owner_id="foreign" if h.auth_case == "owner" else h.config.student.trusted_user_id,
            signin_principal_id=h.config.student.allowed_signin_principal, session_id="t21-test-session",
            run_id=h.run_id, config_revision=h.config.config_revision, expires_at=h.now + timedelta(minutes=5))
    h.contexts = RequestContextFactory(h.policy, boundary="mcp", clock=h.clock,
        authenticator=SimpleNamespace(authenticate=authenticate), bindings=SimpleNamespace(resolve=resolve))
    return h


def replace_documents(h, documents):
    """Compose actual repositories before proposal/consent/apply on emulator."""
    from campuspulse_recovery.application.preflight import PreflightService
    from campuspulse_recovery.storage.plans import FirestorePlanStore
    from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
    from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
    from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
    from campuspulse_recovery.storage.operations import FirestoreExecutionStore
    from campuspulse_recovery.web.approval import FirestoreApprovalStore
    h.documents = documents
    h.plans = FirestorePlanStore(documents, h.policy)
    h.snapshots = FirestoreSnapshotStore(documents, h.policy)
    h.registry = FirestoreOwnedBlockRegistry(documents, h.policy)
    h.approvals = FirestoreApprovalStore(documents, h.policy, clock=h.clock)
    h.coordination = FirestoreCalendarCoordination(documents, h.policy, clock=h.clock, recovery_guard=FakeStopGuard())
    h.executions = FirestoreExecutionStore(documents, h.policy, coordination=h.coordination,
                                          approval_guard=h.approvals.transaction_guard())
    h.assignment_reader.snapshots = h.snapshots
    h.reader.snapshots, h.reader.registry = h.snapshots, h.registry
    h.writer.registry, h.writer.coordination, h.writer.plans = h.registry, h.coordination, h.plans
    h.service = PreflightService(h.plans, h.snapshots, h.approvals, h.executions, h.coordination,
        h.registry, h.assignment_reader, h.reader, h.policy, clock=h.clock)


def app_for(h, port, *, close=None):
    return create_complete_app(contexts=h.contexts, assignments=h.assignment_reader, calendar=h.reader,
        apply=h.executor, verification=h.verification, allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[], port=port, close=close)


@asynccontextmanager
async def serving(h, *, close=None):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app_for(h, port, close=close), log_level="error", access_log=False))
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
    async with httpx.AsyncClient(headers={"Authorization": "Bearer fake-test-only"}, timeout=15) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "campuspulse_recovery"
                yield client


def args(h, name):
    if name.startswith("read_"):
        return ReadCalendarInput(horizon=h.horizon).model_dump(mode="json")
    if name == "apply_plan":
        return h.request_input.model_dump(mode="json")
    return VerifyPlanInput(plan_id=h.plan.payload.plan_id, version=1).model_dump(mode="json")


async def call(client, h, name, **changes):
    return await client.call_tool(name, {**args(h, name), **changes})


@pytest.mark.parametrize("update,two", [(False, False), (True, False), (False, True), (True, True)])
def test_full_protocol_exact_schemas_actual_services_apply_verify_saved_replay(monkeypatch, update, two):
    async def scenario():
        h = await prepared(monkeypatch, update=update, two=two)
        async with serving(h) as url, session(url) as client:
            tools = (await client.list_tools()).tools
            assert [t.name for t in tools] == list(CONTRACTS)
            for tool in tools:
                inputs, outputs = CONTRACTS[tool.name]
                assert tool.inputSchema == inputs.model_json_schema()
                assert tool.outputSchema == outputs.model_json_schema()
                assert tool.inputSchema["additionalProperties"] is False
                assert tool.annotations.readOnlyHint == (tool.name != "apply_plan")
                assert tool.annotations.destructiveHint == (tool.name == "apply_plan")
                assert tool.annotations.idempotentHint == (tool.name != "verify_plan")
                assert tool.annotations.openWorldHint
            for name in ("read_assignments", "read_calendar"):
                result = await call(client, h, name)
                assert not result.isError and result.structuredContent["source"]["complete"]
                assert result.structuredContent["source"]["reference"]["run_id"] == h.run_id
                assert json.loads(result.content[0].text) == result.structuredContent
                CONTRACTS[name][1].model_validate(result.structuredContent)
            result = await call(client, h, "apply_plan")
            applied = ApplyResult.model_validate(result.structuredContent)
            assert not result.isError and applied.execution.status == "applied_unverified"
            assert applied.verification is None and all(o.status == "completed" for o in applied.execution.operations)
            assert len(h.api.mutations) == len(h.plan.payload.operations)
            before = deepcopy(h.api.mutations)
            duplicate = await call(client, h, "apply_plan")
            assert ApplyResult.model_validate(duplicate.structuredContent).execution == applied.execution
            assert h.api.mutations == before
            h.now += timedelta(seconds=1)
            h.run_id = "t21-fresh-verification"
            verified = await call(client, h, "verify_plan")
            proof = VerificationResult.model_validate(verified.structuredContent)
            assert not verified.isError and proof.status == "verified" and proof.verification_at == h.now
            assert proof.run_id == h.run_id and all(e.event_link for e in proof.observed_events)
            assert proof.checked_operations == tuple(o.operation_id for o in h.plan.payload.operations)
            h.now += timedelta(seconds=5)
            replay = ApplyResult.model_validate((await call(client, h, "apply_plan")).structuredContent)
            assert replay.execution.status == "verified" and replay.verification == proof
            assert replay.verification.verification_at != h.now and h.api.mutations == before
    run(scenario())


@pytest.mark.parametrize("tool", list(CONTRACTS))
@pytest.mark.parametrize("field", ["approved", "operations", "owner_id", "calendar_id", "principal_id", "run_id",
                                   "session_id", "source", "lease", "verification", "context"])
def test_extra_model_authority_rejected_before_services(monkeypatch, tool, field):
    async def scenario():
        h = await prepared(monkeypatch)
        before = deepcopy(h.documents.data)
        async with serving(h) as url, session(url) as client:
            result = await call(client, h, tool, **{field: SECRET})
            assert result.isError and result.structuredContent is None
            assert result.content[0].text == "invalid_arguments" and SECRET not in result.model_dump_json()
        assert not h.api.mutations and h.documents.data == before
    run(scenario())


@pytest.mark.parametrize("case", ["missing", "revoked", "expired", "tampered", "foreign", "key_conflict"])
def test_durable_consent_resource_and_request_guards(monkeypatch, case):
    async def scenario():
        h = await prepared(monkeypatch, approve=case != "missing")
        if case == "revoked":
            with h.scope(boundary="backend") as ctx:
                with h.approvals.ui_action(ctx, plan_reference(h.plan), "t21-revoke") as action:
                    await h.approvals.invalidate(ctx, plan_reference(h.plan), reason="revoked", at=action.at)
        elif case == "expired":
            h.now += timedelta(minutes=11)
        elif case == "tampered":
            path = next(p for p in h.documents.data if p.startswith("cp_approvals/"))
            h.documents.data[path] = {"corrupt": SECRET}
        elif case == "key_conflict":
            with h.scope() as ctx:
                await h.executions.bind_request(ctx, request_key=h.request_input.request_key,
                    plan=plan_reference(h.plan), at=h.now)
            other = await h.seed(version=2)
            h.request_input = type(h.request_input)(plan_id=other.payload.plan_id, version=2, request_key=h.request_input.request_key)
        async with serving(h) as url, session(url) as client:
            result = await call(client, h, "apply_plan", **({"plan_id": "foreign-plan"} if case == "foreign" else {}))
            assert result.isError and SECRET not in result.model_dump_json()
            if result.structuredContent:
                assert result.structuredContent["execution"]["status"] not in ("verified", "applied_unverified")
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case", ["conflict", "outage", "incomplete", "assignment_outage"])
def test_actual_fresh_final_failure_is_not_success(monkeypatch, case):
    async def scenario():
        h = await prepared(monkeypatch)
        async with serving(h) as url, session(url) as client:
            assert not (await call(client, h, "apply_plan")).isError
            target = h.plan.payload.blocks[0].interval
            if case == "conflict":
                h.api.events["new-busy"] = event("new-busy", start=target.start)
            elif case == "outage":
                h.api.native_zone = "Invalid/Zone"
            elif case == "incomplete":
                h.api.events["bad"] = event("bad", start={})
            else:
                h.rows = [["bad"]]
            h.now += timedelta(seconds=1)
            result = await call(client, h, "verify_plan")
            assert result.isError
            value = VerificationResult.model_validate(result.structuredContent)
            assert value.status == ("verification_failed" if case == "conflict" else "applied_unverified")
            assert value.findings and len(h.api.mutations) == 1
            with h.scope() as ctx:
                assert (await h.verification.latest(ctx, plan_reference(h.plan))).result == value
    run(scenario())


@pytest.mark.parametrize("case", ["timeout_success", "unknown", "partial"])
def test_write_outcome_and_progress_are_honest(monkeypatch, case):
    async def scenario():
        h = await prepared(monkeypatch, two=case == "partial")
        if case == "timeout_success":
            def after_write(raw, response):
                raise TimeoutError(SECRET)
            h.api.after_write = after_write
        elif case == "unknown":
            h.api.failures["POST"] = TimeoutError(SECRET)
        else:
            original = h.api.request
            count = 0
            def request(uri, method="GET", **kw):
                nonlocal count
                if method == "POST":
                    count += 1
                    if count == 2:
                        raise TimeoutError(SECRET)
                return original(uri, method=method, **kw)
            h.api.request = request
        async with serving(h) as url, session(url) as client:
            result = await call(client, h, "apply_plan")
            value = ApplyResult.model_validate(result.structuredContent)
            assert SECRET not in result.model_dump_json()
            expected = {"timeout_success": "applied_unverified", "unknown": "unknown", "partial": "partial"}[case]
            assert value.execution.status == expected, value
            assert result.isError == (case != "timeout_success") and value.verification is None
            before = deepcopy(h.api.mutations)
            again = await call(client, h, "apply_plan")
            assert h.api.mutations == before
            assert again.structuredContent["execution"]["status"] == expected
            if case == "partial":
                assert value.execution.operations[0].status == "completed"
    run(scenario())


@pytest.mark.parametrize("name", ["read_assignments", "read_calendar"])
def test_incomplete_read_exact_schema_preserved_on_full_transport(monkeypatch, name):
    async def scenario():
        h = await prepared(monkeypatch)
        if name == "read_assignments":
            h.rows[0][4] = ""
        else:
            h.api.events["bad"] = event("bad", start={})
        async with serving(h) as url, session(url) as client:
            result = await call(client, h, name)
            assert result.isError and result.content[0].text == "source_incomplete"
            snapshot = CONTRACTS[name][1].model_validate(result.structuredContent)
            assert not snapshot.source.complete and snapshot.source.issues
    run(scenario())


@pytest.mark.parametrize("case", ["principal", "owner", "missing", "expired"])
def test_authenticated_binding_gate_fails_closed(monkeypatch, case):
    async def scenario():
        h = await prepared(monkeypatch)
        h.auth_case = case
        async with serving(h) as url, httpx.AsyncClient() as http:
            response = await http.post(url + "/mcp", json={}, headers={"Authorization": "Bearer fake-test-only"})
            assert response.status_code == 401 and response.json() == {"error": "access_denied"}
            assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_transport_guards_redaction_unknown_tools_and_single_close(monkeypatch, caplog, asynchronous):
    async def scenario():
        h = await prepared(monkeypatch)
        closed = []
        def close():
            closed.append(True)
        async def aclose():
            close()
        async with serving(h, close=aclose if asynchronous else close) as url:
            async with session(url) as client:
                for name in ("solve_repair", "approve_plan", "save_plan", SECRET):
                    result = await client.call_tool(name, {"approved": True})
                    assert result.isError and SECRET not in result.model_dump_json()
                original = h.documents.get
                async def broken(*a):
                    raise RuntimeError(SECRET)
                h.documents.get = broken
                result = await call(client, h, "apply_plan")
                assert result.isError and result.content[0].text == "domain_failed_reload_request_before_retry"
                h.documents.get = original
            async with httpx.AsyncClient(headers={"Authorization": "Bearer fake-test-only",
                "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}) as http:
                for headers, data, status in [({"Host": "evil.example"}, "{}", 421),
                    ({"Origin": "https://evil.example"}, "{}", 403), ({}, "x" * 17000, 413)]:
                    response = await http.post(url + "/mcp", content=data, headers=headers)
                    assert response.status_code == status
                assert (await http.get(url + "/healthz")).json() == {"status": "alive"}
        assert closed == [True]
    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert SECRET not in caplog.text


@pytest.mark.parametrize("missing", ["stop", "verification_guard", "approval_guard", "replay", "readonly", "reader"])
def test_complete_startup_requires_actual_same_authority_services(monkeypatch, missing):
    async def scenario():
        h = await prepared(monkeypatch)
        if missing == "stop":
            h.coordination.recovery_guard = None
        elif missing == "verification_guard":
            h.executions.verification_guard = None
        elif missing == "approval_guard":
            h.executions.approval_guard = None
        elif missing == "replay":
            h.executor.verification_reader = None
        elif missing == "readonly":
            h.writer.clients.read_only = True
        else:
            h.verification.calendar = copy(h.reader)
        with pytest.raises(ValueError, match="complete_mcp_authority_guards_required"):
            app_for(h, 8080)
    run(scenario())


def _entrypoint_harness():
    """LOCAL TEST ONLY: injected synthetic identity/consent/Google/documents.

    Cloud-shaped explicit test config permits testing main's production signal.
    This fixture is outside the wheel and never a deployable auth composition.
    """
    from campuspulse_recovery.config import AppConfig
    from tests.storage import test_plans
    patch = pytest.MonkeyPatch()
    original = test_plans.config
    cfg = original()
    cloud = AppConfig.model_validate({**cfg.model_dump(), "mode": "cloud"})
    patch.setattr(test_plans, "config", lambda: cloud)
    h = run(prepared(patch))
    return h, patch


def fake_full_entrypoint_factory(*, port):
    h, patch = _entrypoint_harness()
    return app_for(h, port, close=patch.undo)


def fake_legacy_production_factory(*, port):
    h, patch = _entrypoint_harness()
    return create_app(contexts=h.contexts, assignments=h.assignment_reader, calendar=h.reader,
        allowed_hosts=[f"127.0.0.1:{port}"], allowed_origins=[], port=port, close=patch.undo)


def test_production_entrypoint_rejects_legacy_and_runs_complete_port_health(monkeypatch):
    product = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    package_root = Path(sys.modules["campuspulse_recovery"].__file__).parents[1]
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(product), str(Path(pytest.__file__).parents[1])])
    env.update(K_SERVICE="t21-LOCAL-FAKE-production-mode", PORT="8080",
        CAMPUSPULSE_MCP_APP_FACTORY="tests.mcp.test_full_surface:fake_legacy_production_factory")
    script = product / "deploy" / "mcp-entrypoint.py"
    failed = subprocess.run([sys.executable, "-B", str(script)], env=env, capture_output=True, timeout=15)
    assert failed.returncode != 0 and b"mcp_startup_configuration_required" in failed.stderr
    assert b"Traceback" not in failed.stderr
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    env.update(PORT=str(port), CAMPUSPULSE_MCP_APP_FACTORY="tests.mcp.test_full_surface:fake_full_entrypoint_factory")
    child = subprocess.Popen([sys.executable, "-B", str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(150):
            assert child.poll() is None, child.communicate()[1].decode(errors="replace")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=.2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.05)
        else:
            pytest.fail("full_entrypoint_did_not_start")
        async def scenario():
            async with session(f"http://127.0.0.1:{port}") as client:
                assert [t.name for t in (await client.list_tools()).tools] == list(CONTRACTS)
                from tests.integrations.test_calendar_read import HORIZON
                for name in ("read_assignments", "read_calendar"):
                    assert not (await client.call_tool(name, ReadCalendarInput(horizon=HORIZON).model_dump(mode="json"))).isError
                applied = await client.call_tool("apply_plan", ApplyPlanInput(plan_id="t18-plan", version=1, request_key="entry-key").model_dump(mode="json"))
                assert applied.structuredContent["execution"]["status"] == "applied_unverified"
                verified = await client.call_tool("verify_plan", VerifyPlanInput(plan_id="t18-plan", version=1).model_dump(mode="json"))
                assert verified.structuredContent["status"] == "verified"
        run(scenario())
    finally:
        child.terminate()
        child.communicate(timeout=10)


@pytest.mark.parametrize("case", ["copied", "serialized", "closed", "expired", "backend"])
def test_domain_services_reject_nonactive_or_wrong_boundary_capability(monkeypatch, case):
    async def scenario():
        h = await prepared(monkeypatch)
        with h.scope(boundary="backend" if case == "backend" else "mcp") as context:
            if case == "copied":
                with pytest.raises(TypeError):
                    copy(context)

                from dataclasses import fields
                candidate = object.__new__(type(context))
                for field in fields(context):
                    object.__setattr__(candidate, field.name, getattr(context, field.name))
            else:
                candidate = context.trusted.model_dump() if case == "serialized" else context
            if case == "expired":
                h.now += timedelta(minutes=6)
            if case != "closed":
                for service, request in [(h.executor.apply, h.request_input),
                    (h.verification.verify, VerifyPlanInput(plan_id=h.plan.payload.plan_id, version=1))]:
                    with pytest.raises(AccessDenied):
                        await service(candidate, request)
        if case == "closed":
            with pytest.raises(AccessDenied):
                await h.executor.apply(context, h.request_input)
        assert not h.api.mutations
    run(scenario())


def test_meta_headers_cannot_grant_consent_or_change_run(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch, approve=False)
        async with serving(h) as url, session(url) as client:
            result = await client.call_tool("apply_plan", args(h, "apply_plan"),
                meta={"approved": True, "owner_id": SECRET, "run_id": SECRET, "context": SECRET})
            assert result.isError and SECRET not in result.model_dump_json()
            assert not h.api.mutations
            async with httpx.AsyncClient() as http:
                response = await http.post(url + "/mcp", json={}, headers={"X-User": "student-1", "X-Run": SECRET})
                assert response.status_code == 401
    run(scenario())


def test_concurrent_apply_uses_durable_claim_and_stale_update_keeps_if_match(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch, update=True)
        h.api.before_patch = lambda raw: raw.update(etag='"user-edit"')
        async with serving(h) as url, session(url) as a, session(url) as b:
            results = await asyncio.gather(call(a, h, "apply_plan"), call(b, h, "apply_plan"))
            assert len(h.api.mutations) == 1
            assert h.api.mutations[0][4]["If-Match"] == h.plan.payload.operations[0].expected_etag
            assert all(r.isError for r in results)
            assert h.ledger().execution.status == "unknown"
            result = await call(a, h, "verify_plan")
            assert result.isError and result.structuredContent["status"] != "verified"
    run(scenario())


def test_full_protocol_firestore_emulator_actual_apply_and_verification(monkeypatch):
    import re
    from uuid import uuid4
    host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
    if not host:
        pytest.skip("Explicit loopback Firestore emulator absent; fake documents are not distributed proof")
    assert re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]{1,5}", host)
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import AsyncClient
    from campuspulse_recovery.config import AppConfig
    from campuspulse_recovery.storage.plans import FirestoreDocuments
    from tests.storage import test_plans
    cfg = test_plans.config()
    student = {**cfg.student.model_dump(), "trusted_user_id": "t21-" + uuid4().hex,
               "calendar_id": "t21-" + uuid4().hex + "@group.calendar.google.com"}
    cfg = AppConfig.model_validate({**cfg.model_dump(), "student": student})
    monkeypatch.setattr(test_plans, "config", lambda: cfg)
    async def scenario():
        db = AsyncClient(project=cfg.project_id, database=cfg.firestore_database, credentials=AnonymousCredentials())
        try:
            docs = FirestoreDocuments(db, project_id=cfg.project_id, database_id=cfg.firestore_database)
            h = await prepared(monkeypatch, documents=docs)
            async with serving(h) as url, session(url) as client:
                assert [t.name for t in (await client.list_tools()).tools] == list(CONTRACTS)
                for name in ("read_assignments", "read_calendar"):
                    assert not (await call(client, h, name)).isError
                applied = await call(client, h, "apply_plan")
                assert not applied.isError and applied.structuredContent["execution"]["status"] == "applied_unverified"
                result = await call(client, h, "verify_plan")
                assert not result.isError and result.structuredContent["status"] == "verified"
                replay = await call(client, h, "apply_plan")
                assert replay.structuredContent["verification"] == result.structuredContent
                assert len(h.api.mutations) == 1
        finally:
            db.close()
    run(scenario())


def test_verified_replay_retains_original_proof_and_reports_later_outage(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch)
        async with serving(h) as url, session(url) as client:
            assert not (await call(client, h, "apply_plan")).isError
            proof = (await call(client, h, "verify_plan")).structuredContent
            assert proof["status"] == "verified"
            h.now += timedelta(seconds=10)
            h.run_id = "later-outage-run"
            h.api.native_zone = "Invalid/Zone"
            outage = await call(client, h, "verify_plan")
            assert outage.isError and outage.structuredContent["status"] == "applied_unverified"
            replay = await call(client, h, "apply_plan")
            assert replay.structuredContent["execution"]["status"] == "verified"
            assert replay.structuredContent["verification"] == proof
            assert "verify_later_observation_available" in {f["code"] for f in replay.structuredContent["findings"]}
            assert len(h.api.mutations) == 1
    run(scenario())


@pytest.mark.parametrize("name", list(CONTRACTS))
def test_malformed_version_fields_are_safe_before_domain_calls(monkeypatch, name):
    async def scenario():
        h = await prepared(monkeypatch)
        before = deepcopy(h.documents.data)
        async with serving(h) as url, session(url) as client:
            changes = [{"schema_version": SECRET}]
            if name.startswith("read_"):
                changes.append({"horizon": {"start": SECRET, "end": SECRET}})
                changes.append({"horizon": {"start": h.horizon.start.isoformat(),
                    "end": (h.horizon.end + timedelta(seconds=1)).isoformat()}})
            else:
                changes.extend([{"version": 0}, {"version": True}, {"plan_id": ""}])
            for invalid in changes:
                result = await call(client, h, name, **invalid)
                assert result.isError and result.content[0].text == "invalid_arguments"
                assert SECRET not in result.model_dump_json()
        assert h.documents.data == before and not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case", ["sheet", "calendar", "storage"])
def test_full_transport_read_adapter_errors_remain_redacted(monkeypatch, case, caplog):
    async def scenario():
        h = await prepared(monkeypatch)
        name = "read_calendar" if case == "calendar" else "read_assignments"
        if case == "calendar":
            h.api.native_zone = "Invalid/Zone"
        elif case == "sheet":
            def fail(*a, **k):
                raise RuntimeError(SECRET)
            h.assignment_reader.clients.sheet_read = fail
        else:
            async def fail(*a, **k):
                raise RuntimeError(SECRET)
            h.documents.transact = fail
        async with serving(h) as url, session(url) as client:
            result = await call(client, h, name)
            assert result.isError and SECRET not in result.model_dump_json()
            if result.structuredContent is not None:
                assert not result.structuredContent["source"]["complete"]
    with caplog.at_level(logging.DEBUG):
        run(scenario())
    assert SECRET not in caplog.text


def test_google_complete_factory_uses_write_credentials_and_same_real_services(monkeypatch):
    from google.cloud import firestore
    from campuspulse_recovery.mcp_service import server
    from tests.storage.test_plans import config
    from campuspulse_recovery.security.resources import ResourcePolicy
    policy = ResourcePolicy(config(), environment="local")
    clients = []
    class Client:
        def __init__(self, *, project, database):
            self.project, self._database = project, database
            clients.append(self)
        def close(self):
            pass
    monkeypatch.setattr(firestore, "AsyncClient", Client)
    actual = server.create_complete_app
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return actual(**kwargs)
    monkeypatch.setattr(server, "create_complete_app", capture)
    options = dict(policy=policy, environment="local", authenticator=object(), bindings=object(),
                   allowed_hosts=["localhost:8080"], allowed_origins=[])
    with pytest.raises(ValueError, match="trusted_stopped_writer_provider_required"):
        server.create_google_app(**options, recovery_guard=None)
    assert not clients
    app = server.create_google_app(**options, recovery_guard=FakeStopGuard())
    assert app.complete and len(clients) == 1
    apply, verifier = captured["apply"], captured["verification"]
    assert not apply.writer.clients.read_only
    assert captured["assignments"].clients is apply.writer.clients is captured["calendar"].clients
    assert apply.verification_reader is verifier and apply.executions.verification_guard.store is verifier
    assert apply.executions.approval_guard.store is apply.approvals
    assert apply.plans.documents is verifier.documents is apply.coordination.documents
