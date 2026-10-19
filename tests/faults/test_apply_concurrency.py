"""T27 emulator integration: independent processes, actual durable adapter.

Google HTTP, identities, consent and stopped-writer evidence remain synthetic.
No Python shared lock stands in for Firestore coordination.
"""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from uuid import uuid4

import pytest

from campuspulse_recovery.domain.ports import BindingConflict
from campuspulse_recovery.storage.leases import _Claim, _claim_path
from campuspulse_recovery.storage.plans import _decode
from tests.faults.support import Harness, emulator_documents, install_network_guard, synthetic_config


@pytest.fixture(autouse=True)
def literal_loopback_only(monkeypatch):
    install_network_guard(monkeypatch, loopback=True)


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coro)


def wait_file(path, seconds=20):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"missing worker evidence: {path}"
        time.sleep(.02)


@contextmanager
def calendar_server(api, *, block_write=False):
    entered, release = Event(), Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            response, body = api.request(**payload)
            if block_write and payload["method"] != "GET":
                entered.set()
                assert release.wait(15), "test must release fake Calendar response"
            encoded = json.dumps({"status": response.status, "body": body.decode()}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", entered, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def start_worker(tmp_path, h, endpoint, name, *, mode="apply", **extra):
    spec = dict(config=h.config.model_dump(mode="json"), now=h.now.isoformat(),
        plan=h.plan.model_dump(mode="json"), request=h.request_input.model_dump(mode="json"),
        rows=h.rows, calendar_endpoint=endpoint, mode=mode, **extra)
    spec_path, output, gate = tmp_path / (name + "-spec.json"), tmp_path / (name + "-result.json"), tmp_path / "go"
    spec_path.write_text(json.dumps(spec), encoding="utf8")
    entry = tmp_path / (name + "-entry.py")

    installed = str(Path(__import__("campuspulse_recovery").__file__).parents[1])
    qa = str(Path(__file__).parents[2])
    dependencies = [p for p in sys.path if p.replace('\\', '/').endswith('/site-packages')]
    guard = '''
from importlib.abc import MetaPathFinder
class NoCloudImports(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'vertexai' or fullname.startswith(('vertexai.', 'google.cloud.aiplatform', 'google.adk', 'google.genai')):
            raise AssertionError('T27 forbids cloud/model import: ' + fullname)
sys.meta_path.insert(0, NoCloudImports())
'''
    entry.write_text("import sys\nsys.dont_write_bytecode=True\nsys.path[:0]=" + repr([installed, qa, *dependencies]) +
        guard + "\nfrom tests.faults.support import process_worker\nprocess_worker(*sys.argv[1:])\n", encoding="utf8")
    proc = subprocess.Popen([sys.executable, "-I", str(entry), str(spec_path), str(output), str(gate)],
        stdout=(tmp_path / (name + "-stdout.log")).open("w", encoding="utf8"),
        stderr=(tmp_path / (name + "-stderr.log")).open("w", encoding="utf8"))
    gate.touch()
    return proc, output


def finish(proc, output, *, expected=0):
    assert proc.wait(timeout=35) == expected, output.with_name(output.name.replace("-result.json", "-stderr.log")).read_text(encoding="utf8")
    return json.loads(output.read_text(encoding="utf8"))


async def setup(monkeypatch, *, two=False):
    cfg = synthetic_config(uuid4().hex)
    documents, client = emulator_documents(cfg)
    h = Harness(monkeypatch)
    h.recompose(documents, cfg)
    await h.prepare(two=two)
    h.reader.clients = h.clients
    return h, client


def test_emulator_two_process_apply_one_active_executor(monkeypatch, tmp_path):
    async def scenario():
        h, client = await setup(monkeypatch)
        procs = []
        try:
            with calendar_server(h.api, block_write=True) as (endpoint, entered, release):
                first, a = start_worker(tmp_path, h, endpoint, "first")
                procs.append(first)
                assert entered.wait(20), "first executor must reach actual fake Calendar write"
                second, b = start_worker(tmp_path, h, endpoint, "second")
                procs.append(second)
                blocked = finish(second, b)
                assert blocked["result"]["execution"]["operations"][0]["status"] == "in_flight"
                assert "apply_interrupted_recovery_required" in {f["code"] for f in blocked["result"]["findings"]}
                assert len(h.api.mutations) == 1
                release.set()
                success = finish(first, a)
                assert success["pid"] != blocked["pid"] != os.getpid()
                assert success["result"]["execution"]["status"] == "applied_unverified"
                with h.scope() as ctx:
                    ledger = await h.executions.get_ledger(ctx, "apply-key")
                    assert len(ledger.attempts) == 1 and ledger.execution.operations[0].attempts == 1
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(5)
            client.close()
    run(scenario())


def test_emulator_expiry_live_stale_worker_cannot_dispatch_or_clear_unknown(monkeypatch, tmp_path):
    async def scenario():
        h, client = await setup(monkeypatch)
        processes = []
        try:
            with calendar_server(h.api) as (endpoint, _, release):
                paused, resume = tmp_path / "paused", tmp_path / "resume"
                old, a = start_worker(tmp_path, h, endpoint, "old", mode="pause_dispatch", paused=str(paused), resume=str(resume))
                processes.append(old)
                wait_file(paused)
                assert old.poll() is None and not h.api.mutations
                claim = _decode(await h.documents.get(_claim_path(h.config.student.calendar_id)), "calendar_claim", _Claim)
                h.now = claim.lease.expires_at + timedelta(microseconds=1)
                new, b = start_worker(tmp_path, h, endpoint, "takeover")
                processes.append(new)
                recovered = finish(new, b)
                assert recovered["result"]["execution"]["status"] == "unknown"
                assert not h.api.mutations
                current = _decode(await h.documents.get(_claim_path(h.config.student.calendar_id)), "calendar_claim", _Claim)
                assert current.lease.generation == claim.lease.generation + 1
                assert current.previous == claim.lease and current.writer_uncertain
                resume.touch()
                stale = finish(old, a)
                assert stale["pid"] != recovered["pid"]
                assert stale["result"]["execution"]["status"] == "unknown"
                assert not h.api.mutations
                with h.scope() as ctx:
                    state = await h.executions.get(ctx, "apply-key")
                    assert state.operations[0].status == "unknown" and state.operations[0].attempts == 1
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(5)
            client.close()
    run(scenario())


@pytest.mark.parametrize("mode", ["partial", "crash_gap"])
def test_emulator_process_restart_preserves_effect_and_attempt_lineage(monkeypatch, tmp_path, mode):
    async def scenario():
        h, client = await setup(monkeypatch, two=True)
        processes = []
        try:
            with calendar_server(h.api) as (endpoint, _, release):
                original, a = start_worker(tmp_path, h, endpoint, "original", mode=mode)
                processes.append(original)
                before = finish(original, a, expected=73 if mode == "crash_gap" else 0)
                assert len(h.api.mutations) == 1
                if mode == "crash_gap":
                    assert before["stage"] == "calendar_success_before_ledger"
                    assert before["execution"]["operations"][0]["status"] == "in_flight"
                    claim = _decode(await h.documents.get(_claim_path(h.config.student.calendar_id)), "calendar_claim", _Claim)
                    h.now = claim.lease.expires_at + timedelta(microseconds=1)
                else:
                    assert before["result"]["execution"]["status"] == "partial"
                restarted, b = start_worker(tmp_path, h, endpoint, "restart")
                processes.append(restarted)
                after = finish(restarted, b)
                assert before["pid"] != after["pid"]
                states = after["result"]["execution"]["operations"]
                assert states[0]["status"] == "completed" and states[0]["attempts"] == 1
                if mode == "crash_gap":
                    assert states[0]["reconciled_at"] is not None
                    assert after["result"]["execution"]["status"] == "partial"
                    assert states[1]["status"] == "pending" and len(h.api.mutations) == 1

                    claim = _decode(await h.documents.get(_claim_path(h.config.student.calendar_id)), "calendar_claim", _Claim)
                    from campuspulse_recovery.storage.leases import WriterStoppedEvidence
                    from campuspulse_recovery.storage.plans import _encode
                    previous = claim.previous
                    evidence = WriterStoppedEvidence(holder_id=previous.holder_id, generation=previous.generation,
                        stopped_at=h.now, evidence_reference="t27-synthetic-stopped-process-no-outstanding-http")
                    async def attest(tx):
                        tx.set("test_stops/" + str(previous.generation), _encode("test_stop", evidence))
                    await h.documents.transact(attest)


                    h.now = claim.lease.expires_at + timedelta(microseconds=1)
                    continuation, c = start_worker(tmp_path, h, endpoint, "stopped-continuation")
                    processes.append(continuation)
                    after = finish(continuation, c)
                assert after["result"]["execution"]["status"] == "applied_unverified", after
                assert [s["attempts"] for s in after["result"]["execution"]["operations"]] == [1, 1]
                assert len(h.api.mutations) == 2
                assert len({c[3]["id"] for c in h.api.mutations}) == 2
                with h.scope() as ctx:
                    ledger = await h.executions.get_ledger(ctx, "apply-key")
                    assert len(ledger.attempts) == 2
                    assert {a.ordinal for a in ledger.attempts} == {1}
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(5)
            client.close()
    run(scenario())
