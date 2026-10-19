"""Ownership persistence with FAKE transactional store/Calendar observations."""

import asyncio
from datetime import timedelta

import pytest

from campuspulse_recovery.domain.models import (
    CalendarEvent, CalendarSnapshot, OwnedBlock, OwnedBlockRecord, OwnershipMarker,
    PlanPayload, PlanVersion, calendar_content_hash,
)
from campuspulse_recovery.domain.ports import BindingConflict
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.owned_blocks import FirestoreOwnedBlockRegistry
from campuspulse_recovery.storage.plans import _encode, _key, plan_reference
from tests.storage.test_plans import Harness, NOW, FakeDocuments, run, sources


def reservation(plan):
    op = plan.payload.operations[0]
    return OwnedBlockRecord(owner_id=plan.payload.owner_id, calendar_id=plan.payload.calendar_id,
        block_id=op.target.block_id, assignment_id=op.target.assignment_id, event_id=op.event_id,
        state="reserved", intent_plan=plan_reference(plan), intent_operation_id=op.operation_id, reserved_at=NOW)


def observed(plan):
    record = reservation(plan)
    return CalendarEvent(event_id=record.event_id, etag='"created-etag"', interval=plan.payload.operations[0].target.interval,
        campuspulse_marker=OwnershipMarker(owner_id=record.owner_id, calendar_id=record.calendar_id,
            block_id=record.block_id, assignment_id=record.assignment_id, create_operation_id=record.intent_operation_id))


def test_atomic_reserve_confirm_lookup_and_restart(tmp_path):
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        with h.request("mcp") as ctx:
            assert await registry.reserve(ctx, record) == record
            assert await registry.reserve(ctx, record) == record
            assert await registry.get_by_event(ctx, record.event_id) == record
            assert await registry.list_for_calendar(ctx) == (record,)
            confirmed = await registry.confirm(ctx, record, observed=observed(plan), at=NOW + timedelta(seconds=1))
            assert confirmed.state == "confirmed"
            assert await registry.reserve(ctx, record) == confirmed
            assert await registry.confirm(ctx, record, observed=observed(plan), at=NOW + timedelta(seconds=2)) == confirmed
            assert await registry.get_by_block(ctx, record.block_id) == confirmed
        path = tmp_path / "registry-fake.pkl"
        h.documents.checkpoint(path)
        fresh = Harness(FakeDocuments.restore(path))
        registry = FirestoreOwnedBlockRegistry(fresh.documents, fresh.policy)
        with fresh.request("mcp", run="later-run") as ctx:
            assert await registry.get_by_event(ctx, record.event_id) == confirmed
            assert await registry.list_for_calendar(ctx) == (confirmed,)
    run(scenario())


@pytest.mark.parametrize("confirmed", [False, True])
def test_reservation_replay_with_new_timestamp_preserves_original_durable_record(confirmed):
    async def scenario():
        from copy import deepcopy
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        retry = OwnedBlockRecord.model_validate(record.model_dump() | {"reserved_at": NOW + timedelta(seconds=1)})
        with h.request("mcp") as ctx:
            expected = await registry.reserve(ctx, record)
            if confirmed:
                expected = await registry.confirm(ctx, record, observed=observed(plan), at=NOW + timedelta(seconds=2))
            before = deepcopy(h.documents.data)
            replay = await registry.reserve(ctx, retry)
            assert replay == expected
            assert replay.reserved_at == NOW
            assert h.documents.data == before
            assert await registry.get_by_event(ctx, record.event_id) == expected
    run(scenario())


def test_new_reservation_with_invalid_chronology_is_rejected_without_binding():
    async def scenario():
        from copy import deepcopy
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        invalid = OwnedBlockRecord.model_validate(record.model_dump() | {"reserved_at": NOW - timedelta(seconds=1)})
        before = deepcopy(h.documents.data)
        with h.request("mcp") as ctx:
            with pytest.raises(BindingConflict, match="intent_plan_mismatch"):
                await registry.reserve(ctx, invalid)
            assert await registry.get_by_block(ctx, record.block_id) is None
        assert h.documents.data == before
    run(scenario())


