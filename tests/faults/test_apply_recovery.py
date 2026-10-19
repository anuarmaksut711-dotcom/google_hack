"""T27 actual apply/preflight/verify repositories, fake documents/Google HTTP.

These are local composition tests, NOT distributed or live evidence.
"""
from copy import deepcopy
from datetime import timedelta

import pytest

from campuspulse_recovery.application.reconcile import changed
from campuspulse_recovery.domain.models import Interval, PlanPayload, PlanVersion, SolveRepairInput
from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.domain.solver import solve_repair
from campuspulse_recovery.domain.time_rules import planning_horizon
from campuspulse_recovery.storage.plans import plan_reference
from tests.faults.support import codes, event, install_network_guard, prepared, run


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    install_network_guard(monkeypatch)


def test_duplicate_request_has_no_new_calls_and_payload_mismatch_rejected(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch)
        first = await h.apply()
        assert first.execution.status == "applied_unverified"
        before = deepcopy(h.api.calls)
        second = await h.apply()
        assert second.execution == first.execution and h.api.calls == before
        assert len(h.api.mutations) == 1 and first.execution.operations[0].attempts == 1
        other = await h.seed(version=2)
        h.request_input = changed(h.request_input, plan_id=other.payload.plan_id, version=2)
        with pytest.raises(BindingConflict, match="request_key_payload_conflict"):
            await h.apply()
        assert h.api.calls == before
    run(scenario())


def test_calendar_reads_and_writes_are_outside_transaction_callbacks(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch)
        inside = False
        callbacks = 0
        original_tx, original_http = h.documents.transact, h.api.request
        async def traced_tx(callback):
            async def traced_callback(tx):
                nonlocal inside, callbacks
                assert not inside
                inside = True
                callbacks += 1
                try:
                    return await callback(tx)
                finally:
                    inside = False
            return await original_tx(traced_callback)
        def traced_http(*args, **kwargs):
            assert not inside, "Calendar side effect/read inside retryable Firestore transaction"
            return original_http(*args, **kwargs)
        h.documents.transact = traced_tx
        h.api.request = traced_http
        assert (await h.apply()).execution.status == "applied_unverified"
        assert (await h.verify()).status == "verified"
        assert callbacks > 0 and len(h.api.mutations) == 1
    run(scenario())


@pytest.mark.parametrize("update", [False, True], ids=["create", "update"])
def test_timeout_after_success_reconciles_actual_target_without_replay(monkeypatch, update):
    async def scenario():
        h = await prepared(monkeypatch, update=update)
        op = h.plan.payload.operations[0]
        def lost_response(raw, response):
            raise TimeoutError("synthetic response lost after Calendar committed")
        h.api.after_write = lost_response
        result = await h.apply()
        state = result.execution.operations[0]
        assert result.execution.status == "applied_unverified" and result.verification is None
        assert state.status == "completed" and state.attempts == 1 and state.reconciled_at is not None
        assert state.observed_event.interval == op.target.interval
        assert state.observed_event.etag != op.expected_etag
        assert len(h.api.mutations) == 1
        assert h.api.mutations[0][0] == ("PATCH" if update else "POST")
        reads = [c for c in h.api.calls if c[0] == "GET" and c[1].endswith("/" + op.event_id)]
        write_index = next(i for i, call in enumerate(h.api.calls) if call[0] != "GET")
        assert reads and any(i > write_index and call[0] == "GET" and call[1].endswith("/" + op.event_id)
                             for i, call in enumerate(h.api.calls))
        assert len([v for v in h.api.events.values() if v["id"] == op.event_id]) == 1
        calls = deepcopy(h.api.calls)
        assert (await h.apply()).execution == result.execution and h.api.calls == calls
    run(scenario())


@pytest.mark.parametrize("effect", ["old", "target_old_etag", "edited"])
def test_update_timeout_event_existence_is_not_target_proof(monkeypatch, effect):
    async def scenario():
        h = await prepared(monkeypatch, update=True)
        op = h.plan.payload.operations[0]
        original = deepcopy(h.api.events[op.event_id])
        if effect == "old":
            h.api.failures["PATCH"] = TimeoutError("before remote write")
        else:
            def lost_response(raw, response):
                if effect == "target_old_etag":
                    raw["etag"] = op.expected_etag
                else:
                    raw.update(etag='"manual"', start=original["start"], end=original["end"])
                raise TimeoutError("synthetic ambiguous target")
            h.api.after_write = lost_response
        result = await h.apply()
        assert result.execution.status == "unknown"
        assert result.execution.operations[0].status == "unknown"
        assert result.execution.operations[0].observed_event is None
        assert len(h.api.mutations) == 1 and h.api.mutations[0][4]["If-Match"] == op.expected_etag
        saved = deepcopy(h.api.events[op.event_id])
        h.expire_lease()
        recovered = await h.apply()
        assert recovered.execution.status == "unknown" and len(h.api.mutations) == 1
        assert h.api.events[op.event_id] == saved  # no overwrite of old/manual state
    run(scenario())


