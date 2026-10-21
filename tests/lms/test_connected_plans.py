import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from campuspulse_recovery.cloud.connected_calendar import EncryptedCalendarDocuments
from campuspulse_recovery.domain.ports import BindingConflict
from campuspulse_recovery.integrations.connected_plans import ConnectedPlans, WRITE_SCOPE, validate_blocks
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from tests.lms.test_multiuser import run_async

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


class Calendar:
    def __init__(self, documents):
        self.documents = documents
        self.events = {}
        self.clock = lambda: NOW

    async def read(self, owner):
        return {'events': list(deepcopy(self.events).values()), 'timezone': 'Asia/Almaty',
                'horizon': {'start': NOW.isoformat(), 'end': (NOW+timedelta(hours=72)).isoformat()}}


class IO:
    def __init__(self, calendar):
        self.calendar, self.calls, self.timeout, self.fail_at = calendar, 0, False, None
        self.gate = None

    async def get(self, binding, event_id):
        return deepcopy(self.calendar.events.get(event_id))

    async def create(self, binding, body):
        self.calls += 1
        if self.gate:
            self.gate[0].set()
            await self.gate[1].wait()
        if self.calls == self.fail_at:
            raise RuntimeError('network')
        event = {**deepcopy(body), 'etag': 'v1', 'htmlLink': 'https://calendar.google.com/calendar/event?id=example'}
        self.calendar.events[body['id']] = event
        if self.timeout:
            self.timeout = False
            raise TimeoutError()
        return deepcopy(event)


async def fixture(tmp_path):
    raw = SqliteDocuments(tmp_path, project_id='test-project', database_id='test')
    docs = EncryptedCalendarDocuments(raw, Fernet.generate_key())
    calendar = Calendar(docs)
    async def setup(tx):
        await tx.get('cp_local_google_calendars/alice')
        tx.create('cp_local_google_calendars/alice', {'owner': 'alice', 'connected': True,
            'calendar_id': 'alice@example.test', 'generation': 1, 'tokens': {'scopes': [WRITE_SCOPE]}})
    await docs.transact(setup)
    io = IO(calendar)
    plans = ConnectedPlans(calendar, io=io)
    return plans, calendar, io


def blocks(count=1):
    return [{'title': 'Встреча '+str(i), 'start': (NOW+timedelta(hours=2+i)).isoformat(),
             'end': (NOW+timedelta(hours=2+i, minutes=30)).isoformat()} for i in range(count)]


async def live():
    pass


async def draft(plans, calendar, count=1, key='request-1'):
    return await plans.propose('alice', key, blocks(count), await calendar.read('alice'))


async def approve(plans, plan):
    await plans.approve('alice', plan['plan_id'], plan['hash'], 'session-1')