@pytest.mark.parametrize("field", ["owner_id", "calendar_id", "block_id", "assignment_id", "event_id", "plan_hash", "operation_id"])
def test_timestamp_replay_never_ignores_changed_stable_binding(field):
    async def scenario():
        from copy import deepcopy
        from pydantic import ValidationError
        from campuspulse_recovery.domain.models import calendar_event_id
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        data = record.model_dump()
        data["reserved_at"] = NOW + timedelta(seconds=1)
        if field == "plan_hash":
            data["intent_plan"]["payload_hash"] = "0" * 64
        elif field == "operation_id":
            data["intent_operation_id"] = "another-operation"
        elif field == "event_id":
            data["event_id"] = "cp" + "0" * 64
        else:
            data[field] = "another-" + field
            if field in ("owner_id", "calendar_id", "block_id"):
                data["event_id"] = calendar_event_id(data["owner_id"], data["calendar_id"], data["block_id"])
        with h.request("mcp") as ctx:
            await registry.reserve(ctx, record)
            before = deepcopy(h.documents.data)


            with pytest.raises((BindingConflict, AccessDenied, ValidationError)):
                changed = OwnedBlockRecord.model_validate(data)
                await registry.reserve(ctx, changed)
            assert await registry.get_by_event(ctx, record.event_id) == record
            assert h.documents.data == before
    run(scenario())


def test_registry_rejects_extreme_intent_version_before_io():
    async def scenario():
        from campuspulse_recovery.storage.plans import MAX_STORAGE_PLAN_VERSION
        h = Harness()
        plan = await h.seed()
        record, event = reservation(plan), observed(plan)
        data = record.model_dump()
        data["intent_plan"]["version"] = MAX_STORAGE_PLAN_VERSION + 1
        changed = OwnedBlockRecord.model_validate(data)
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        before = h.documents.calls
        with h.request("mcp") as ctx:
            with pytest.raises(BindingConflict, match="version_out_of_range"):
                await registry.reserve(ctx, changed)
            with pytest.raises(BindingConflict, match="version_out_of_range"):
                await registry.confirm(ctx, changed, observed=event, at=NOW)
        assert h.documents.calls == before
    run(scenario())


def test_forged_calendar_metadata_without_registry_never_grants_ownership():
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        with h.request("mcp") as ctx:
            assert await registry.get_by_event(ctx, observed(plan).event_id) is None
            assert await registry.list_for_calendar(ctx) == ()
            with pytest.raises(BindingConflict, match="not_found"):
                await registry.confirm(ctx, reservation(plan), observed=observed(plan), at=NOW)
            assert await registry.list_for_calendar(ctx) == ()
    run(scenario())


def test_snapshot_owned_blocks_require_confirmed_durable_registry_join():
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        event, record = observed(plan), reservation(plan)
        block = OwnedBlock(owner_id=record.owner_id, calendar_id=record.calendar_id,
            block_id=record.block_id, assignment_id=record.assignment_id, event_id=record.event_id,
            interval=event.interval, etag=event.etag)
        _, calendar = sources(h.config)
        reference = calendar.source.reference.model_copy(update={"snapshot_id": "new-calendar-snapshot",
            "content_hash": calendar_content_hash((event,), (block,), True)})
        snapshot = CalendarSnapshot(source=calendar.source.model_copy(update={"reference": reference}),
            events=(event,), owned_blocks=(block,), recurrence_expanded=True)
        with h.request("mcp") as ctx:
            with pytest.raises(BindingConflict):
                await h.snapshots.put_calendar(ctx, snapshot)
            await registry.reserve(ctx, record)
            with pytest.raises(BindingConflict, match="registry_mismatch"):
                await h.snapshots.put_calendar(ctx, snapshot)
            await registry.confirm(ctx, record, observed=event, at=NOW)
            assert await h.snapshots.put_calendar(ctx, snapshot) == reference
            assert await h.snapshots.get_calendar(ctx, reference) == snapshot
    run(scenario())


def test_other_owner_cannot_lookup_or_confirm_shared_calendar_binding():
    async def scenario():
        from campuspulse_recovery.config import AppConfig
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        with h.request("mcp") as ctx:
            await registry.reserve(ctx, record)
        cfg = h.config.model_dump()
        cfg["student"]["trusted_user_id"] = "other-student"
        other = Harness(h.documents, AppConfig.model_validate(cfg))
        foreign = FirestoreOwnedBlockRegistry(other.documents, other.policy)
        with other.request("mcp") as ctx:
            with pytest.raises(AccessDenied):
                await foreign.get_by_event(ctx, record.event_id)
            with pytest.raises(AccessDenied):
                await foreign.confirm(ctx, record, observed=observed(plan), at=NOW)
            with pytest.raises(AccessDenied):
                await foreign.list_for_calendar(ctx)
    run(scenario())


