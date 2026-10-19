"""Offline transactional fake: persistence/leases, not live Firestore or Moodle."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from campuspulse_recovery.domain.ports import BindingConflict
from campuspulse_recovery.lms.client import MoodleError
from campuspulse_recovery.lms.models import MoodleSnapshot
from campuspulse_recovery.lms.sync import MoodleSync, _Record
from campuspulse_recovery.storage.plans import _decode, _encode
from tests.storage.test_plans import FakeDocuments


NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
SITE = 'https://lms.astanait.edu.kz'


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def snapshot(now=NOW, **overrides):
    return MoodleSnapshot.model_validate(dict(site_url=SITE, user_id=42,
        fetched_at=now, courses=[], tasks=[], **overrides))


def service(documents, clock, fetch=None, **overrides):
    params = dict(owner_id='student-1', site_url=SITE, expected_user_id=42,
        clock=clock, fetch_snapshot=fetch)
    params.update(overrides)
    return MoodleSync(documents, **params)


def test_success_survives_store_restart_and_reader_needs_no_token(tmp_path):
    async def scenario():
        documents, clock = FakeDocuments(), Clock()
        calls = []
        async def fetch():
            calls.append(True)
            return snapshot(clock())
        writer = service(documents, clock, fetch)
        initial = await writer.read()
        assert initial.connected and initial.stale and initial.status == 'never_synced'
        state = await writer.refresh()
        assert state.status == 'ready' and state.last_success == NOW and state.last_attempt == NOW
        assert not state.stale and not state.syncing and state.snapshot.user_id == 42
        file = tmp_path / 'offline-store.bin'
        documents.checkpoint(file)
        restarted = service(FakeDocuments.restore(file), clock)
        assert await restarted.read() == state
        with pytest.raises(RuntimeError, match='no_refresh_authority'):
            await restarted.refresh()
        assert calls == [True]
    asyncio.run(scenario())


def test_force_is_rate_limited_and_normal_poll_honors_interval():
    async def scenario():
        documents, clock, calls = FakeDocuments(), Clock(), []
        async def fetch():
            calls.append(clock())
            return snapshot(clock())
        writer = service(documents, clock, fetch)
        await writer.refresh()
        clock.advance(seconds=59)
        await writer.refresh(force=True)
        assert len(calls) == 1
        clock.advance(seconds=1)
        await writer.refresh(force=True)
        assert len(calls) == 2
        clock.advance(minutes=14, seconds=59)
        await writer.refresh()
        assert len(calls) == 2
        clock.advance(seconds=1)
        await writer.refresh()
        assert len(calls) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,code', [
    (MoodleError('auth_failed'), 'auth_failed'),
    (MoodleError('unavailable'), 'unavailable'),
    (TimeoutError('secret-token-do-not-store'), 'sync_timeout'),
    (RuntimeError('secret-token-do-not-store'), 'sync_failed'),
])
def test_failure_keeps_last_good_cache_and_sanitizes_errors(failure, code):
    async def scenario():
        documents, clock = FakeDocuments(), Clock()
        async def good():
            return snapshot(clock())
        writer = service(documents, clock, good)
        original = await writer.refresh()
        clock.advance(minutes=31)
        async def bad():
            raise failure
        failed = service(documents, clock, bad)
        state = await failed.refresh()
        assert state.snapshot == original.snapshot and state.last_success == original.last_success
        assert state.last_attempt == clock() and state.error == code and state.stale
        assert state.status == 'error' and not state.syncing
        assert 'secret-token-do-not-store' not in repr(documents.data)
        # Poll attempts after failure are bounded too.
        assert await failed.refresh(force=True) == state
    asyncio.run(scenario())


def test_snapshot_freshness_is_upstream_timestamp_not_read_time():
    async def scenario():
        documents, clock = FakeDocuments(), Clock()
        async def fetch():
            return snapshot(clock())
        writer = service(documents, clock, fetch)
        await writer.refresh()
        clock.advance(minutes=30)
        state = await writer.read()
        assert state.stale and state.status == 'stale'
        assert state.snapshot.fetched_at == NOW and state.last_success == NOW
    asyncio.run(scenario())


def test_parallel_workers_only_one_fetch_and_commit():
    async def scenario():
        documents, clock, entered, release = FakeDocuments(), Clock(), asyncio.Event(), asyncio.Event()
        calls = []
        async def fetch():
            calls.append(True)
            entered.set()
            await release.wait()
            return snapshot(clock())
        first = service(documents, clock, fetch)
        second = service(documents, clock, fetch)
        task = asyncio.create_task(first.refresh())
        await entered.wait()
        concurrent = await second.refresh(force=True)
        assert concurrent.syncing and concurrent.status == 'syncing' and concurrent.snapshot is None
        assert len(calls) == 1
        release.set()
        assert (await task).status == 'ready'
    asyncio.run(scenario())


def test_expired_lease_is_recovered_and_old_result_is_fenced():
    async def scenario():
        documents, clock, entered, release = FakeDocuments(), Clock(), asyncio.Event(), asyncio.Event()
        async def slow():
            captured = snapshot(clock())
            entered.set()
            await release.wait()
            return captured
        first = service(documents, clock, slow)
        task = asyncio.create_task(first.refresh())
        await entered.wait()
        clock.advance(minutes=3)
        async def fast():
            return snapshot(clock())
        second = service(documents, clock, fast)
        latest = await second.refresh(force=True)
        assert latest.status == 'ready' and latest.snapshot.fetched_at == clock()
        release.set()
        assert await task == latest
        assert await first.read() == latest
    asyncio.run(scenario())


def test_process_cancellation_leaves_recoverable_lease():
    async def scenario():
        documents, clock, entered = FakeDocuments(), Clock(), asyncio.Event()
        async def interrupted():
            entered.set()
            await asyncio.Event().wait()
        first = service(documents, clock, interrupted)
        task = asyncio.create_task(first.refresh())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await first.read()).syncing
        clock.advance(minutes=3)
        async def fetch():
            return snapshot(clock())
        assert (await service(documents, clock, fetch).refresh(force=True)).status == 'ready'
    asyncio.run(scenario())


def test_disconnect_clears_cache_and_fences_an_inflight_job():
    async def scenario():
        documents, clock, entered, release = FakeDocuments(), Clock(), asyncio.Event(), asyncio.Event()
        calls = []
        async def fetch():
            calls.append(True)
            entered.set()
            await release.wait()
            return snapshot(clock())
        writer = service(documents, clock, fetch)
        task = asyncio.create_task(writer.refresh())
        await entered.wait()
        disconnected = await writer.disconnect()
        assert not disconnected.connected and disconnected.snapshot is None
        release.set()
        assert (await task).status == 'not_connected'
        clock.advance(hours=1)
        assert (await service(documents, clock, fetch).refresh()).status == 'not_connected'
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('override,error', [
    ({'user_id': 7}, 'identity_mismatch'),
    ({'site_url': 'https://another.example'}, 'identity_mismatch'),
    ({'fetched_at': NOW - timedelta(days=1)}, 'invalid_response'),
    ({'fetched_at': NOW + timedelta(days=1)}, 'invalid_response'),
])
def test_wrong_account_site_and_implausible_timestamps_never_persist(override, error):
    async def scenario():
        documents, clock = FakeDocuments(), Clock()
        async def fetch():
            value = dict(site_url=SITE, user_id=42, fetched_at=NOW, courses=[], tasks=[])
            value.update(override)
            return MoodleSnapshot.model_validate(value)
        state = await service(documents, clock, fetch).refresh()
        assert state.error == error and state.snapshot is None and state.last_success is None
    asyncio.run(scenario())


def test_owner_binding_uses_separate_keys_and_rejects_copied_foreign_document():
    async def scenario():
        documents, clock = FakeDocuments(), Clock()
        async def fetch():
            return snapshot(clock())
        original = service(documents, clock, fetch)
        await original.refresh()
        foreign = service(documents, clock, owner_id='student-2')
        assert (await foreign.read()).snapshot is None
        documents.data[foreign.path] = documents.data[original.path]
        with pytest.raises(BindingConflict, match='binding_mismatch'):
            await foreign.read()
        record = _decode(documents.data[original.path], 'moodle_connection', _Record)
        bad = record.model_copy(update={'snapshot': record.snapshot.model_copy(update={'user_id': 7})})
        documents.data[original.path] = _encode('moodle_connection', bad)
        with pytest.raises(BindingConflict, match='snapshot_binding_mismatch'):
            await original.read()
    asyncio.run(scenario())