def test_deterministic_id_collision_is_never_adopted(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch)
        original = h.executor._write
        foreign = None
        async def race(ctx, plan, op, record, lease, request_key):
            nonlocal foreign
            foreign = event(op.event_id, start=op.target.interval.start, summary="external owner")
            h.api.events[op.event_id] = deepcopy(foreign)
            return await original(ctx, plan, op, record, lease, request_key)
        h.executor._write = race
        result = await h.apply()
        op = h.plan.payload.operations[0]
        assert result.execution.status == "unknown" and len(h.api.mutations) == 1
        assert h.api.events[op.event_id] == foreign
        with h.scope() as ctx:
            record = await h.registry.get_by_event(ctx, op.event_id)
            assert record.state == "reserved"  # never confirmed foreign metadata
        h.expire_lease()
        assert (await h.apply()).execution.status == "unknown"
        assert len(h.api.mutations) == 1
    run(scenario())


def test_stale_etag_racing_conditional_patch_preserves_manual_edit(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch, update=True)
        op = h.plan.payload.operations[0]
        before = deepcopy(h.api.events[op.event_id])
        h.api.before_patch = lambda raw: raw.update(etag='"manual-new-version"', summary="manual edit")
        result = await h.apply()
        assert result.execution.status == "unknown"
        assert h.api.events[op.event_id]["etag"] == '"manual-new-version"'
        assert h.api.events[op.event_id]["start"] == before["start"]
        assert h.api.events[op.event_id]["end"] == before["end"]
        assert h.api.mutations[0][4]["If-Match"] == op.expected_etag
        assert len(h.api.mutations) == 1
    run(scenario())


def test_stale_availability_blocks_write_even_when_owned_etag_unchanged(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch, update=True)
        op = h.plan.payload.operations[0]
        before_etag = h.api.events[op.event_id]["etag"]
        h.api.events["new-busy"] = event("new-busy", start=op.target.interval.start)
        result = await h.apply()
        assert "preflight_new_busy_conflict" in codes(result)
        assert not h.api.mutations
        assert h.api.events[op.event_id]["etag"] == before_etag == op.expected_etag
        assert result.execution.operations[0].attempts == 0
        assert any(c[0] == "GET" and c[1].endswith("/events") for c in h.api.calls)
    run(scenario())


@pytest.mark.parametrize("change", ["clean", "busy", "revoked"])
def test_partial_restart_preserves_completed_and_rechecks_authority(monkeypatch, tmp_path, change):
    async def scenario():
        h = await prepared(monkeypatch, two=True)
        def interrupt(raw, response):
            h.api.failures["GET"] = TimeoutError("between operations")
            return response(200, raw)
        h.api.after_write = interrupt
        first = await h.apply()
        assert first.execution.status == "partial"
        assert [o.status for o in first.execution.operations] == ["completed", "pending"]
        assert len(h.api.mutations) == 1
        checkpoint = tmp_path / "synthetic-documents.pkl"
        h.documents.checkpoint(checkpoint)
        h.recompose(type(h.documents).restore(checkpoint))
        h.api.after_write = None
        h.reader.clients = h.clients
        if change == "busy":
            h.api.events["new-busy"] = event("new-busy", start=h.plan.payload.operations[1].target.interval.start)
        elif change == "revoked":
            await h.revoke()
        second = await h.apply()
        assert second.execution.operations[0] == first.execution.operations[0]
        assert second.execution.run_id == first.execution.run_id
        assert second.execution.status == ("applied_unverified" if change == "clean" else "partial")
        assert len(h.api.mutations) == (2 if change == "clean" else 1)
        assert second.execution.operations[1].attempts == (1 if change == "clean" else 0)
    run(scenario())