@pytest.mark.parametrize("change", ["marker", "target", "etag", "status", "transparent", "event", "recurrence", "before_reservation"])
def test_confirmation_requires_exact_observation_and_intent(change):
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record, event = reservation(plan), observed(plan)
        data = event.model_dump(mode="json")
        at = NOW
        if change == "marker":
            data["campuspulse_marker"]["create_operation_id"] = "forged"
        elif change == "target":
            data["interval"]["end"] = (event.interval.end + timedelta(minutes=15)).isoformat()
        elif change == "etag":
            data["etag"] = None
        elif change == "status":
            data["status"] = "cancelled"
        elif change == "transparent":
            data["transparency"] = "transparent"
        elif change == "event":
            data["event_id"] = "foreign-event"
        elif change == "recurrence":
            data["recurrence"] = {"series_event_id": "external-series", "original_start_time": NOW.isoformat()}
        else:
            at -= timedelta(seconds=1)
        event = CalendarEvent.model_validate(data)
        with h.request("mcp") as ctx:
            await registry.reserve(ctx, record)
            with pytest.raises(BindingConflict, match="observation"):
                await registry.confirm(ctx, record, observed=event, at=at)
            assert await registry.get_by_block(ctx, record.block_id) == record
    run(scenario())


@pytest.mark.parametrize("fault", ["assignment", "operation", "plan_hash", "another_plan_version", "confirmed_input"])
def test_invalid_trusted_create_intent_and_existing_binding_collision(fault):
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        data = record.model_dump(mode="json")
        if fault == "assignment":
            data["assignment_id"] = "other-assignment"
        elif fault == "operation":
            data["intent_operation_id"] = "forged"
        elif fault == "plan_hash":
            data["intent_plan"]["payload_hash"] = "0" * 64
        elif fault == "another_plan_version":
            other = await h.seed(version=2)
            data = reservation(other).model_dump(mode="json")
        else:
            data |= {"state": "confirmed", "confirmed_at": NOW.isoformat(), "confirmed_etag": "fake-etag"}
        with h.request("mcp") as ctx:
            await registry.reserve(ctx, record)
            with pytest.raises(BindingConflict):
                await registry.reserve(ctx, OwnedBlockRecord.model_validate(data))
            assert await registry.get_by_event(ctx, record.event_id) == record
    run(scenario())


def test_registry_refuses_update_operation_as_create_intent():
    async def scenario():
        h = Harness()
        original = await h.seed()
        payload = original.payload.model_dump(mode="json")
        payload["operations"][0] |= {"kind": "update", "expected_etag": "old-etag", "before": {
            "start": (NOW + timedelta(hours=3)).isoformat(), "end": (NOW + timedelta(hours=4)).isoformat()}}
        payload["plan_id"] = "update-plan"
        from campuspulse_recovery.domain.models import operation_id
        payload["operations"][0]["operation_id"] = operation_id("update-plan", 1, 0)
        update = PlanVersion.seal(PlanPayload.model_validate(payload))
        with h.request() as ctx:
            await h.plans.create(ctx, update)
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        with h.request("mcp") as ctx:
            with pytest.raises(BindingConflict, match="create_intent"):
                await registry.reserve(ctx, reservation(update))
    run(scenario())


def test_colliding_event_index_rejects_and_does_not_create_partial_binding():
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)

        path = f"cp_calendars/{_key(record.calendar_id)}/events/{_key(record.event_id)}"
        h.documents.data[path] = _encode("owned_block", record)
        with h.request("mcp") as ctx:
            with pytest.raises(BindingConflict):
                await registry.reserve(ctx, record)
            assert await registry.get_by_block(ctx, record.block_id) is None
    run(scenario())


def test_confirmation_never_rewrites_creation_evidence():
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        record = reservation(plan)
        with h.request("mcp") as ctx:
            await registry.reserve(ctx, record)
            confirmed = await registry.confirm(ctx, record, observed=observed(plan), at=NOW)
            changed = CalendarEvent.model_validate(observed(plan).model_dump() | {"etag": "later-version"})
            with pytest.raises(BindingConflict, match="confirmation_changed"):
                await registry.confirm(ctx, record, observed=changed, at=NOW + timedelta(seconds=1))
            assert await registry.get_by_block(ctx, record.block_id) == confirmed
        with h.request() as ctx:
            with pytest.raises(AccessDenied):
                await registry.reserve(ctx, record)
    run(scenario())


def test_concurrent_identical_reservations_share_one_binding():
    async def scenario():
        h = Harness()
        plan = await h.seed()
        registry = FirestoreOwnedBlockRegistry(h.documents, h.policy)
        with h.request("mcp") as ctx:
            results = await asyncio.gather(*(registry.reserve(ctx, reservation(plan)) for _ in range(8)))
            assert all(result == reservation(plan) for result in results)
            assert len(await registry.list_for_calendar(ctx)) == 1
    run(scenario())
