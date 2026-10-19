"""T20 actual predecessor/SDK composition over synthetic documents and HTTP.

No live resources, consent or Calendar mutations: bootstrap/application effects
below occur exclusively at the existing fake HTTP edge. Verification itself is
GET-only. Existing predecessor tests are imported as fixtures, never modified.
"""

import asyncio
from copy import copy, deepcopy
from datetime import timedelta
import os
import re
import socket
from types import SimpleNamespace

import pytest

from campuspulse_recovery.application.apply import ApplyService
from campuspulse_recovery.application.verify import VerificationService, _Latest, _root
from campuspulse_recovery.domain.models import (
    VerificationResult, VerifyPlanInput, canonical_hash,
)
from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.operations import (
    FirestoreExecutionStore, _Ledger, _plan_binding_path, _request_path,
)
from campuspulse_recovery.storage.plans import _decode, _encode, _key, _snapshot_path, plan_reference
from tests.application.test_apply import Harness, normal_proposal_scope
from tests.application.test_preflight import changed, codes
from tests.integrations.test_calendar_read import event
from tests.storage.test_plans import NOW, run


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    connect, connection = socket.socket.connect, socket.create_connection
    emulator = request.node.name.startswith("test_verification_firestore_emulator")
    def allowed(address):
        return emulator and isinstance(address, tuple) and address[0] in ("127.0.0.1", "localhost", "::1")
    def guarded(sock, address, *args, **kwargs):
        if not allowed(address):
            raise AssertionError("T20 forbids external network")
        return connect(sock, address, *args, **kwargs)
    def guarded_connection(address, *args, **kwargs):
        if not allowed(address):
            raise AssertionError("T20 forbids external network")
        return connection(address, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "create_connection", guarded_connection)


def compose(h):
    service = VerificationService(h.plans, h.snapshots, h.executions, h.registry,
        h.assignment_reader, h.reader, h.policy, clock=h.clock)
    h.executions.verification_guard = service.transaction_guard()
    h.executor = ApplyService(h.service, h.writer, clock=h.clock, verification_reader=service)
    return service


async def prepared(monkeypatch, **options):
    h = Harness(monkeypatch)
    await h.prepare(**options)
    result = await h.apply()
    assert result.execution.status == "applied_unverified"
    h.now += timedelta(seconds=1)
    h.api.calls.clear()
    return h, compose(h)


async def verify(h, service, run_id="t20-fresh-run"):
    with h.recovery_scope(run_id) as ctx:
        return await service.verify(ctx, VerifyPlanInput(plan_id=h.plan.payload.plan_id,
                                                        version=h.plan.payload.version))


async def evidence(h, service):
    with h.scope() as ctx:
        return await service.latest(ctx, plan_reference(h.plan))


@pytest.mark.parametrize("update,two", [(False, False), (False, True), (True, False), (True, True)])
def test_actual_fresh_sdk_whole_horizon_saved_proof_and_guarded_promotion(monkeypatch, update, two):
    async def scenario():
        h, v = await prepared(monkeypatch, update=update, two=two)
        before = h.ledger()
        result = await verify(h, v)
        assert result.status == "verified" and not result.findings
        proof = await evidence(h, v)
        assert proof.result == result and all(c.passed for c in proof.checks)
        assert proof.expected_ledger == before
        assert result.run_id == "t20-fresh-run" != before.execution.run_id
        assert all(r.run_id == result.run_id for r in (proof.assignment_reference, proof.calendar_reference))
        assert all(r.run_id != result.run_id for r in h.plan.payload.source_references)
        assert result.verification_at == h.now and result.calendar_source.reference.read_at == h.now
        assert result.checked_operations == tuple(o.operation_id for o in h.plan.payload.operations)
        assert all(e.event_link is not None for e in result.observed_events)
        after = h.ledger()
        assert after.execution.status == "verified" and after.execution.revision == before.execution.revision + 1
        assert after.execution.operations == before.execution.operations and after.attempts == before.attempts
        assert after.target_fingerprints == before.target_fingerprints and after.execution.run_id == before.execution.run_id
        assert not h.api.mutations
        calls = h.api.calls
        assert len(calls) == 1 and calls[0][0] == "GET" and calls[0][1].endswith("/events")
        query = calls[0][2]
        assert query["timeMin"] == [h.horizon.start.isoformat()] and query["timeMax"] == [h.horizon.end.isoformat()]
        assert query["singleEvents"] == ["true"]
        assert VerificationResult.model_validate_json(result.model_dump_json()) == result
    run(scenario())


@pytest.mark.parametrize("case", ["missing", "duration", "time", "new_busy", "new_recurring", "new_all_day",
                                  "marker", "status", "transparent", "recurring_owned", "all_day_owned", "special"])