@run_async
async def test_exact_approval_owner_isolation_and_immutable_duplicate(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    p = await draft(plans, calendar)
    assert await draft(plans, calendar) == p
    assert len(await plans.list('alice')) == 1
    with pytest.raises(AccessDenied):
        await plans.get('bob', p['plan_id'])
    with pytest.raises(AccessDenied):
        await plans.apply('alice', p['plan_id'], session_check=live)
    with pytest.raises(BindingConflict):
        await plans.approve('alice', p['plan_id'], '0'*64, 'session')
    assert io.calls == 0
    await approve(plans, p)
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'verified'
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 1


@run_async
async def test_timeout_after_success_reconciles_without_duplicate(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    p = await draft(plans, calendar)
    await approve(plans, p)
    io.timeout = True
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'unknown'
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 1


@run_async
async def test_partial_failure_preserves_completed_and_resumes(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    p = await draft(plans, calendar, 2)
    await approve(plans, p)
    io.fail_at = 2
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'partial'
    assert len(calendar.events) == 1
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 3 and len(calendar.events) == 2


@run_async
async def test_new_calendar_event_invalidates_approval_before_write(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    p = await draft(plans, calendar)
    await approve(plans, p)
    calendar.events['external'] = {'id': 'external', 'etag': 'new', 'summary': 'External',
        'start': {'dateTime': (NOW+timedelta(hours=5)).isoformat()},
        'end': {'dateTime': (NOW+timedelta(hours=6)).isoformat()}}
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'stale'
    assert io.calls == 0


@run_async
async def test_concurrent_apply_and_other_plan_are_blocked_durably(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    p = await draft(plans, calendar)
    p2 = await draft(plans, calendar, key='request-2')
    await approve(plans, p)
    await approve(plans, p2)
    io.gate = (asyncio.Event(), asyncio.Event())
    pending = asyncio.create_task(plans.apply('alice', p['plan_id'], session_check=live))
    await io.gate[0].wait()
    for plan in (p, p2):
        with pytest.raises(BindingConflict):
            await plans.apply('alice', plan['plan_id'], session_check=live)
    io.gate[1].set()
    assert (await pending)['status'] == 'verified'


@run_async
async def test_failed_final_verification_is_not_success_and_edited_event_not_overwritten(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    p = await draft(plans, calendar)
    await approve(plans, p)
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] == 'verified'
    event = calendar.events[p['blocks'][0]['event_id']]
    event['summary'] = 'Изменено пользователем'
    event['etag'] = 'v2'
    assert (await plans.verify('alice', p['plan_id'], session_check=live))['status'] == 'applied_unverified'
    assert (await plans.apply('alice', p['plan_id'], session_check=live))['status'] != 'verified'
    assert event['summary'] == 'Изменено пользователем' and io.calls == 1


@run_async
async def test_conflict_and_changed_binding_rejected(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    calendar.events['busy'] = {'id': 'busy', 'etag': 'v1', 'start': {'dateTime': blocks()[0]['start']},
                               'end': {'dateTime': blocks()[0]['end']}}
    with pytest.raises(BindingConflict):
        await draft(plans, calendar)
    calendar.events.clear()
    p = await draft(plans, calendar)
    await approve(plans, p)
    async def disconnect(tx):
        saved = await tx.get('cp_local_google_calendars/alice')
        tx.set('cp_local_google_calendars/alice', {**saved, 'generation': 2})
    await plans.documents.transact(disconnect)
    with pytest.raises(BindingConflict):
        await plans.apply('alice', p['plan_id'], session_check=live)
    assert io.calls == 0


@run_async
async def test_reconnect_same_calendar_keeps_plan_binding_but_disconnect_invalidates(tmp_path):
    from tests.lms.test_local_google_calendar import calendar as make_calendar
    from campuspulse_recovery.integrations.connected_plans import ConnectedPlans
    cal = make_calendar(tmp_path)
    connection = {'owner': 'alice', 'subject': 'google-alice', 'calendar_id': 'alice@example.test',
        'timezone': 'Asia/Almaty', 'tokens': {'refresh_token': 'synthetic-one'}}
    await cal.save('alice', connection)
    store = ConnectedPlans(cal)
    original = await store.binding('alice')
    reference = {'calendar': original['calendar_id'], 'generation': original['binding_generation']}
    await cal.save('alice', {**connection, 'tokens': {'refresh_token': 'synthetic-two'}})
    reconnected = await store.binding('alice', reference)
    assert reconnected['generation'] == 2 and reconnected['binding_generation'] == 1
    await cal.disconnect('alice')
    await cal.save('alice', connection)
    with pytest.raises(BindingConflict):
        await store.binding('alice', reference)


@pytest.mark.parametrize('change', ['past', 'too_far', 'overlap', 'missing_offset', 'negative'])
def test_deterministic_time_validation(change):
    raw = blocks()
    if change == 'past': raw[0]['start'] = (NOW-timedelta(hours=1)).isoformat()
    if change == 'too_far': raw[0]['end'] = (NOW+timedelta(days=4)).isoformat()
    if change == 'overlap': raw *= 2
    if change == 'missing_offset': raw[0]['start'] = '2026-10-21T14:00:00'
    if change == 'negative': raw[0]['end'] = raw[0]['start']
    with pytest.raises((ValueError, BindingConflict)):
        validate_blocks(raw, now=NOW)
