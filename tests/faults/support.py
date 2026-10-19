"""T27 local harness. Identity/consent and Google effects are synthetic.

Reuse accepted fixture *construction*, never predecessor test functions or
solver output as an oracle. Firestore cases use the actual durable adapter.
"""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace

import pytest

from campuspulse_recovery.application.apply import ApplyService
from campuspulse_recovery.application.preflight import PreflightService
from campuspulse_recovery.application.verify import VerificationService
from campuspulse_recovery.config import AppConfig
from campuspulse_recovery.domain.models import ApplyPlanInput, PlanVersion, VerifyPlanInput
from campuspulse_recovery.integrations.calendar_write import CalendarAdapter
from campuspulse_recovery.security.context import AuthenticatedCaller, RequestContextFactory, ServerRunBinding
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
from campuspulse_recovery.storage.operations import FirestoreExecutionStore
from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
from campuspulse_recovery.storage.plans import FirestoreDocuments, FirestorePlanStore
from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
from campuspulse_recovery.web.approval import FirestoreApprovalStore
from tests.application.test_apply import Harness as AcceptedFixture
from tests.integrations.test_calendar_read import event
from tests.storage.test_apply_coordination import FakeStopGuard
from tests.storage.test_plans import NOW, config, run


def codes(result):
    return {f.code for f in result.findings}


def install_network_guard(monkeypatch, *, loopback=False):
    connect = socket.socket.connect
    def guarded(sock, address):
        if not loopback or not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise AssertionError("T27 forbids non-literal-loopback network")
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connect_ex forbidden")))
    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ADC forbidden")))


def synthetic_config(namespace):
    data = config().model_dump()
    data.update(project_id="demo-campuspulse-t27", firestore_database="t27-" + namespace)
    # Retained fixture locations are inert validation data, never deployment targets.
    data["student"].update(trusted_user_id="student-" + namespace,
                           calendar_id=namespace + "@group.calendar.google.com")
    for key in data["identities"]:
        data["identities"][key] = key.replace("_service_account", "") + "-test@demo-campuspulse-t27.iam.gserviceaccount.com"
    return AppConfig.model_validate(data)


def emulator_documents(cfg):
    host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
    if not host:
        pytest.skip("DEFERRED: real Firestore emulator not configured; no fake distributed proof")
    assert re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", host), "literal loopback required"
    assert cfg.project_id == "demo-campuspulse-t27" and cfg.firestore_database.startswith("t27-")
    from google.cloud.firestore_v1 import AsyncClient
    from google.auth.credentials import AnonymousCredentials
    client = AsyncClient(project=cfg.project_id, database=cfg.firestore_database,
                         credentials=AnonymousCredentials())
    return FirestoreDocuments(client, project_id=cfg.project_id, database_id=cfg.firestore_database), client


class Harness(AcceptedFixture):
    def recompose(self, documents=None, cfg=None):
        """New repository/executor objects, not a high-level success stub."""
        if cfg is not None:
            self.config = cfg
            self.policy = ResourcePolicy(cfg, environment="local")
            self.clients.policy = self.policy
            self.now = NOW
        if documents is not None:
            self.documents = documents
        self.plans = FirestorePlanStore(self.documents, self.policy)
        self.snapshots = FirestoreSnapshotStore(self.documents, self.policy)
        self.registry = FirestoreOwnedBlockRegistry(self.documents, self.policy)
        self.coordination = FirestoreCalendarCoordination(self.documents, self.policy, clock=self.clock,
                                                        recovery_guard=FakeStopGuard())
        self.approvals = FirestoreApprovalStore(self.documents, self.policy, clock=self.clock)
        self.executions = FirestoreExecutionStore(self.documents, self.policy, coordination=self.coordination,
                                                  approval_guard=self.approvals.transaction_guard())
        self.assignment_reader.clients.policy = self.policy
        self.assignment_reader.snapshots = self.snapshots
        self.reader.snapshots, self.reader.registry = self.snapshots, self.registry
        if hasattr(self.reader.clients, "policy"):
            self.reader.clients.policy = self.policy
        self.service = PreflightService(self.plans, self.snapshots, self.approvals, self.executions,
            self.coordination, self.registry, self.assignment_reader, self.reader, self.policy, clock=self.clock)
        self.writer = CalendarAdapter(self.clients, self.snapshots, self.registry, self.coordination,
                                      self.plans, clock=self.clock)
        self.verification = VerificationService(self.plans, self.snapshots, self.executions, self.registry,
            self.assignment_reader, self.reader, self.policy, clock=self.clock)
        self.executions.verification_guard = self.verification.transaction_guard()
        self.executor = ApplyService(self.service, self.writer, clock=self.clock,
                                     verification_reader=self.verification)

    @contextmanager
    def scope(self, *, boundary="mcp", lifetime=timedelta(minutes=5)):
        principal = self.config.student.allowed_signin_principal if boundary == "backend" else self.config.identities.agent_service_account
        caller = AuthenticatedCaller(principal_id=principal, session_id="t27-synthetic-session",
            run_id="t27-run-" + str(os.getpid()), authenticated_at=self.now, expires_at=self.now + lifetime)
        binding = ServerRunBinding(owner_id=self.config.student.trusted_user_id,
            signin_principal_id=self.config.student.allowed_signin_principal, session_id=caller.session_id,
            run_id=caller.run_id, config_revision=self.config.config_revision, expires_at=caller.expires_at)
        factory = RequestContextFactory(self.policy, boundary=boundary, clock=self.clock,
            authenticator=SimpleNamespace(authenticate=lambda *a, **k: caller),
            bindings=SimpleNamespace(resolve=lambda **k: binding))
        with factory.request(object()) as ctx:
            token = self.current.set(ctx)
            try:
                yield ctx
            finally:
                self.current.reset(token)

    async def verify(self):
        with self.scope() as ctx:
            return await self.verification.verify(ctx, VerifyPlanInput(plan_id=self.plan.payload.plan_id,
                                                                       version=self.plan.payload.version))

    async def revoke(self):
        from campuspulse_recovery.storage.plans import plan_reference
        with self.scope(boundary="backend") as ctx:
            with self.approvals.ui_action(ctx, plan_reference(self.plan), "t27-synthetic-revoke") as action:
                await self.approvals.invalidate(ctx, plan_reference(self.plan), reason="revoked", at=action.at)


