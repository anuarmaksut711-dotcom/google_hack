"""T06 fake state-machine tests + opt-in REAL Firestore emulator/process tests.

Fake tests are not distributed proof. Emulator uses explicit demo project,
loopback, AnonymousCredentials and independently spawned Python processes.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from campuspulse_recovery.config import AppConfig
from campuspulse_recovery.domain.models import (
    Approval, BlockTarget, CalendarEvent, ExecutionState, Operation, OperationState,
    OwnershipMarker, PlanPayload, PlanVersion, WorkAllocation, calendar_event_id, operation_id,
)
from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.leases import (
    FirestoreCalendarCoordination, WriterStoppedEvidence, _Claim, _claim_path,
)
from campuspulse_recovery.storage.operations import (
    FirestoreExecutionStore, OutcomeEvidence, _Audit, _Ledger, _request_path, target_fingerprint,
)
from campuspulse_recovery.storage.plans import FirestoreDocuments, _decode, _encode, plan_reference
from tests.storage.test_plans import Harness, NOW, FakeDocuments, config, interval, run, sources


@dataclass
class Clock:
    now: object = NOW
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeApprovalGuard:
    """Explicit TEST ONLY durable approval reader; not actual UI consent."""
    async def require_active(self, context, tx, plan, now):
        value = await tx.get("test_approval/active")
        if value is None:
            raise BindingConflict("test_missing_approval")
        return _decode(value, "test_approval", Approval)


class FakeStopGuard:
    """Explicit TEST ONLY runtime stop attestation, not an elapsed timeout."""
    async def require_stopped(self, context, tx, previous, now):
        value = await tx.get("test_stops/" + str(previous.generation))
        if value is None:
            raise BindingConflict("test_writer_not_stopped")
        return _decode(value, "test_stop", WriterStoppedEvidence)


class Setup:
    def __init__(self, documents=None, cfg=None, *, clock=None, guards=True):
        self.h = Harness(documents, cfg)
        self.clock = clock or Clock()
        self.leases = FirestoreCalendarCoordination(self.h.documents, self.h.policy,
            clock=self.clock, recovery_guard=FakeStopGuard() if guards else None)
        self.store = FirestoreExecutionStore(self.h.documents, self.h.policy,
            coordination=self.leases, approval_guard=FakeApprovalGuard() if guards else None)

    async def seed(self, *, two=False, update=False):
        if not two and not update:
            self.plan = await self.h.seed()
            return self.plan
        a, c = sources(self.h.config)
        with self.h.request("mcp") as ctx:
            await self.h.snapshots.put_assignments(ctx, a)
            await self.h.snapshots.put_calendar(ctx, c)
        blocks = tuple(BlockTarget(block_id=f"b{i}", assignment_id="assignment-1",
            interval=interval(NOW + timedelta(hours=i+1))) for i in range(2 if two else 1))
        ops = tuple(Operation(operation_id=operation_id("plan-test", 1, i), kind="update" if update else "create",
            event_id=calendar_event_id(self.h.config.student.trusted_user_id, self.h.config.student.calendar_id, b.block_id),
            target=b, expected_etag='"before"' if update else None,
            before=interval(NOW + timedelta(hours=4)) if update else None) for i, b in enumerate(blocks))
        self.plan = PlanVersion.seal(PlanPayload(owner_id=self.h.config.student.trusted_user_id,
            calendar_id=self.h.config.student.calendar_id, plan_id="plan-test", version=1, created_at=NOW,
            source_references=(a.source.reference, c.source.reference),
            constraints=(await self.h.seed()).payload.constraints, operations=ops, blocks=blocks,
            allocations=(WorkAllocation(assignment_id="assignment-1", remaining_minutes=60 * len(blocks),
                required_minutes=60 * len(blocks), padding_minutes=0, scheduled_minutes=60 * len(blocks)),)))
        with self.h.request() as ctx:
            await self.h.plans.create(ctx, self.plan)
        return self.plan

    async def approval(self, *, status="active", plan=None):
        plan = plan or self.plan
        value = Approval(owner_id=plan.payload.owner_id, calendar_id=plan.payload.calendar_id,
            plan=plan_reference(plan), approved_by=self.h.config.student.allowed_signin_principal,
            approved_at=NOW, expires_at=NOW + timedelta(minutes=15), status=status,
            invalidated_at=self.clock() if status == "revoked" else None)
        async def put(tx):
            tx.set("test_approval/active", _encode("test_approval", value))
        await self.h.documents.transact(put)

    async def stopped(self, lease):
        value = WriterStoppedEvidence(holder_id=lease.holder_id, generation=lease.generation,
            stopped_at=self.clock(), evidence_reference="test-runtime-stop")
        async def put(tx):
            tx.set("test_stops/" + str(lease.generation), _encode("test_stop", value))
        await self.h.documents.transact(put)

    async def acquire(self, ctx, holder="worker-a", seconds=60):
        return await self.leases.acquire(ctx, holder_id=holder, now=self.clock(),
            expires_at=self.clock() + timedelta(seconds=seconds))

    async def begin(self, ctx):
        self.lease = await self.acquire(ctx)
        self.state = await self.store.bind_request(ctx, request_key="req-1", plan=plan_reference(self.plan), at=self.clock())
        await self.approval()
        self.state = await self.store.compare_and_set(ctx, expected_revision=0,
            execution=ExecutionState.model_validate(self.state.model_dump() | {"status": "approved", "revision": 1}), lease=self.lease)
        return self.state

    async def record(self, ctx, status, *, index=0, observed=None, evidence=None):
        before = self.state.operations[index]
        op = OperationState(operation_id=before.operation_id, status=status,
            attempts=before.attempts + (1 if status == "in_flight" else 0), updated_at=self.clock(),
            fencing_generation=self.lease.generation if status == "in_flight" else before.fencing_generation,
            observed_event=observed, error_code="synthetic_timeout" if status == "unknown" else "known_failure" if status == "failed" else None,
            reconciled_at=evidence.at if evidence and evidence.kind.startswith("read_") else None)
        self.state = await self.store.record_operation(ctx, request_key="req-1", expected_revision=self.state.revision,
            operation=op, lease=self.lease, evidence=evidence)
        return self.state

    def observed(self, index=0):
        op = self.plan.payload.operations[index]
        return CalendarEvent(event_id=op.event_id, etag='"after"', interval=op.target.interval,
            campuspulse_marker=OwnershipMarker(owner_id=self.plan.payload.owner_id,
                calendar_id=self.plan.payload.calendar_id, block_id=op.target.block_id,
                assignment_id=op.target.assignment_id, create_operation_id=op.operation_id),
            event_link="https://calendar.google.com/calendar/event?eid=synthetic")

    def evidence(self, kind="read_target", *, observed=None):
        op = self.plan.payload.operations[0]
        return OutcomeEvidence(operation_id=op.operation_id, target_fingerprint=target_fingerprint(op),
            kind=kind, at=self.clock(), reference="test-read-1", observed_event=observed)


def test_duplicate_binding_changed_payload_and_new_key_rejected():
    async def scenario():
        s = Setup()
        p1 = await s.seed()
        p2 = await s.h.seed(version=2)
        with s.h.request("mcp") as ctx:
            state = await s.store.bind_request(ctx, request_key="req-1", plan=plan_reference(p1), at=NOW)
            assert await s.store.bind_request(ctx, request_key="req-1", plan=plan_reference(p1), at=NOW) == state
            with pytest.raises(BindingConflict, match="payload_conflict"):
                await s.store.bind_request(ctx, request_key="req-1", plan=plan_reference(p2), at=NOW)
            with pytest.raises(BindingConflict, match="already_bound"):
                await s.store.bind_request(ctx, request_key="new-key", plan=plan_reference(p1), at=NOW)
            forged = plan_reference(p1).model_copy(update={"payload_hash": "0" * 64})
            with pytest.raises(BindingConflict, match="hash_mismatch"):
                await s.store.bind_request(ctx, request_key="forged", plan=forged, at=NOW)
    run(scenario())


def test_fake_concurrent_acquire_and_monotonic_clean_release():
    async def scenario():
        s = Setup()
        second = FirestoreCalendarCoordination(s.h.documents, s.h.policy, clock=s.clock)
        with s.h.request("mcp") as ctx:
            outcomes = await asyncio.gather(s.acquire(ctx), second.acquire(ctx, holder_id="worker-b", now=NOW,
                expires_at=NOW + timedelta(seconds=60)), return_exceptions=True)
            assert sum(isinstance(x, BindingConflict) for x in outcomes) == 1
            lease = next(x for x in outcomes if not isinstance(x, Exception))
            await s.leases.release(ctx, lease)
            new = await s.acquire(ctx, "worker-c")
            assert new.generation == 2 and new.state == "active"
            with pytest.raises(BindingConflict, match="stale"):
                await s.leases.assert_current(ctx, lease, now=NOW)
    run(scenario())


def test_renew_exact_token_and_server_clock_prevent_future_takeover():
    async def scenario():
        s = Setup()
        with s.h.request("mcp") as ctx:
            lease = await s.acquire(ctx)
            renewed = await s.leases.renew(ctx, lease, now=NOW, expires_at=NOW + timedelta(seconds=90))
            await s.leases.assert_current(ctx, renewed, now=NOW)
            with pytest.raises(BindingConflict, match="stale"):
                await s.leases.assert_current(ctx, lease, now=NOW)
            with pytest.raises(BindingConflict, match="clock_mismatch"):
                await s.leases.acquire(ctx, holder_id="future", now=NOW + timedelta(minutes=4), expires_at=NOW + timedelta(minutes=5))
    run(scenario())


def test_dispatch_persists_attempt_fingerprint_and_append_only_history():
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            ledger = await s.store.get_ledger(ctx, "req-1")
            assert ledger.attempts[0].attempt_id.startswith("att_")
            assert ledger.attempts[0].target_fingerprint == target_fingerprint(s.plan.payload.operations[0])
            assert ledger.attempts[0].fencing_generation == s.lease.generation
            completed = await s.record(ctx, "completed", observed=s.observed())
            assert completed.status == "applied_unverified"
            assert await s.store.bind_request(ctx, request_key="req-1", plan=completed.plan, at=NOW) == completed
            path = _request_path(completed.owner_id, completed.calendar_id, completed.request_key)
            history = await s.h.documents.scan(path + "/history")
            assert len(history) == 4
            audit = _decode(s.h.documents.data[path + "/history/r2"], "execution_audit", _Audit)
            assert audit.ledger.execution.operations[0].status == "in_flight"
            assert audit.ledger.attempts[0] == ledger.attempts[0]
    run(scenario())


@pytest.mark.parametrize("mode", ["missing", "expired", "revoked", "wrong_plan"])
def test_approval_guard_is_required_for_new_dispatch(mode):
    async def scenario():
        s = Setup(guards=mode != "missing")
        await s.seed()
        with s.h.request("mcp") as ctx:
            s.lease = await s.acquire(ctx, seconds=300)
            state = await s.store.bind_request(ctx, request_key="req-1", plan=plan_reference(s.plan), at=NOW)
            await s.approval(status="revoked" if mode == "revoked" else "active")
            if mode == "wrong_plan":
                await s.approval(plan=await s.h.seed(version=2))
            if mode == "expired":
                s.clock.advance(901)

                s.lease = await s.acquire(ctx, "after-expiry")

                await s.stopped(s.lease.model_copy(update={"holder_id": "worker-a", "generation": 1}))
                s.lease = await s.leases.complete_recovery(ctx, s.lease, now=s.clock())
            next_state = ExecutionState.model_validate(state.model_dump() | {
                "status": "approved", "revision": 1, "updated_at": s.clock()})
            with pytest.raises(BindingConflict, match="approval"):
                await s.store.compare_and_set(ctx, expected_revision=0, execution=next_state, lease=s.lease)
    run(scenario())


@pytest.mark.parametrize("outcome", ["completed", "unknown"])
def test_outcome_is_saved_after_approval_revoked(outcome):
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            await s.approval(status="revoked")
            state = await s.record(ctx, outcome, observed=s.observed() if outcome == "completed" else None)
            assert state.operations[0].status == outcome
    run(scenario())


def test_approval_expiry_during_transaction_reads_does_not_grant_dispatch():
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)

            s.clock.advance(899)
            s.lease = await s.acquire(ctx, "later-worker")
            await s.stopped(s.lease.model_copy(update={"holder_id": "worker-a", "generation": 1}))
            s.lease = await s.leases.complete_recovery(ctx, s.lease, now=s.clock())
            original = s.store.approval_guard
            class SlowApproval:
                async def require_active(self, context, tx, plan, now):
                    approval = await original.require_active(context, tx, plan, now)
                    s.clock.advance(2)
                    return approval
            s.store.approval_guard = SlowApproval()
            with pytest.raises(BindingConflict, match="approval"):
                await s.record(ctx, "in_flight")
            assert (await s.store.get(ctx, "req-1")).operations[0].status == "pending"
    run(scenario())


def test_crash_expiry_restart_reconciles_before_recovery_and_keeps_original_writer(tmp_path):
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            original = s.lease
        checkpoint = tmp_path / "ledger.pkl"
        s.h.documents.checkpoint(checkpoint)
        fresh = Setup(FakeDocuments.restore(checkpoint), clock=s.clock)
        fresh.plan = s.plan
        fresh.clock.advance(61)
        with fresh.h.request("mcp", run="run-restart") as ctx:
            fresh.lease = await fresh.acquire(ctx, "worker-b")
            fresh.state = await fresh.store.get(ctx, "req-1")
            assert fresh.state.operations[0].status == "in_flight"
            assert fresh.lease.state == "recovery_required" and fresh.lease.generation == 2
            with pytest.raises(BindingConflict, match="unreconciled"):
                await fresh.leases.complete_recovery(ctx, fresh.lease, now=fresh.clock())
            with pytest.raises(BindingConflict, match="stale"):
                await fresh.leases.release(ctx, original)
            await fresh.record(ctx, "unknown")
            with pytest.raises(BindingConflict, match="reconciliation"):
                await fresh.record(ctx, "completed", observed=fresh.observed())
            await fresh.record(ctx, "completed", observed=fresh.observed(), evidence=fresh.evidence(observed=fresh.observed()))
            with pytest.raises(BindingConflict, match="not_stopped"):
                await fresh.leases.complete_recovery(ctx, fresh.lease, now=fresh.clock())
            fresh.clock.advance(61)
            fresh.lease = await fresh.acquire(ctx, "worker-c")
            claim = _decode(await fresh.h.documents.get(_claim_path(fresh.plan.payload.calendar_id)), "calendar_claim", _Claim)
            assert claim.previous == original
            await fresh.stopped(original)
            active = await fresh.leases.complete_recovery(ctx, fresh.lease, now=fresh.clock())
            assert active.state == "active" and active.generation == 3
            assert (await fresh.store.get(ctx, "req-1")).operations[0].status == "completed"
    run(scenario())


def test_timeout_success_is_reconciled_without_second_attempt():
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")

            external_event = s.observed()
            await s.record(ctx, "unknown")
            await s.record(ctx, "completed", observed=external_event, evidence=s.evidence(observed=external_event))
            ledger = await s.store.get_ledger(ctx, "req-1")
            assert ledger.execution.operations[0].attempts == 1
            assert ledger.execution.status == "applied_unverified"


            with pytest.raises(BindingConflict, match="writer_recovery"):
                await s.leases.release(ctx, s.lease)
            with pytest.raises(BindingConflict, match="writer_recovery"):
                await s.leases.assert_current(ctx, s.lease, now=s.clock())
            original = s.lease
            with pytest.raises(BindingConflict, match="takeover_required"):
                await s.leases.complete_recovery(ctx, s.lease, now=s.clock())
            s.clock.advance(61)
            s.lease = await s.acquire(ctx, "recovery-worker")
            await s.stopped(original)
            s.lease = await s.leases.complete_recovery(ctx, s.lease, now=s.clock())
            await s.leases.release(ctx, s.lease)
    run(scenario())


def test_partial_progress_survives_failure_and_unknown_cannot_release_or_switch_plan():
    async def scenario():
        s = Setup()
        await s.seed(two=True)
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            await s.record(ctx, "completed", observed=s.observed())
            await s.record(ctx, "in_flight", index=1)
            state = await s.record(ctx, "unknown", index=1)
            assert state.status == "partial" and state.operations[0].status == "completed"
            with pytest.raises(BindingConflict, match="unreconciled"):
                await s.leases.release(ctx, s.lease)
            p2 = await s.h.seed(version=2)
            await s.store.bind_request(ctx, request_key="req-2", plan=plan_reference(p2), at=NOW)
            s.clock.advance(61)
            s.lease = await s.acquire(ctx, "takeover")
            before = await s.store.get(ctx, "req-2")
            operation = before.operations[0].model_copy(update={"status": "failed", "error_code": "known_failure"})
            with pytest.raises(BindingConflict):
                await s.store.record_operation(ctx, request_key="req-2", expected_revision=0, operation=operation, lease=s.lease)
            assert (await s.store.get(ctx, "req-1")).operations[0].status == "completed"
    run(scenario())


@pytest.mark.parametrize("wrong", ["interval", "etag", "event", "marker", "recurrence"])
def test_update_completion_requires_target_and_version_evidence(wrong):
    async def scenario():
        s = Setup()
        await s.seed(update=True)
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            event = s.observed()
            changes = {"interval": {"interval": interval(NOW + timedelta(hours=3))},
                "etag": {"etag": '"before"'}, "event": {"event_id": "other-event"},
                "marker": {"campuspulse_marker": None},
                "recurrence": {"recurrence": {"series_event_id": "series", "original_start_time": NOW}}}
            bad = CalendarEvent.model_validate(event.model_dump() | changes[wrong])
            with pytest.raises(BindingConflict, match="target_evidence"):
                await s.record(ctx, "completed", observed=bad)
            assert (await s.store.get(ctx, "req-1")).operations[0].status == "in_flight"
    run(scenario())


def test_unknown_no_effect_needs_stopped_writer_then_new_approval_on_retry():
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            old_lease = s.lease
            await s.record(ctx, "unknown")
            s.clock.advance(61)
            s.lease = await s.acquire(ctx, "new-worker")
            await s.approval(status="revoked")
            with pytest.raises(BindingConflict, match="not_stopped"):
                await s.record(ctx, "pending", evidence=s.evidence("read_no_effect"))
            await s.stopped(old_lease)
            await s.record(ctx, "pending", evidence=s.evidence("read_no_effect"))
            s.lease = await s.leases.complete_recovery(ctx, s.lease, now=s.clock())
            with pytest.raises(BindingConflict, match="approval"):
                await s.record(ctx, "in_flight")
            await s.approval()
            await s.record(ctx, "in_flight")
            assert s.state.operations[0].attempts == 2
            history = await s.h.documents.scan(_request_path(s.state.owner_id, s.state.calendar_id, "req-1") + "/history")
            attempts = {_decode(a, "execution_audit", _Audit).ledger.attempts[0].attempt_id
                for a in history if _decode(a, "execution_audit", _Audit).ledger.attempts}
            assert len(attempts) == 2
    run(scenario())


def test_partial_known_failure_keeps_completed_result_and_error_evidence():
    async def scenario():
        s = Setup()
        await s.seed(two=True)
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            await s.record(ctx, "completed", observed=s.observed())
            await s.record(ctx, "in_flight", index=1)
            op = s.plan.payload.operations[1]
            evidence = OutcomeEvidence(operation_id=op.operation_id, target_fingerprint=target_fingerprint(op),
                kind="response_no_effect", at=NOW, reference="test-412-response")
            state = await s.record(ctx, "failed", index=1, evidence=evidence)
            assert state.status == "partial"
            assert state.operations[0].observed_event == s.observed()
            assert state.operations[1].error_code == "known_failure"
            with pytest.raises(BindingConflict, match="already_bound"):
                await s.store.bind_request(ctx, request_key="hide-failure", plan=state.plan, at=NOW)
    run(scenario())


@pytest.mark.parametrize("wrong", ["missing", "etag", "interval"])
def test_update_unknown_cannot_replay_from_event_existence_or_wrong_version(wrong):
    async def scenario():
        s = Setup()
        await s.seed(update=True)
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            await s.record(ctx, "in_flight")
            await s.record(ctx, "unknown")
            original = s.lease
            s.clock.advance(61)
            s.lease = await s.acquire(ctx, "reconciler")
            await s.stopped(original)
            op = s.plan.payload.operations[0]
            event = CalendarEvent(event_id=op.event_id, etag=op.expected_etag, interval=op.before)
            if wrong == "missing":
                event = None
            elif wrong == "etag":
                event = event.model_copy(update={"etag": '"changed-by-user"'})
            else:
                event = event.model_copy(update={"interval": interval(NOW + timedelta(hours=3))})
            with pytest.raises(BindingConflict, match="no_effect_evidence"):
                await s.record(ctx, "pending", evidence=s.evidence("read_no_effect", observed=event))
            assert (await s.store.get(ctx, "req-1")).operations[0].status == "unknown"
    run(scenario())


def test_stale_revision_terminal_rewrite_and_manufactured_verification_rejected():
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            approved = s.state
            await s.record(ctx, "in_flight")
            with pytest.raises(BindingConflict, match="revision_conflict"):
                await s.store.compare_and_set(ctx, expected_revision=1,
                    execution=approved.model_copy(update={"revision": 2, "status": "applying"}), lease=s.lease)
            await s.record(ctx, "completed", observed=s.observed())
            with pytest.raises(BindingConflict, match="transition_invalid"):
                await s.record(ctx, "in_flight")
            with pytest.raises(BindingConflict, match="verification"):
                await s.store.compare_and_set(ctx, expected_revision=s.state.revision,
                    execution=s.state.model_copy(update={"revision": s.state.revision+1, "status": "verified"}), lease=s.lease)
    run(scenario())


def test_repeated_in_flight_cas_never_returns_second_dispatch_permit():
    async def scenario():
        s = Setup()
        await s.seed()
        with s.h.request("mcp") as ctx:
            await s.begin(ctx)
            in_flight = await s.record(ctx, "in_flight")
            with pytest.raises(BindingConflict, match="dispatch_already_claimed"):
                await s.store.compare_and_set(ctx, expected_revision=1, execution=in_flight, lease=s.lease)
            with pytest.raises(BindingConflict, match="dispatch_already_claimed"):
                await s.store.record_operation(ctx, request_key="req-1", expected_revision=1,
                    operation=in_flight.operations[0], lease=s.lease)
            assert (await s.store.get(ctx, "req-1")).operations[0].attempts == 1
    run(scenario())


@pytest.mark.parametrize("boundary", ["backend", "agent", "raw_dto"])
def test_mutation_requires_current_trusted_mcp_scope(boundary):
    async def scenario():
        s = Setup()
        with s.h.request("mcp" if boundary == "raw_dto" else boundary) as ctx:
            with pytest.raises(AccessDenied):
                await s.acquire(ctx.as_port_context() if boundary == "raw_dto" else ctx)
        assert s.h.documents.calls == 0
    run(scenario())


def _emulator_config(namespace):
    data = config().model_dump()
    data["project_id"] = "demo-campuspulse-t06"
    data["student"]["trusted_user_id"] = "student-" + namespace
    data["student"]["calendar_id"] = namespace + "@group.calendar.google.com"
    for key, name in (("backend_service_account", "backend"), ("agent_service_account", "agent-test"), ("mcp_service_account", "mcp-test")):
        data["identities"][key] = name + "@demo-campuspulse-t06.iam.gserviceaccount.com"
    return AppConfig.model_validate(data)


def _emulator_setup(namespace):
    from google.cloud.firestore_v1 import AsyncClient
    from google.auth.credentials import AnonymousCredentials
    cfg = _emulator_config(namespace)
    client = AsyncClient(project=cfg.project_id, database=cfg.firestore_database, credentials=AnonymousCredentials())
    return Setup(FirestoreDocuments(client, project_id=cfg.project_id, database_id=cfg.firestore_database), cfg), client


def _loopback_emulator():
    host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
    if not host:
        pytest.skip("T06 emulator not configured; fake tests are not distributed proof")
    import re
    if re.fullmatch(r"(?:127\.0\.0\.1|localhost):[0-9]{1,5}", host) is None:
        pytest.fail("T06 tests require strict loopback FIRESTORE_EMULATOR_HOST")
    pytest.importorskip("google.cloud.firestore_v1")


def _worker(namespace, gate, output, mode):
    """Entrypoint for independent subprocess; no credentials/cloud fallback."""
    _loopback_emulator()
    deadline = time.monotonic() + 30
    while not Path(gate).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test_gate_timeout")
        time.sleep(0.02)

    async def scenario():
        s, client = _emulator_setup(namespace)
        try:
            with s.h.request("mcp") as ctx:
                if mode == "claim":
                    holder = "worker-" + str(os.getpid())
                    try:
                        lease = await s.acquire(ctx, holder)
                        result = {"status": "acquired", "generation": lease.generation}
                    except PortError as exc:


                        result = {"status": "refused", "code": str(exc)}
                    result["holder_id"] = holder
                else:
                    state = await s.store.get(ctx, "req-1")
                    result = {"status": "read", "operation_status": state.operations[0].status,
                        "attempts": state.operations[0].attempts, "revision": state.revision}
                Path(output).write_text(json.dumps(result), encoding="utf-8")
        finally:
            client.close()
    asyncio.run(scenario())


def test_emulator_independent_processes_exclusive_claim(tmp_path):
    _loopback_emulator()
    namespace = uuid4().hex
    gate = tmp_path / "go"
    processes, outputs = [], []
    for i in range(2):
        output = tmp_path / f"worker-{i}.json"
        outputs.append(output)
        code = "import runpy; m=runpy.run_path('tests/storage/test_apply_coordination.py'); m['_worker'](*__import__('sys').argv[1:])"
        processes.append(subprocess.Popen([sys.executable, "-c", code, namespace, str(gate), str(output), "claim"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    gate.touch()
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=50)
            assert process.returncode == 0, stdout + stderr
        results = [json.loads(p.read_text(encoding="utf-8")) for p in outputs]
        acquired = [r for r in results if r["status"] == "acquired"]
        assert len(acquired) <= 1, results
        for result in results:
            assert result["status"] in {"acquired", "refused"}, results
            if result["status"] == "refused":
                assert result["code"] in {
                    "calendar_claim_busy", "storage_transaction_outcome_unknown"}, results

        async def persisted_claim():
            s, client = _emulator_setup(namespace)
            try:
                document = await client.document(_claim_path(s.h.config.student.calendar_id)).get()
                return _decode(document.to_dict(), "calendar_claim", _Claim) if document.exists else None
            finally:
                client.close()

        # Contention can exhaust both SDK transactions in the emulator. A failed
        # response is not proof that no claim committed: inspect durable state.
        claim = asyncio.run(persisted_claim())
        if claim is None:
            assert not acquired, results
            assert all(r["code"] == "storage_transaction_outcome_unknown" for r in results), results
        else:
            assert claim.lease.generation == 1
            assert claim.lease.holder_id in {r["holder_id"] for r in results}
            if acquired:
                assert acquired[0]["generation"] == 1
                assert claim.lease.holder_id == acquired[0]["holder_id"]

        # An independent, uncontended process must acquire an empty claim or
        # refuse the persisted owner. This checks progress and exclusivity
        # without blindly replaying an uncertain Calendar operation.
        probe_output = tmp_path / "probe.json"
        probe = subprocess.Popen([sys.executable, "-c", code, namespace, str(gate), str(probe_output), "claim"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(probe)
        stdout, stderr = probe.communicate(timeout=50)
        assert probe.returncode == 0, stdout + stderr
        result = json.loads(probe_output.read_text(encoding="utf-8"))
        if claim is None:
            assert result["status"] == "acquired", result
            assert result["generation"] == 1
            expected_holder = result["holder_id"]
        else:
            assert result["status"] == "refused", result
            assert result["code"] == "calendar_claim_busy", result
            expected_holder = claim.lease.holder_id
        final_claim = asyncio.run(persisted_claim())
        assert final_claim is not None
        assert final_claim.lease.generation == 1
        assert final_claim.lease.holder_id == expected_holder
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def test_emulator_unknown_survives_new_client_and_process_restart(tmp_path):
    _loopback_emulator()
    namespace = uuid4().hex

    async def write():
        s, client = _emulator_setup(namespace)
        try:
            await s.seed()
            with s.h.request("mcp") as ctx:
                await s.begin(ctx)
                await s.record(ctx, "in_flight")
                await s.record(ctx, "unknown")
        finally:
            client.close()
    asyncio.run(write())
    gate, output = tmp_path / "go", tmp_path / "restart.json"
    gate.touch()
    code = "import runpy; m=runpy.run_path('tests/storage/test_apply_coordination.py'); m['_worker'](*__import__('sys').argv[1:])"
    result = subprocess.run([sys.executable, "-c", code, namespace, str(gate), str(output), "read"],
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "status": "read", "operation_status": "unknown", "attempts": 1, "revision": 3}


def test_emulator_duplicate_cas_lease_expiry_stale_worker_and_partial_progress():
    _loopback_emulator()

    async def scenario():
        namespace = uuid4().hex
        s, client = _emulator_setup(namespace)
        fresh, second_client = _emulator_setup(namespace)
        try:
            await s.seed(two=True)
            fresh.plan = s.plan
            with s.h.request("mcp") as ctx, fresh.h.request("mcp") as second_ctx:
                await s.begin(ctx)
                assert await fresh.store.bind_request(second_ctx, request_key="req-1", plan=s.state.plan, at=NOW) == s.state
                p2 = await s.h.seed(version=2)
                with pytest.raises(BindingConflict, match="payload_conflict"):
                    await fresh.store.bind_request(second_ctx, request_key="req-1", plan=plan_reference(p2), at=NOW)
                await s.record(ctx, "in_flight")
                await s.record(ctx, "completed", observed=s.observed())
                await s.record(ctx, "in_flight", index=1)
                old_lease = s.lease
                fresh.clock.advance(61)
                fresh.lease = await fresh.acquire(second_ctx, "restarted")
                fresh.state = await fresh.store.get(second_ctx, "req-1")
                assert fresh.state.status == "partial"
                assert fresh.state.operations[0].status == "completed"
                s.clock.advance(61)
                with pytest.raises(BindingConflict, match="stale"):
                    await s.record(ctx, "completed", index=1, observed=s.observed(1))
                await fresh.record(second_ctx, "unknown", index=1)
                with pytest.raises(BindingConflict, match="unreconciled"):
                    await fresh.leases.complete_recovery(second_ctx, fresh.lease, now=fresh.clock())
                assert fresh.lease.generation == old_lease.generation + 1
        finally:
            client.close()
            second_client.close()
    asyncio.run(scenario())


def test_emulator_two_clients_one_operation_dispatch_permit():
    _loopback_emulator()

    async def scenario():
        namespace = uuid4().hex
        s, client = _emulator_setup(namespace)
        other, second_client = _emulator_setup(namespace)
        try:
            await s.seed()
            with s.h.request("mcp") as ctx, other.h.request("mcp") as second_ctx:
                await s.begin(ctx)
                old = s.state.operations[0]
                intent = OperationState(operation_id=old.operation_id, status="in_flight", attempts=1,
                    updated_at=NOW, fencing_generation=s.lease.generation)


                outcomes = await asyncio.gather(
                    s.store.record_operation(ctx, request_key="req-1", expected_revision=1,
                        operation=intent, lease=s.lease),
                    other.store.record_operation(second_ctx, request_key="req-1", expected_revision=1,
                        operation=intent, lease=s.lease), return_exceptions=True)
                assert sum(isinstance(o, ExecutionState) for o in outcomes) == 1
                refused = next(o for o in outcomes if not isinstance(o, ExecutionState))
                assert isinstance(refused, PortError)
                assert str(refused) in {"execution_dispatch_already_claimed", "execution_revision_conflict",
                    "storage_transaction_outcome_unknown"}
                assert (await other.store.get(second_ctx, "req-1")).operations[0].attempts == 1
        finally:
            client.close()
            second_client.close()
    asyncio.run(scenario())