def test_crash_gap_success_before_ledger_checkpoint_is_reconciled(monkeypatch, tmp_path):
    async def scenario():
        h = await prepared(monkeypatch, two=True)
        original = h.executions.record_operation
        async def unavailable(*args, **kwargs):
            if kwargs["operation"].status != "in_flight":
                raise PortError("synthetic ledger outage after Calendar commit")
            return await original(*args, **kwargs)
        h.executions.record_operation = unavailable
        first = await h.apply()
        assert first.execution.operations[0].status == "in_flight" and len(h.api.mutations) == 1
        checkpoint = tmp_path / "crash-gap.pkl"
        h.documents.checkpoint(checkpoint)
        h.recompose(type(h.documents).restore(checkpoint))
        h.reader.clients = h.clients
        h.expire_lease()
        second = await h.apply()
        assert second.execution.status == "partial"
        assert second.execution.operations[0].status == "completed"
        assert second.execution.operations[0].reconciled_at is not None
        assert second.execution.operations[1].status == "pending"
        assert len(h.api.mutations) == 1  # surviving writer not certified stopped
        assert second.execution.operations[0].attempts == 1
        await h.stop()
        h.expire_lease()  # new recovery holder; live previous recovery cannot be stolen
        third = await h.apply()
        assert third.execution.status == "applied_unverified", third
        assert len(h.api.mutations) == 2
        assert [o.attempts for o in third.execution.operations] == [1, 1]
    run(scenario())


@pytest.mark.parametrize("failure", ["read_outage", "post_preflight_conflict", "wrong_target"])
def test_failed_final_verification_never_promotes_success(monkeypatch, failure):
    async def scenario():
        h = await prepared(monkeypatch)
        result = await h.apply()
        assert result.execution.status == "applied_unverified"
        op = h.plan.payload.operations[0]
        if failure == "read_outage":
            h.api.failures["GET"] = TimeoutError("final fresh read unavailable")
        elif failure == "post_preflight_conflict":
            h.api.events["late-busy"] = event("late-busy", start=op.target.interval.start)
        else:
            h.api.events[op.event_id]["end"]["dateTime"] = (op.target.interval.end + timedelta(minutes=15)).isoformat()
        h.now += timedelta(seconds=1)
        h.api.calls.clear()
        verified = await h.verify()
        assert verified.status == ("applied_unverified" if failure == "read_outage" else "verification_failed")
        assert codes(verified) and not h.api.mutations
        assert any(c[0] == "GET" and c[1].endswith("/events") for c in h.api.calls)
        with h.scope() as ctx:
            saved = await h.verification.latest(ctx, plan_reference(h.plan))
            assert saved.result == verified
            state = await h.executions.get(ctx, "apply-key")
            assert state.status != "verified"
        if failure == "post_preflight_conflict":
            assert "verify_external_busy_conflict" in codes(verified)
    run(scenario())


def test_no_change_revalidated_and_fresh_verified_with_zero_writes(monkeypatch):
    async def scenario():
        h = await prepared(monkeypatch)
        await h.apply()
        h.horizon = Interval(start=planning_horizon(h.now)[0], end=planning_horizon(h.now)[1])
        constraints = changed(h.plan.payload.constraints, horizon=h.horizon)
        with h.request(run="t27-nochange") as ctx:
            assignments = await h.assignment_reader.read(ctx, h.horizon)
            calendar = await h.reader.read(ctx, h.horizon)
        solved = solve_repair(SolveRepairInput(assignments=assignments, calendar=calendar,
            constraints=constraints, now=h.now, plan_id="t27-nochange", version=1))
        assert solved.status == "no_change" and solved.operations == ()
        plan = PlanVersion.seal(PlanPayload(owner_id=h.plan.payload.owner_id,
            calendar_id=h.plan.payload.calendar_id, plan_id="t27-nochange", version=1, created_at=h.now,
            source_references=solved.source_references, constraints=constraints, blocks=solved.blocks,
            operations=(), allocations=solved.allocations))
        with h.request("backend", run="t27-nochange") as ctx:
            await h.plans.create(ctx, plan)
            await h.plans.set_current(ctx, plan_reference(plan), expected=plan_reference(h.plan))
            await h.approvals.approve(ctx, plan_reference(plan), "t27-synthetic-nochange")
        h.plan = plan
        h.request_input = changed(h.request_input, plan_id=plan.payload.plan_id, request_key="t27-nochange-key")
        h.api.calls.clear()
        applied = await h.apply()
        assert applied.execution.operations == () and not h.api.mutations
        result = await h.verify()
        assert result.status == "verified" and result.verification_at == h.now
        assert not h.api.mutations and h.sheet_calls >= 2
        assert any(c[0] == "GET" and c[1].endswith("/events") for c in h.api.calls)
    run(scenario())