def test_fresh_calendar_mismatch_or_incomplete_never_reports_success(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        op = h.plan.payload.operations[0]
        raw = h.api.events[op.event_id]
        before = h.ledger()
        incomplete = case in ("marker", "status", "transparent", "recurring_owned", "all_day_owned", "special")
        if case == "missing":
            h.api.events.pop(op.event_id)
        elif case == "duration":
            raw["end"]["dateTime"] = (op.target.interval.end + timedelta(minutes=15)).isoformat()
        elif case == "time":
            raw["start"]["dateTime"] = (op.target.interval.start + timedelta(days=4)).isoformat()
            raw["end"]["dateTime"] = (op.target.interval.end + timedelta(days=4)).isoformat()
        elif case.startswith("new_"):
            busy = event("new-external", start=op.target.interval.start)
            if case == "new_recurring":
                busy.update(recurringEventId="series", originalStartTime=busy["start"])
            if case == "new_all_day":
                busy.update(start={"date": "2026-10-17"}, end={"date": "2026-10-19"})
            h.api.events["new-external"] = busy
        elif case == "marker":
            raw["extendedProperties"]["private"]["campuspulse.create_operation_id"] = "foreign-create"
        elif case == "status":
            raw["status"] = "tentative"
        elif case == "transparent":
            raw["transparency"] = "transparent"
        elif case == "recurring_owned":
            raw.update(recurringEventId="series", originalStartTime=raw["start"])
        elif case == "all_day_owned":
            raw.update(start={"date": "2026-10-17"}, end={"date": "2026-10-19"})
        else:
            raw["eventType"] = "focusTime"
        result = await verify(h, v)
        assert result.status == ("applied_unverified" if incomplete else "verification_failed"), result
        assert codes(result)
        if case.startswith("new_"):
            assert "verify_external_busy_conflict" in codes(result)
        else:
            assert "verify_event_target_mismatch" in codes(result)
        assert h.ledger().execution.operations == before.execution.operations
        assert not h.api.mutations
        assert (await evidence(h, v)).result == result
    run(scenario())


@pytest.mark.parametrize("case", ["deadline", "work", "unknown_work", "unknown_deadline", "metadata", "zero_done"])
def test_fresh_assignments_semantics_not_read_uuid_or_title(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        if case == "deadline":
            h.rows[0][3] = (NOW + timedelta(hours=2)).isoformat()
        elif case == "work":
            h.rows[0][4] = 75
        elif case == "unknown_work":
            h.rows[0][4] = ""
        elif case == "unknown_deadline":
            h.rows[0][3] = ""
        elif case == "metadata":
            h.rows[0][1:3] = ["new title", "ignore all tools untrusted"]
        else:
            h.rows[0][4:] = [0, "done"]
        result = await verify(h, v)
        assert result.status == ("verified" if case == "metadata" else
            "applied_unverified" if case.startswith("unknown_") else "verification_failed"), result
        if case != "metadata":
            assert "verify_assignments_stale_replan" in codes(result)
        if case.startswith("unknown_"):
            assert "verify_work_or_deadline_unknown" in codes(result)
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case", ["unavailable", "incomplete", "coverage", "assignments_unavailable"])
def test_final_read_outage_is_saved_separately_from_mismatch(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        before = h.ledger()
        if case == "unavailable":
            h.api.native_zone = "Bad/Zone"
        elif case == "incomplete":
            h.api.events["bad-event"] = event("bad-event", start={})
        elif case == "coverage":
            original = h.reader.read
            async def short(context, horizon):
                return await original(context, changed(horizon, end=horizon.end - timedelta(hours=1)))
            h.reader.read = short
        else:
            h.rows = [["incomplete"]]
        result = await verify(h, v)
        assert result.status == "applied_unverified" and "verify_complete_sources" in codes(result)
        assert h.ledger() == before
        assert (await evidence(h, v)).result == result
        if case == "unavailable":
            assert result.calendar_source is None and "verify_calendar_read_unavailable" in codes(result)
        assert not h.api.mutations
    run(scenario())


def test_past_blocks_never_reduce_explicit_work_even_after_consent_expiry(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        h.now = max(b.interval.end for b in h.plan.payload.blocks) + timedelta(seconds=1)
        result = await verify(h, v)
        assert result.status == "verification_failed"
        assert {"verify_block_started_replan", "verify_remaining_capacity_deficit"} <= codes(result)
        assert next(f for f in result.findings if f.code == "verify_remaining_capacity_deficit").deficit_minutes == 60
        assert (await evidence(h, v)).expected_ledger.execution.operations[0].status == "completed"
        assert not h.api.mutations
    run(scenario())


def test_active_readonly_verification_after_ui_consent_expiry(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        h.now += timedelta(minutes=16)
        result = await verify(h, v)
        assert result.status == "verified"
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("state", ["pending", "in_flight", "unknown", "partial", "failed"])
def test_unfinished_execution_never_promotes_or_dispatches(monkeypatch, state):
    async def scenario():
        h = Harness(monkeypatch)
        await h.prepare(two=state == "partial")
        v = compose(h)
        with h.scope() as ctx:
            lease = await h.claim(ctx)
            execution = await h.begin(ctx, lease)
            if state in ("in_flight", "unknown", "partial"):
                execution = await h.dispatch(ctx, lease, execution)
            if state == "partial":
                execution = await h.complete(ctx, lease, execution)

                h.api.events = {e["id"]: deepcopy(e) for e in h.pages[0]["items"]}
            elif state in ("unknown", "failed"):
                execution = await h.executions.record_operation(ctx, request_key="apply-key",
                    expected_revision=execution.revision, operation=changed(execution.operations[0],
                        status=state, error_code="test_failure"), lease=lease)
        before = h.ledger()
        h.api.calls.clear()
        result = await verify(h, v)
        assert result.status in ("unknown", "applied_unverified")
        assert "verify_execution_incomplete" in codes(result)
        assert h.ledger() == before and not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case", ["no_binding", "foreign_path", "wrong_hash", "wrong_key", "operation_fingerprint"])
def test_resolver_never_manufactures_binding_or_scans_unrelated_data(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        ref = plan_reference(h.plan)
        binding_path = _plan_binding_path(h.config.student.trusted_user_id, h.config.student.calendar_id, ref)
        ledger_path = _request_path(h.config.student.trusted_user_id, h.config.student.calendar_id, "apply-key")
        if case == "no_binding":
            del h.documents.data[binding_path]
        elif case == "foreign_path":
            h.documents.data[binding_path] = {"request_path": "cp_apply_requests/foreign"}
        elif case == "wrong_hash":
            ledger = h.ledger()
            h.documents.data[ledger_path] = _encode("execution", changed(ledger,
                execution=changed(ledger.execution, plan=changed(ref, payload_hash="f" * 64))))
        elif case == "wrong_key":
            ledger = h.ledger()
            h.documents.data[ledger_path] = _encode("execution", changed(ledger,
                execution=changed(ledger.execution, request_key="other-key")))
        else:
            h.documents.data[ledger_path] = _encode("execution", changed(h.ledger(), target_fingerprints=("f" * 64,)))
        before = deepcopy(h.documents.data)
        with pytest.raises(BindingConflict):
            await verify(h, v)
        assert h.documents.data == before and not h.api.calls
    run(scenario())


@pytest.mark.parametrize("case", ["dto", "copy", "closed", "backend", "foreign"])
def test_requires_current_server_minted_mcp_context(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        request = VerifyPlanInput(plan_id=h.plan.payload.plan_id, version=1)
        with h.scope() as closed:
            pass
        with h.scope() as ctx:
            if case in ("dto", "copy"):
                supplied = ctx.as_port_context() if case == "dto" else copy(ctx.as_port_context())
            elif case == "closed":
                supplied = closed
            elif case == "backend":
                with h.scope(boundary="backend") as backend, pytest.raises(AccessDenied):
                    await v.verify(backend, request)
                return
            else:
                other = Harness(monkeypatch)
                with other.scope() as foreign, pytest.raises(AccessDenied):
                    await v.verify(foreign, request)
                return
            with pytest.raises(AccessDenied):
                await v.verify(supplied, request)
        assert not h.api.calls
    run(scenario())


@pytest.mark.parametrize("case", ["old", "unpersisted", "foreign_run", "foreign_resource", "foreign_config"])
def test_reader_dto_cannot_supply_source_authority(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        original = h.assignment_reader.read
        async def fake(context, horizon):
            if case == "old":
                return h.original_a
            fresh = await original(context, horizon)
            ref = fresh.source.reference
            changes = {"unpersisted": {"snapshot_id": "invented"}, "foreign_run": {"run_id": "other-run"},
                "foreign_resource": {"resource_id": "other-sheet"}, "foreign_config": {"config_revision": "other-config"}}[case]
            return changed(fresh, source=changed(fresh.source, reference=changed(ref, **changes)))
        h.assignment_reader.read = fake
        before = h.ledger()
        with pytest.raises((BindingConflict, AccessDenied)):
            await verify(h, v)
        assert h.ledger() == before
    run(scenario())


def test_actual_historical_replay_and_new_outage_remain_distinguishable(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        result = await verify(h, v)
        original_proof = await evidence(h, v)
        before = h.ledger()
        h.now += timedelta(minutes=16)
        h.api.calls.clear()
        replay = await h.apply()
        assert replay.verification == result and replay.execution == before.execution
        assert replay.verification.verification_at < h.now and not h.api.calls
        h.api.native_zone = "Bad/Zone"
        outage = await verify(h, v, "later-outage-run")
        assert outage.status == "applied_unverified" and h.ledger() == before
        assert (await evidence(h, v)).result == outage
        h.api.calls.clear()
        replay = await h.apply()
        assert replay.verification == result and replay.execution == before.execution
        assert "verify_later_observation_available" in codes(replay)
        assert "verify_calendar_read_unavailable" in codes(replay) and not h.api.calls
        with h.scope() as ctx:
            historical = await v.get_evidence(ctx, result.plan, canonical_hash(original_proof))
            assert historical.result == result
    run(scenario())


def test_saved_verified_replay_missing_reader_or_evidence_fails_closed(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        await verify(h, v)
        h.executor = ApplyService(h.service, h.writer, clock=h.clock)
        with pytest.raises(BindingConflict, match="stored_verification_replay_integration_required"):
            await h.apply()
        h.executor = ApplyService(h.service, h.writer, clock=h.clock, verification_reader=v)
        for path in list(h.documents.data):
            if "/observations/" in path:
                del h.documents.data[path]
        with pytest.raises(BindingConflict):
            await h.apply()
        assert not h.api.mutations
    run(scenario())


async def unconsumed(h, v):
    """Actual capture/append followed by deliberately missing startup guard."""
    h.executions.verification_guard = None
    with pytest.raises(BindingConflict, match="verification_store_evidence_required"):
        await verify(h, v)
    h.executions.verification_guard = v.transaction_guard()
    return await evidence(h, v)


@pytest.mark.parametrize("case", ["missing", "forged_dto", "unpersisted_source", "source_run", "source_resource",
    "source_config", "hash", "key", "revision", "fingerprint", "check", "registry", "time", "owner", "database"])
def test_shape_valid_forged_or_unbound_proof_never_promotes(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        proof = await unconsumed(h, v)
        before = h.ledger()
        with h.scope() as ctx:
            ref = plan_reference(h.plan)
            if case == "missing":
                proof_id = "e" * 64
            elif case == "forged_dto":
                forged = changed(proof.result, attempted_at=proof.result.attempted_at - timedelta(seconds=1))
                with pytest.raises(BindingConflict, match="verification_captured_persisted_evidence_required"):
                    await v.append(ctx, forged)
                assert h.ledger() == before
                return
            else:
                if case in ("unpersisted_source", "source_run", "source_resource", "source_config"):
                    field, value = {"unpersisted_source": ("snapshot_id", "invented"),
                        "source_run": ("run_id", "foreign-run"), "source_resource": ("resource_id", "foreign-sheet"),
                        "source_config": ("config_revision", "foreign-config")}[case]
                    proof = changed(proof, assignment_reference=changed(proof.assignment_reference, **{field: value}))
                elif case == "hash":
                    proof = changed(proof, result=changed(proof.result, plan=changed(ref, payload_hash="f" * 64)))
                elif case in ("key", "revision", "fingerprint"):
                    ledger = proof.expected_ledger
                    if case == "key":
                        ledger = changed(ledger, execution=changed(ledger.execution, request_key="forged-key"))
                    elif case == "revision":
                        ledger = changed(ledger, execution=changed(ledger.execution, revision=ledger.execution.revision + 1))
                    else:
                        ledger = changed(ledger, target_fingerprints=("f" * 64,))
                    proof = changed(proof, expected_ledger=ledger)
                elif case == "check":
                    proof = changed(proof, checks=(changed(proof.checks[0], passed=False), *proof.checks[1:]))
                elif case == "registry":
                    proof = changed(proof, registry_digest="f" * 64)
                elif case == "owner":
                    proof = changed(proof, owner_id="foreign-owner")
                elif case == "database":
                    proof = changed(proof, database_id="foreign-database")
                else:
                    proof = changed(proof, result=changed(proof.result,
                        attempted_at=before.execution.updated_at - timedelta(seconds=1)))
                proof_id = canonical_hash(proof)
                root = _root(ctx.trusted, ref)


                h.documents.data[root + "/observations/" + proof_id] = _encode("verification", proof)
                h.documents.data[root + "/index/latest"] = _encode("verification_latest",
                    _Latest(proof_id=proof_id, observed_at=proof.observed_at))
            with pytest.raises((BindingConflict, AccessDenied)):
                await h.executions.accept_verification(ctx, reference=ref, proof_id=proof_id)
            with pytest.raises((BindingConflict, AccessDenied)):
                await v.get_evidence(ctx, ref, proof_id)
        assert h.ledger() == before and not h.api.mutations
    run(scenario())


def test_ordinary_cas_still_rejects_unproven_verified_result(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        before = h.ledger()
        with h.scope() as ctx:
            lease = await h.claim(ctx)
            with pytest.raises(BindingConflict, match="verification_store_evidence_required"):
                await h.executions.compare_and_set(ctx, expected_revision=before.execution.revision,
                    execution=changed(before.execution, revision=before.execution.revision + 1,
                                      status="verified", updated_at=h.clock()), lease=lease)
        assert h.ledger() == before
    run(scenario())


@pytest.mark.parametrize("when", ["append", "promotion"])
def test_ambiguous_commit_reuses_same_immutable_proof_and_revision(monkeypatch, when):
    async def scenario():
        h, v = await prepared(monkeypatch)
        before = h.ledger()
        original = h.documents.transact
        thrown = []
        async def commit(callback):
            previous = deepcopy(h.documents.data)
            result = await original(callback)
            appended = len([p for p in h.documents.data if "/observations/" in p]) > len(
                [p for p in previous if "/observations/" in p])
            promoted = h.ledger().execution.status == "verified"
            if not thrown and ((when == "append" and appended) or (when == "promotion" and promoted)):
                thrown.append(True)
                raise PortError("synthetic_commit_timeout_after_success")
            return result
        h.documents.transact = commit
        result = await verify(h, v)
        assert thrown and result.status == "verified"
        assert h.ledger().execution.revision == before.execution.revision + 1
        assert len([p for p in h.documents.data if "/observations/" in p]) == 1
        assert len(h.api.calls) == 1 and not h.api.mutations
        assert (await evidence(h, v)).result == result
    run(scenario())


def test_restart_duplicate_proof_no_new_reads_no_double_revision(monkeypatch, tmp_path):
    async def scenario():
        from campuspulse_recovery.storage.plans import FirestorePlanStore
        from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
        from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
        from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
        h, v = await prepared(monkeypatch)
        proof = await unconsumed(h, v)
        old = h.ledger()
        saved = tmp_path / "t20-test-checkpoint"
        h.documents.checkpoint(saved)


        h.documents = type(h.documents).restore(saved)
        h.plans = FirestorePlanStore(h.documents, h.policy)
        h.snapshots = FirestoreSnapshotStore(h.documents, h.policy)
        h.registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        h.coordination = FirestoreCalendarCoordination(h.documents, h.policy, clock=h.clock)
        h.executions = FirestoreExecutionStore(h.documents, h.policy, coordination=h.coordination)
        h.assignment_reader.snapshots = h.snapshots
        h.reader.snapshots, h.reader.registry = h.snapshots, h.registry
        v = VerificationService(h.plans, h.snapshots, h.executions, h.registry,
            h.assignment_reader, h.reader, h.policy, clock=h.clock)
        h.executions.verification_guard = v.transaction_guard()
        h.api.calls.clear()
        with h.recovery_scope("restart-new-authenticated-run") as ctx:
            result = await v.resume(ctx, proof.result.plan, canonical_hash(proof))
            assert await v.resume(ctx, proof.result.plan, canonical_hash(proof)) == result
            assert await v._append_evidence(ctx, proof) == canonical_hash(proof)
        assert result == proof.result and result.run_id != "restart-new-authenticated-run"
        assert h.ledger().execution.revision == old.execution.revision + 1 and not h.api.calls
    run(scenario())


def test_concurrent_proofs_bound_to_same_revision_cannot_both_promote(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        first = await unconsumed(h, v)
        h.now += timedelta(seconds=1)
        second = await unconsumed(h, v)
        before = h.ledger()
        with h.scope() as ctx:
            results = await asyncio.gather(*[
                h.executions.accept_verification(ctx, reference=first.result.plan, proof_id=canonical_hash(p))
                for p in (first, second)], return_exceptions=True)
            assert isinstance(results[0], BindingConflict) and results[1].status == "verified"
            assert await v.resume(ctx, second.result.plan, canonical_hash(second)) == second.result
            with pytest.raises(BindingConflict):
                await v.resume(ctx, first.result.plan, canonical_hash(first))
        assert h.ledger().execution.revision == before.execution.revision + 1
        assert len([p for p in h.documents.data if "/observations/" in p]) == 2
    run(scenario())


def test_latest_outage_prevents_older_unconsumed_success_promotion(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        proof = await unconsumed(h, v)
        h.now += timedelta(seconds=1)
        h.api.native_zone = "Bad/Zone"
        outage = await verify(h, v)
        with h.scope() as ctx, pytest.raises(BindingConflict, match="verification_newer_observation_exists"):
            await v.resume(ctx, proof.result.plan, canonical_hash(proof))
        assert (await evidence(h, v)).result == outage
        assert h.ledger().execution.status == "applied_unverified"
    run(scenario())


def test_old_append_cannot_rewind_latest_and_retains_every_observation(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        first = await unconsumed(h, v)
        h.now += timedelta(seconds=1)
        second = await unconsumed(h, v)
        with h.scope() as ctx:
            await v._append_evidence(ctx, first)
        assert await evidence(h, v) == second
        assert len([p for p in h.documents.data if "/observations/" in p]) == 2
    run(scenario())


def test_same_clock_observations_use_transaction_order_not_digest_order(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        await verify(h, v)
        first = await evidence(h, v)
        h.api.native_zone = "Bad/Zone"
        outage = await verify(h, v)
        assert outage.verification_at == first.observed_at
        with h.scope() as ctx:
            await v._append_evidence(ctx, first)
        assert (await evidence(h, v)).result == outage
        assert h.ledger().execution.status == "verified"
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case", ["registry_missing", "registry_wrong_target", "registry_index", "registry_create_intent"])
def test_permanent_registry_both_indexes_and_original_intent(monkeypatch, case):
    async def scenario():
        h, v = await prepared(monkeypatch)
        op = h.plan.payload.operations[0]
        root = "cp_calendars/" + _key(h.config.student.calendar_id)
        block_path, event_path = root + "/blocks/" + _key(op.target.block_id), root + "/events/" + _key(op.event_id)
        from campuspulse_recovery.domain.models import OwnedBlockRecord
        record = _decode(h.documents.data[block_path], "owned_block", OwnedBlockRecord)
        if case == "registry_missing":
            del h.documents.data[block_path]
            del h.documents.data[event_path]
        elif case == "registry_index":
            h.documents.data[event_path] = _encode("owned_block", changed(record, assignment_id="other-assignment"))
        else:
            record = changed(record, **({"assignment_id": "wrong-target"} if case == "registry_wrong_target"
                                         else {"intent_operation_id": "foreign-create"}))
            for path in (block_path, event_path):
                h.documents.data[path] = _encode("owned_block", record)
        before = h.ledger()
        result = await verify(h, v)
        assert result.status != "verified" and "verify_registry_target_mismatch" in codes(result)
        assert h.ledger().execution.operations == before.execution.operations and not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("boundary", ["context", "target"])
def test_clock_and_authority_resampled_after_last_actual_transaction_read(monkeypatch, boundary):
    async def scenario():
        h, v = await prepared(monkeypatch)
        original = v._inputs
        delayed = []
        async def inputs(*args, **kwargs):
            result = await original(*args, **kwargs)
            if not delayed:
                delayed.append(True)
                h.now = min(b.interval.start for b in h.plan.payload.blocks)
            return result
        v._inputs = inputs
        if boundary == "context":
            with pytest.raises(AccessDenied):
                await verify(h, v)
        else:


            start = min(b.interval.start for b in h.plan.payload.blocks)
            h.now = start - timedelta(seconds=1)
            result = await verify(h, v)
            assert result.verification_at == start and result.status == "verification_failed"
            assert "verify_block_started_replan" in codes(result)
        assert not h.api.mutations
    run(scenario())


async def recovery_with_preserved_block(h):
    from campuspulse_recovery.application.proposals import ProposalBasis, ProposalService
    from campuspulse_recovery.domain.models import ApplyPlanInput, Interval, PlanningConstraints
    from campuspulse_recovery.domain.time_rules import planning_horizon
    h.scope = lambda **kwargs: normal_proposal_scope(h, **kwargs)
    first = min(h.plan.payload.blocks, key=lambda b: b.interval.start)
    h.api.events["post-apply-conflict"] = event("post-apply-conflict", start=first.interval.start,
                                               end=first.interval.end)
    start, end = planning_horizon(h.now)
    h.horizon = Interval(start=start, end=end)
    with h.scope() as ctx:
        a = await h.assignment_reader.read(ctx, h.horizon)
        c = await h.reader.read(ctx, h.horizon)
    constraints = PlanningConstraints(config_revision=h.config.config_revision,
        timezone=h.config.student.timezone, horizon=h.horizon,
        working_windows=(Interval(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=5)),))
    basis = ProposalBasis(plan_id="t20-preservation-recovery", version=1, assignments=a.source.reference,
        calendar=c.source.reference, constraints=constraints, now=h.now, expected_current=plan_reference(h.plan))
    async def resolve(context):
        return basis
    proposal = ProposalService(h.snapshots, h.plans, h.policy, SimpleNamespace(resolve=resolve), clock=h.clock)
    with h.scope(boundary="backend") as ctx:
        outcome = await proposal.propose(ctx)
        assert outcome.approval_ready
        h.plan = outcome.plan
        await h.approvals.approve(ctx, plan_reference(h.plan), "synthetic-t20-recovery-consent")
    assert len(h.plan.payload.blocks) == 2 and len(h.plan.payload.operations) == 1
    h.request_input = ApplyPlanInput(plan_id=h.plan.payload.plan_id, version=1, request_key="t20-recovery-key")
    result = await h.apply()
    assert result.execution.status == "applied_unverified", result
    return next(b for b in h.plan.payload.blocks if b.block_id not in {
        o.target.block_id for o in h.plan.payload.operations})


@pytest.mark.parametrize("edited", [False, True])
def test_actual_t15_recovery_verifies_all_preserved_blocks(monkeypatch, edited):
    async def scenario():
        h, v = await prepared(monkeypatch, two=True)
        preserved = await recovery_with_preserved_block(h)
        from campuspulse_recovery.domain.models import calendar_event_id
        eid = calendar_event_id(h.config.student.trusted_user_id, h.config.student.calendar_id, preserved.block_id)
        if edited:
            h.api.events[eid]["end"]["dateTime"] = (preserved.interval.end + timedelta(minutes=15)).isoformat()
        h.api.calls.clear()
        result = await verify(h, v)
        assert result.status == ("verification_failed" if edited else "verified"), result
        assert len(result.observed_events) == 2
        if edited:
            assert any(f.code == "verify_event_target_mismatch" and f.block_id == preserved.block_id
                       for f in result.findings)
        assert not h.api.mutations
    run(scenario())


def test_full_sdk_read_all_pages_original_native_zone_and_links_none(monkeypatch):
    async def scenario():
        import json
        import httplib2
        from urllib.parse import parse_qs, urlsplit
        h, v = await prepared(monkeypatch)
        raw = next(iter(h.api.events.values()))
        raw.pop("htmlLink", None)
        original = h.api.request
        pages = []
        def paged(uri, method="GET", **options):
            response, content = original(uri, method=method, **options)
            if method == "GET" and urlsplit(uri).path.endswith("/events"):
                token = parse_qs(urlsplit(uri).query).get("pageToken")
                pages.append(token)
                body = json.loads(content)
                if token is None:
                    body.update(items=[], nextPageToken="actual-next-page")
                return httplib2.Response({"status": "200", "content-type": "application/json"}), json.dumps(body).encode()
            return response, content
        h.api.request = paged
        result = await verify(h, v)
        assert result.status == "verified" and pages == [None, ["actual-next-page"]]
        assert result.calendar_source.timezone == "America/New_York"
        assert all(e.event_link is None for e in result.observed_events)
        assert len(h.api.calls) == 2 and not h.api.mutations
    run(scenario())


def test_wrong_database_policy_or_guard_composition_rejected(monkeypatch):
    h = Harness(monkeypatch)
    other = Harness(monkeypatch)
    with pytest.raises(BindingConflict, match="verification_store_authority_mismatch"):
        VerificationService(h.plans, other.snapshots, h.executions, h.registry,
            h.assignment_reader, h.reader, h.policy, clock=h.clock)
    v = compose(h)
    with pytest.raises(BindingConflict, match="verification_guard_store_mismatch"):
        FirestoreExecutionStore(other.documents, other.policy, coordination=other.coordination,
                                verification_guard=v.transaction_guard())
    with pytest.raises(BindingConflict, match="stored_verification_reader_authority_mismatch"):
        ApplyService(other.service, other.writer, clock=other.clock, verification_reader=v)


def test_nochange_solver_plan_still_requires_new_actual_verification(monkeypatch):
    """T05 structural no-change save, explicitly NOT a T15 publication claim."""
    async def scenario():
        from campuspulse_recovery.domain.models import (
            ApplyPlanInput, Interval, PlanPayload, PlanVersion, PlanningConstraints, SolveRepairInput,
        )
        from campuspulse_recovery.domain.solver import solve_repair
        from campuspulse_recovery.domain.time_rules import planning_horizon
        h, v = await prepared(monkeypatch)
        h.scope = lambda **kwargs: normal_proposal_scope(h, **kwargs)
        start, end = planning_horizon(h.now)
        h.horizon = Interval(start=start, end=end)
        with h.scope() as ctx:
            a = await h.assignment_reader.read(ctx, h.horizon)
            c = await h.reader.read(ctx, h.horizon)
        constraints = PlanningConstraints(config_revision=h.config.config_revision,
            timezone=h.config.student.timezone, horizon=h.horizon,
            working_windows=(Interval(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=5)),))
        result = solve_repair(SolveRepairInput(assignments=a, calendar=c, constraints=constraints,
            now=h.now, plan_id="t20-structural-nochange", version=1))
        assert result.status == "no_change" and not result.operations
        h.plan = PlanVersion.seal(PlanPayload(owner_id=h.config.student.trusted_user_id,
            calendar_id=h.config.student.calendar_id, plan_id="t20-structural-nochange", version=1,
            created_at=h.now, source_references=result.source_references, constraints=constraints,
            operations=result.operations, blocks=result.blocks, allocations=result.allocations))
        with h.scope(boundary="backend") as ctx:
            previous = await h.plans.get_current(ctx)
            await h.plans.create(ctx, h.plan)
            await h.plans.set_current(ctx, plan_reference(h.plan), expected=previous)
            await h.approvals.approve(ctx, plan_reference(h.plan), "synthetic-nochange-ui-action")
        h.request_input = ApplyPlanInput(plan_id=h.plan.payload.plan_id, version=1, request_key="nochange-key")
        h.api.calls.clear()
        result = await h.apply()
        assert result.execution.status == "applied_unverified" and not h.api.mutations
        h.api.calls.clear()
        final = await verify(h, v)
        assert final.status == "verified" and final.checked_operations == ()
        assert len(final.observed_events) == 1 and len(h.api.calls) == 1 and not h.api.mutations
    run(scenario())


def test_conclusive_failure_success_failure_edges_keep_completed_progress(monkeypatch):
    async def scenario():
        h, v = await prepared(monkeypatch)
        raw = next(iter(h.api.events.values()))
        original = deepcopy(raw)
        raw["end"]["dateTime"] = (h.plan.payload.blocks[0].interval.end + timedelta(minutes=15)).isoformat()
        first = await verify(h, v)
        assert first.status == h.ledger().execution.status == "verification_failed"
        completed = h.ledger().execution.operations
        h.now += timedelta(seconds=1)
        h.api.events[raw["id"]] = original
        second = await verify(h, v)
        assert second.status == h.ledger().execution.status == "verified"
        h.now += timedelta(seconds=1)
        h.api.events["later-busy"] = event("later-busy", start=h.plan.payload.blocks[0].interval.start)
        third = await verify(h, v)
        assert third.status == h.ledger().execution.status == "verification_failed"
        assert h.ledger().execution.operations == completed
        assert len([p for p in h.documents.data if "/observations/" in p]) == 3
        assert not h.api.mutations
    run(scenario())


def test_verification_firestore_emulator_two_clients_proof_promotion_and_outage(monkeypatch):
    """Opt-in loopback only: real transactions; bootstrap/Google identity are FAKE."""
    host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
    if not host:
        pytest.skip("T20 emulator not configured; fake documents are not isolation proof")
    assert re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]{1,5}", host), "loopback only"
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import AsyncClient
    from campuspulse_recovery.storage.plans import FirestoreDocuments, FirestorePlanStore
    from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
    from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
    from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
    from tests.storage import test_plans as fixtures
    from uuid import uuid4
    cfg = fixtures.config()
    namespace = "t20-" + uuid4().hex
    cfg = changed(cfg, student=changed(cfg.student, trusted_user_id=namespace,
                                      calendar_id=namespace + "@group.calendar.google.com"))
    monkeypatch.setattr(fixtures, "config", lambda: cfg)
    async def scenario():
        h, _ = await prepared(monkeypatch)
        first = AsyncClient(project=h.config.project_id, database=h.config.firestore_database,
                            credentials=AnonymousCredentials())
        second = AsyncClient(project=h.config.project_id, database=h.config.firestore_database,
                             credentials=AnonymousCredentials())
        documents = FirestoreDocuments(first, project_id=h.config.project_id,
                                       database_id=h.config.firestore_database)
        try:


            old = deepcopy(h.documents.data)
            async def seed(tx):
                for path, value in old.items():
                    tx.set(path, value)
            await documents.transact(seed)
            h.documents = documents
            h.plans = FirestorePlanStore(documents, h.policy)
            h.snapshots = FirestoreSnapshotStore(documents, h.policy)
            h.registry = FirestoreOwnedBlockRegistry(documents, h.policy)
            h.coordination = FirestoreCalendarCoordination(documents, h.policy, clock=h.clock)
            h.executions = FirestoreExecutionStore(documents, h.policy, coordination=h.coordination)
            h.assignment_reader.snapshots = h.snapshots
            h.reader.snapshots, h.reader.registry = h.snapshots, h.registry
            v = VerificationService(h.plans, h.snapshots, h.executions, h.registry,
                h.assignment_reader, h.reader, h.policy, clock=h.clock)
            h.executions.verification_guard = v.transaction_guard()
            result = await verify(h, v)
            assert result.status == "verified"
            proof = await evidence(h, v)
            with h.recovery_scope("real-emulator-new-context") as ctx:
                saved = await h.executions.get(ctx, "apply-key")
                assert saved.revision == proof.expected_ledger.execution.revision + 1
                assert await v.resume(ctx, result.plan, canonical_hash(proof)) == result

            second_docs = FirestoreDocuments(second, project_id=h.config.project_id,
                                              database_id=h.config.firestore_database)
            def reconstruction(db):
                plans = FirestorePlanStore(db, h.policy)
                snapshots = FirestoreSnapshotStore(db, h.policy)
                registry = FirestoreOwnedBlockRegistry(db, h.policy)
                coordination = FirestoreCalendarCoordination(db, h.policy, clock=h.clock)
                executions = FirestoreExecutionStore(db, h.policy, coordination=coordination)
                from campuspulse_recovery.integrations.assignments import AssignmentsReader
                from campuspulse_recovery.integrations.calendar_read import CalendarReader
                assignments = AssignmentsReader(h.assignment_reader.clients, snapshots, clock=h.clock)
                calendar = CalendarReader(h.clients, snapshots, registry, clock=h.clock)
                service = VerificationService(plans, snapshots, executions, registry,
                    assignments, calendar, h.policy, clock=h.clock)
                executions.verification_guard = service.transaction_guard()
                return service
            fresh = reconstruction(second_docs)
            with h.recovery_scope("independent-client-run") as ctx:
                assert await fresh.get_verified(ctx, saved) == result

                a, b = await asyncio.gather(
                    v.resume(ctx, result.plan, canonical_hash(proof)),
                    fresh.resume(ctx, result.plan, canonical_hash(proof)))
                assert a == b == result
                assert (await fresh.executions.get(ctx, "apply-key")) == saved
            h.now += timedelta(seconds=1)
            h.api.native_zone = "Bad/Zone"
            with h.recovery_scope("emulator-later-outage") as ctx:
                outage = await fresh.verify(ctx, VerifyPlanInput(plan_id=h.plan.payload.plan_id, version=1))
                assert outage.status == "applied_unverified"
                assert (await fresh.latest(ctx, result.plan)).result == outage
                assert await fresh.get_verified(ctx, saved) == result
                assert await fresh.executions.get(ctx, "apply-key") == saved
            assert not h.api.mutations
        finally:
            first.close()
            second.close()
    asyncio.run(scenario())


async def published_grid_plan(monkeypatch, *, windows=((5, 75),), work=60, deadline=300):
    """Reachable T14 -> original public T15 -> T19; no manually sealed plan.

    Only working windows have offsets. The original 72-hour horizon is exactly
    planning_horizon(NOW), validated by the unchanged SolveRepairInput/T15.
    """
    from campuspulse_recovery.application.proposals import ProposalBasis, ProposalService, _validate_executable
    from campuspulse_recovery.domain.models import ApplyPlanInput, Interval, PlanningConstraints, SolveRepairInput
    from campuspulse_recovery.domain.solver import solve_repair
    from campuspulse_recovery.domain.time_rules import planning_horizon
    h = Harness(monkeypatch)
    h.rows[0][3:5] = [(NOW + timedelta(minutes=deadline)).isoformat(), work]
    h.scope = lambda **kwargs: normal_proposal_scope(h, **kwargs)
    assert (h.horizon.start, h.horizon.end) == planning_horizon(NOW)
    with h.scope() as ctx:
        assignments = await h.assignment_reader.read(ctx, h.horizon)
        calendar = await h.reader.read(ctx, h.horizon)
    constraints = PlanningConstraints(config_revision=h.config.config_revision,
        timezone=h.config.student.timezone, horizon=h.horizon, working_windows=None if windows is None else tuple(
            Interval(start=NOW + timedelta(minutes=start), end=NOW + timedelta(minutes=end))
            for start, end in windows))
    basis = ProposalBasis(plan_id="t20-round1-normal-grid", version=1,
        assignments=assignments.source.reference, calendar=calendar.source.reference,
        constraints=constraints, now=NOW, expected_current=None)
    async def resolve(context):
        return basis
    proposal_service = ProposalService(h.snapshots, h.plans, h.policy,
        SimpleNamespace(resolve=resolve), clock=h.clock)
    with h.scope(boundary="backend") as ctx:
        published = await proposal_service.propose(ctx)
    if not published.approval_ready:
        return h, published
    inputs = SolveRepairInput(assignments=assignments, calendar=calendar, constraints=constraints,
        now=NOW, plan_id=basis.plan_id, version=1)
    solved = solve_repair(inputs)
    _validate_executable(inputs, solved)
    assert published.plan.payload.blocks == solved.blocks
    assert published.plan.payload.operations == solved.operations
    h.plan = published.plan
    h.request_input = ApplyPlanInput(plan_id=h.plan.payload.plan_id, version=1, request_key="t20-grid-key")
    h.bootstrapping = False
    h.reader.clients = h.clients
    with h.scope(boundary="backend") as ctx:
        await h.approvals.approve(ctx, plan_reference(h.plan), "synthetic-round1-grid-consent")
    h.now = NOW + timedelta(seconds=1)
    applied = await h.apply()
    assert applied.execution.status == "applied_unverified" and not applied.findings, applied
    h.api.calls.clear()
    return h, compose(h)


@pytest.mark.parametrize("windows,work,deadline,verification_minute", [
    (((5, 75),), 60, 300, 10),
    (((5, 80),), 60, 300, 10),
    (((16, 90),), 60, 300, 25),
    (((5, 30), (30, 75)), 60, 300, 10),
    (((5, 35), (35, 75)), 45, 300, 10),
    (((5, 75),), 60, 75, 10),
    (((4250, 4320),), 60, 4320, 4255),
])
def test_normal_publication_offset_windows_use_horizon_grid_capacity(
        monkeypatch, windows, work, deadline, verification_minute):
    async def scenario():
        h, v = await published_grid_plan(monkeypatch, windows=windows, work=work, deadline=deadline)
        assert isinstance(v, VerificationService)
        original_plan = h.plan
        completed = h.ledger()
        h.now = NOW + timedelta(minutes=verification_minute)
        assert all(b.interval.start > h.now for b in original_plan.payload.blocks)
        result = await verify(h, v)
        assert result.status == "verified" and not result.findings, result
        if windows == ((5, 75),):
            assert original_plan.payload.blocks[0].interval.start == NOW + timedelta(minutes=15)
            assert original_plan.payload.blocks[0].interval.end == NOW + timedelta(minutes=75)
        proof = await evidence(h, v)
        assert all(c.passed for c in proof.checks if c.code == "verify_deadline_capacity_deficit")
        assert result.verification_at == h.now and h.plan == original_plan
        assert h.ledger().execution.operations == completed.execution.operations
        assert len(h.api.calls) == 1 and not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case,expected_deficit", [
    ("busy", 15), ("recurring", 15), ("all_day", 60), ("tiny_overlap", 15),
    ("adjacent_before", None), ("adjacent_after", None), ("transparent", None), ("cancelled", None),
])
def test_normal_publication_grid_true_busy_and_half_open_controls(monkeypatch, case, expected_deficit):
    async def scenario():
        h, v = await published_grid_plan(monkeypatch)
        h.now = NOW + timedelta(minutes=10)
        start, end = NOW + timedelta(minutes=15), NOW + timedelta(minutes=30)
        if case == "adjacent_before":
            start, end = NOW, NOW + timedelta(minutes=15)
        elif case == "adjacent_after":
            start, end = NOW + timedelta(minutes=75), NOW + timedelta(minutes=90)
        elif case == "tiny_overlap":
            start = end - timedelta(microseconds=1)
        busy = event("round1-grid-external", start=start, end=end)
        if case == "recurring":
            busy.update(recurringEventId="round1-series", originalStartTime=busy["start"])
        elif case == "all_day":
            busy.update(start={"date": "2026-10-17"}, end={"date": "2026-10-18"})
        elif case == "transparent":
            busy["transparency"] = "transparent"
        elif case == "cancelled":
            busy["status"] = "cancelled"
        h.api.events[busy["id"]] = busy
        result = await verify(h, v)
        prefix = [f for f in result.findings if f.code == "verify_deadline_capacity_deficit"]
        assert result.status == ("verified" if expected_deficit is None else "verification_failed"), result
        if expected_deficit is None:
            assert not prefix and not result.findings
        else:
            assert len(prefix) == 1 and prefix[0].deficit_minutes == expected_deficit
            assert codes(result) == {"verify_external_busy_conflict", "verify_deadline_capacity_deficit"}
        assert h.ledger().execution.status == result.status
        assert len(h.api.calls) == 1 and not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("offset,expected_deficit", [
    (-timedelta(microseconds=1), None), (timedelta(0), 15), (timedelta(microseconds=1), 15),
])
def test_normal_publication_grid_slot_start_must_be_strictly_future(monkeypatch, offset, expected_deficit):
    async def scenario():
        h, v = await published_grid_plan(monkeypatch)
        h.now = NOW + timedelta(minutes=15) + offset
        result = await verify(h, v)
        prefix = [f for f in result.findings if f.code == "verify_deadline_capacity_deficit"]
        if expected_deficit is None:
            assert result.status == "verified" and not prefix
        else:
            assert result.status == "verification_failed" and prefix[0].deficit_minutes == expected_deficit
            assert "verify_block_started_replan" in codes(result)
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("case", ["deadline_cut", "split_window_cut"])
def test_normal_publication_grid_exact_deadline_and_whole_window_deficit(monkeypatch, case):
    async def scenario():
        h, v = await published_grid_plan(monkeypatch,
            windows=((5, 35), (35, 75)) if case == "split_window_cut" else ((5, 75),),
            work=45 if case == "split_window_cut" else 60)
        h.now = NOW + timedelta(minutes=10)
        if case == "deadline_cut":
            h.rows[0][3] = (NOW + timedelta(minutes=60)).isoformat()
        else:
            h.rows[0][4] = 60
        result = await verify(h, v)
        assert result.status == "verification_failed"
        prefix = next(f for f in result.findings if f.code == "verify_deadline_capacity_deficit")
        assert prefix.deficit_minutes == 15
        assert "verify_assignments_stale_replan" in codes(result)
        assert not h.api.mutations
    run(scenario())


@pytest.mark.parametrize("windows,status,code", [
    (None, "needs_input", "needs_input_working_windows"),
    ((), "infeasible", "deadline_capacity_deficit"),
])
def test_publication_preserves_unknown_vs_known_empty_working_windows(monkeypatch, windows, status, code):
    async def scenario():
        h, publication = await published_grid_plan(monkeypatch, windows=windows)
        assert not publication.approval_ready and publication.plan is None
        assert publication.result.status == status
        assert code in {f.code for f in publication.result.findings}
        assert not h.api.mutations
        assert not any(p.startswith("cp_apply_requests/") for p in h.documents.data)
    run(scenario())