async def prepared(monkeypatch, **options):
    h = Harness(monkeypatch)
    h.recompose()
    await h.prepare(**options)
    return h


def save_process_result(path, **values):
    Path(path).write_text(json.dumps({"pid": os.getpid(), **values}, default=str, indent=2), encoding="utf8")


class ProxyCalendar:
    """Only the fake Google transport crosses loopback to a shared test edge."""
    def __init__(self, endpoint):
        assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", endpoint)
        self.endpoint = endpoint

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        import urllib.request
        import httplib2
        payload = json.dumps(dict(uri=uri, method=method, body=body, headers=headers)).encode()
        request = urllib.request.Request(self.endpoint, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.load(response)
        return httplib2.Response({"status": str(value["status"]), "content-type": "application/json"}), value["body"].encode()

    def close(self):
        pass


def process_worker(spec_path, output, gate):
    """Independent process: actual ApplyService + actual FirestoreDocuments."""
    import time
    from campuspulse_recovery.integrations import google_clients
    from campuspulse_recovery.domain.ports import BindingConflict
    spec = json.loads(Path(spec_path).read_text(encoding="utf8"))
    mp = pytest.MonkeyPatch()
    install_network_guard(mp, loopback=True)
    cfg = AppConfig.model_validate(spec["config"])
    async def scenario():
        documents, client = emulator_documents(cfg)
        try:
            h = Harness(mp)
            h.recompose(documents, cfg)
            h.now = __import__("datetime").datetime.fromisoformat(spec["now"])
            h.plan = PlanVersion.model_validate(spec["plan"])
            h.request_input = ApplyPlanInput.model_validate(spec["request"])
            h.rows = spec["rows"]
            h.bootstrapping = False
            h.reader.clients = h.clients
            api = ProxyCalendar(spec["calendar_endpoint"])
            mp.setattr(google_clients, "_RequestsHttp", lambda *a, **k: api)
            mode = spec.get("mode", "apply")
            if mode == "crash_gap":
                original_record = h.executions.record_operation
                async def crash(*args, **kwargs):
                    if kwargs["operation"].status == "completed":
                        save_process_result(output, stage="calendar_success_before_ledger", execution=(await h.executions.get(args[0], h.request_input.request_key)).model_dump(mode="json"))
                        os._exit(73)
                    return await original_record(*args, **kwargs)
                h.executions.record_operation = crash
            elif mode == "pause_dispatch":
                original_write = h.executor._write
                async def paused(*args, **kwargs):
                    Path(spec["paused"]).touch()
                    deadline = time.monotonic() + 25
                    while not Path(spec["resume"]).exists():
                        assert time.monotonic() < deadline, "stale worker gate timeout"
                        await asyncio.sleep(.02)
                    return await original_write(*args, **kwargs)
                h.executor._write = paused
            elif mode == "partial":
                original_check = h.service.check
                calls = 0
                async def stop_second(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        from campuspulse_recovery.domain.ports import PortError
                        raise PortError("synthetic interruption before second operation")
                    return await original_check(*args, **kwargs)
                h.service.check = stop_second
            until = time.monotonic() + 20
            while not Path(gate).exists():
                assert time.monotonic() < until, "worker start gate timeout"
                await asyncio.sleep(.02)
            result = await h.apply()
            save_process_result(output, result=result.model_dump(mode="json"),
                origin=__import__("campuspulse_recovery.application.apply", fromlist=["x"]).__file__)
        finally:
            client.close()
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(scenario())
    finally:
        mp.undo()
