"""Weekly publication uses the real encrypted proposal/consent/write state machine."""
from copy import deepcopy
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from campuspulse_recovery.domain.ports import BindingConflict
from tests.lms.test_multiuser import run_async
from tests.pitch_ui.test_connected_legacy_guards import NOW, fixture, live


async def saved_classes(plans, *, classes=None, until='2026-11-18'):
    value = {'owner_id': 'alice', 'schedule': {'valid_from': '2026-10-21',
        'valid_until': until, 'classes': classes if classes is not None else [
            {'subject': 'Алгоритмы', 'weekday': 2, 'start': '19:00', 'end': '20:20'},
            {'subject': 'Базы данных', 'weekday': 4, 'start': '10:00', 'end': '11:20'}]}}
    async def save(tx):
        tx.set('cp_class_schedules/alice', value)
    await plans.documents.documents.transact(save)
    return value


def full_source(calendar, horizon):
    events = []
    for event in deepcopy(calendar.events).values():
        if not event.get('recurrence'):
            events.append(event)
            continue
        start = datetime.fromisoformat(event['start']['dateTime']).astimezone(ZoneInfo(event['start']['timeZone']))
        end = datetime.fromisoformat(event['end']['dateTime']).astimezone(ZoneInfo(event['end']['timeZone']))
        rule = event['recurrence'][0]
        until = datetime.strptime(rule.split('UNTIL=')[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=ZoneInfo('UTC'))
        while start <= until:
            events.append({**event, 'id': event['id'] + '_' + start.strftime('%Y%m%d'),
                'recurringEventId': event['id'], 'start': {'dateTime': start.isoformat()},
                'end': {'dateTime': end.isoformat()}})
            start += timedelta(days=7)
            end += timedelta(days=7)
    return {'events': events, 'timezone': 'Asia/Almaty',
        'horizon': horizon, 'read_at': calendar.clock().isoformat()}


async def class_plan(plans, calendar):
    horizon = await plans.class_horizon('alice', class_timezone='Asia/Almaty')
    return await plans.propose_classes('alice', full_source(calendar, horizon), class_timezone='Asia/Almaty')


@run_async
async def test_weekly_classes_have_reviewable_recurrence_and_require_exact_consent(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    plan = await class_plan(plans, calendar)
    assert plan['purpose'] == 'classes'
    assert len(plan['blocks']) == 2
    algorithms = next(b for b in plan['blocks'] if b['title'] == 'Алгоритмы')
    assert algorithms['start'] == '2026-10-21T19:00:00+05:00'
    assert algorithms['recurrence'] == ['RRULE:FREQ=WEEKLY;UNTIL=20261118T140000Z']
    assert algorithms['timezone'] == 'Asia/Almaty'
    assert len(algorithms['occurrences']) == 5
    assert io.calls == 0
    with pytest.raises(Exception, match='calendar_exact_approval_required'):
        await plans.apply('alice', plan['plan_id'], session_check=live)
    await plans.approve('alice', plan['plan_id'], plan['hash'], 'session-one',
        source=full_source(calendar, plan['horizon']))
    async def read(owner, *, horizon=None):
        assert horizon == plan['horizon']
        return full_source(calendar, horizon)
    calendar.read = read
    result = await plans.apply('alice', plan['plan_id'], session_check=live)
    assert result['status'] == 'verified'
    assert io.calls == 2
    written = calendar.events[algorithms['event_id']]
    assert written['recurrence'] == algorithms['recurrence']
    assert written['start']['timeZone'] == written['end']['timeZone'] == 'Asia/Almaty'
    assert (await plans.apply('alice', plan['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 2


@run_async
async def test_conflict_on_later_week_rejects_class_proposal(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    calendar.events['personal'] = {'id': 'personal', 'etag': 'v1', 'summary': 'Appointment',
        'start': {'dateTime': '2026-11-04T19:30:00+05:00'},
        'end': {'dateTime': '2026-11-04T20:30:00+05:00'}}
    with pytest.raises(BindingConflict, match='calendar_plan_conflict'):
        await class_plan(plans, calendar)
    assert not await plans.list('alice') and io.calls == 0


@run_async
async def test_class_proposal_reuses_saved_intent_after_clock_moves_or_event_written(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    plan = await class_plan(plans, calendar)
    calendar.clock = lambda: NOW + timedelta(minutes=1)
    assert await class_plan(plans, calendar) == plan
    assert len(await plans.list('alice')) == 1 and io.calls == 0


@pytest.mark.parametrize('change', ['recurrence', 'timezone'])
@run_async
async def test_verification_checks_recurrence_and_timezone_not_only_first_occurrence(tmp_path, change):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    plan = await class_plan(plans, calendar)
    async def read(owner, *, horizon=None):
        return full_source(calendar, horizon or plan['horizon'])
    calendar.read = read
    await plans.approve('alice', plan['plan_id'], plan['hash'], 'session', source=await read('alice'))
    assert (await plans.apply('alice', plan['plan_id'], session_check=live))['status'] == 'verified'
    event = calendar.events[plan['blocks'][0]['event_id']]
    if change == 'recurrence':
        event['recurrence'] = ['RRULE:FREQ=WEEKLY;COUNT=99']
    else:
        event['start']['timeZone'] = 'UTC'
    assert (await plans.verify('alice', plan['plan_id'], session_check=live))['status'] != 'verified'
    assert io.calls == 2


@run_async
async def test_class_series_skips_past_occurrences_and_empty_or_expired_week_has_no_intent(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans, classes=[{'subject': 'Morning', 'weekday': 2, 'start': '09:00', 'end': '10:00'}])
    plan = await class_plan(plans, calendar)
    assert plan['blocks'][0]['start'] == '2026-10-28T09:00:00+05:00'
    assert all(datetime.fromisoformat(v['start']) >= NOW for v in plan['blocks'][0]['occurrences'])
    await saved_classes(plans, classes=[])
    with pytest.raises(BindingConflict, match='calendar_class_schedule_empty'):
        await class_plan(plans, calendar)
    await saved_classes(plans, until='2026-10-21', classes=[{'subject': 'Morning', 'weekday': 2, 'start': '09:00', 'end': '10:00'}])
    with pytest.raises(BindingConflict, match='calendar_class_schedule_empty'):
        await class_plan(plans, calendar)
    assert io.calls == 0


@run_async
async def test_changed_or_missing_series_instance_is_not_verified(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    plan = await class_plan(plans, calendar)
    changed = False
    async def read(owner, *, horizon=None):
        value = full_source(calendar, horizon or plan['horizon'])
        if changed:
            value['events'][0]['summary'] = 'Changed one occurrence'
        return value
    calendar.read = read
    await plans.approve('alice', plan['plan_id'], plan['hash'], 'session', source=await read('alice'))
    assert (await plans.apply('alice', plan['plan_id'], session_check=live))['status'] == 'verified'
    changed = True
    assert (await plans.verify('alice', plan['plan_id'], session_check=live))['status'] != 'verified'
    assert io.calls == 2


@run_async
async def test_calendar_read_explicit_horizon_is_bounded_and_preserved_by_session_grant(tmp_path):
    from campuspulse_recovery.cloud.connected_calendar import CalendarReadGrants
    from campuspulse_recovery.security.context import AccessDenied
    from tests.lms.test_multiuser import ORIGIN, Verifier, client, login
    from campuspulse_recovery.lms.multiuser import create_multiuser_app
    from campuspulse_recovery.storage.plans import _decode
    from campuspulse_recovery.web.auth import WebSession
    from tests.lms.test_local_google_calendar import calendar as local_calendar, connect
    calls = []
    cal = local_calendar(tmp_path / 'cal', lambda tokens, resource, opts: calls.append(opts) or {'items': []})
    await connect(cal)
    horizon = {'start': '2026-10-21T00:00:00+05:00', 'end': '2026-11-19T00:00:00+05:00'}
    source = await cal.read('alice', horizon=horizon)
    assert datetime.fromisoformat(calls[0]['timeMax']) == datetime.fromisoformat(horizon['end'])
    assert datetime.fromisoformat(source['horizon']['start']) == datetime.fromisoformat(horizon['start'])
    app = create_multiuser_app(origin=ORIGIN, client_id='client', directory=tmp_path / 'app', test_verifier=Verifier(), clock=lambda: NOW)
    async with client(app) as browser:
        await login(browser, 'alice')
    raw = app.state.documents
    index = (await raw.scan('cp_web_session_index'))[0]
    session = _decode(await raw.get(index['path']), 'web_session', WebSession)
    grants = CalendarReadGrants(raw, clock=lambda: NOW)
    ref = await grants.issue(session.binding.owner_id, session.binding.session_id, horizon=horizon)
    value = await grants.consume(ref)
    assert datetime.fromisoformat(value['horizon']['end']) == datetime.fromisoformat(horizon['end'])
    with pytest.raises(AccessDenied):
        await grants.consume(ref)
    for invalid in ({**horizon, 'end': '2027-11-19T00:00:00+05:00'},
            {**horizon, 'start': '2026-10-21T00:00:00'}, {**horizon, 'owner': 'bob'}):
        with pytest.raises((ValueError, AccessDenied)):
            await grants.issue(session.binding.owner_id, session.binding.session_id, horizon=invalid)
    with pytest.raises(AccessDenied):
        await grants.issue(session.binding.owner_id, session.binding.session_id,
            action='apply_plan', plan_id='a'*64, horizon=horizon)


def test_class_publication_endpoint_uses_saved_owner_week_and_full_period_before_approval():
    from tests.storage.test_plans import run
    from tests.web.test_approval import Setup, login
    from tests.web.test_managed_study_chat import composition
    from tests.pitch_ui.test_connected_study_flow import calendar as connected, save_weekly, weekly
    from campuspulse_recovery.web.calendar_plans import install_plan_routes
    async def scenario():
        h = Setup()
        cal, io = await connected(h)
        owner = h.config.student.trusted_user_id
        await save_weekly(h, weekly(h, valid_until='2026-11-18', classes=[
            {'subject': 'Saved class', 'weekday': 2, 'start': '19:00', 'end': '20:20'}]))
        reads = []
        class Tools:
            async def read(self, *, session, horizon=None):
                reads.append((session, horizon))
                return await cal.read(owner, horizon=horizon)
        cal.tools = lambda selected: Tools() if selected == owner else pytest.fail('foreign owner')
        app, _, _, _ = composition(h)
        install_plan_routes(app, auth=app.state.web_auth, calendar=cal)
        async with h.client(app) as browser:
            headers = await login(browser)
            first = await browser.post('/api/google-calendar/plans/classes', headers=headers, json={})
            assert first.status_code == 200, first.text
            assert first.json()['status'] == 'draft' and io.creates == []
            again = await browser.post('/api/google-calendar/plans/classes', headers=headers, json={})
            assert again.json() == first.json()
            items = (await browser.get('/api/google-calendar/plans')).json()['items']
            assert len(items) == 1
            plan = items[0]['plan']
            assert plan['purpose'] == 'classes'
            assert datetime.fromisoformat(reads[0][1]['end']) - datetime.fromisoformat(reads[0][1]['start']) > timedelta(days=20)
            choice = {'plan_id': plan['plan_id'], 'payload_hash': plan['hash']}
            approved = await browser.post('/api/google-calendar/plans/approve', headers=headers, json=choice)
            assert approved.status_code == 200, approved.text
            assert reads[-1][1] == plan['horizon']
            for path, value in [('/api/google-calendar/plans/classes?owner=other', {}),
                    ('/api/google-calendar/plans/classes', {'owner': 'other'})]:
                assert (await browser.post(path, headers=headers, json=value)).status_code >= 400
            assert io.creates == []
    run(scenario())


@run_async
async def test_class_series_lost_insert_response_is_reconciled_without_duplicate(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    plan = await class_plan(plans, calendar)
    async def read(owner, *, horizon=None):
        return full_source(calendar, horizon or plan['horizon'])
    calendar.read = read
    await plans.approve('alice', plan['plan_id'], plan['hash'], 'session', source=await read('alice'))
    io.timeout = True
    first = await plans.apply('alice', plan['plan_id'], session_check=live)
    assert first['status'] == 'unknown'
    assert io.calls == 1 and len(calendar.events) == 1
    assert (await plans.apply('alice', plan['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 2 and len(calendar.events) == 2
    assert await class_plan(plans, calendar) == plan
    assert io.calls == 2


@run_async
async def test_class_read_requires_full_recurrence_horizon_and_basis_stays_bound(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    short = await calendar.read('alice')
    with pytest.raises(BindingConflict, match='calendar_source_unavailable'):
        await plans.propose_classes('alice', short, class_timezone='Asia/Almaty')
    plan = await class_plan(plans, calendar)
    await saved_classes(plans, until='2026-11-17')
    with pytest.raises(BindingConflict, match='calendar_class_schedule_changed'):
        await plans.approve('alice', plan['plan_id'], plan['hash'], 'session',
            source=full_source(calendar, plan['horizon']))
    assert io.calls == 0


@run_async
async def test_class_slow_committed_insert_returns_unknown_within_dispatch_budget(tmp_path, monkeypatch):
    from campuspulse_recovery.integrations import connected_plans
    monkeypatch.setattr(connected_plans, 'DISPATCH_SECONDS', .2)
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    plan = await class_plan(plans, calendar)
    async def read(owner, *, horizon=None):
        return full_source(calendar, horizon or plan['horizon'])
    calendar.read = read
    await plans.approve('alice', plan['plan_id'], plan['hash'], 'session', source=await read('alice'))
    create = io.create
    async def stalled(binding, body):
        await create(binding, body)
        await asyncio.Event().wait()
    io.create = stalled
    result = await asyncio.wait_for(plans.apply('alice', plan['plan_id'], session_check=live), 1)
    assert result['status'] == 'unknown' and io.calls == 1
    io.create = create
    monkeypatch.setattr(connected_plans, 'DISPATCH_SECONDS', 25)
    assert (await plans.apply('alice', plan['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 2


@pytest.mark.parametrize('direct_apply', [False, True])
@run_async
async def test_unstarted_class_draft_can_refresh_after_first_start_passes(tmp_path, direct_apply):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    calendar.clock = lambda: datetime.fromisoformat('2026-10-21T18:55:00+05:00')
    old = await class_plan(plans, calendar)
    if direct_apply:
        await plans.approve('alice', old['plan_id'], old['hash'], 'session',
            source=full_source(calendar, old['horizon']))
    calendar.clock = lambda: datetime.fromisoformat('2026-10-21T19:01:00+05:00')
    async def read(owner, *, horizon=None):
        return full_source(calendar, horizon or old['horizon'])
    calendar.read = read
    if direct_apply:
        result = await plans.apply('alice', old['plan_id'], session_check=live)
        assert result['status'] == 'stale' and io.calls == 0
        assert not await plans.documents.get(plans.path('owned_events', old['blocks'][0]['event_id']))
    fresh = await class_plan(plans, calendar)
    assert fresh['plan_id'] != old['plan_id']
    algorithms = next(b for b in fresh['blocks'] if b['title'] == 'Алгоритмы')
    assert algorithms['start'] == '2026-10-28T19:00:00+05:00'
    _, old_state = await plans.get('alice', old['plan_id'])
    assert old_state['status'] == 'stale' and not old_state.get('approval')
    assert await class_plan(plans, calendar) == fresh
    await plans.approve('alice', fresh['plan_id'], fresh['hash'], 'session',
        source=full_source(calendar, fresh['horizon']))
    assert (await plans.apply('alice', fresh['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 2


@run_async
async def test_unstarted_class_draft_refreshes_changed_busy_snapshot(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    old = await class_plan(plans, calendar)
    calendar.events['beside-class'] = {'id': 'beside-class', 'etag': 'v1',
        'start': {'dateTime': '2026-10-21T21:00:00+05:00'},
        'end': {'dateTime': '2026-10-21T22:00:00+05:00'}}
    fresh = await class_plan(plans, calendar)
    assert fresh['plan_id'] != old['plan_id']
    assert fresh['snapshot'] != old['snapshot']
    await plans.approve('alice', fresh['plan_id'], fresh['hash'], 'session',
        source=full_source(calendar, fresh['horizon']))
    assert io.calls == 0


@run_async
async def test_class_refresh_never_replaces_intent_after_possible_write(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    calendar.clock = lambda: datetime.fromisoformat('2026-10-21T18:55:00+05:00')
    old = await class_plan(plans, calendar)
    async def read(owner, *, horizon=None):
        return full_source(calendar, horizon or old['horizon'])
    calendar.read = read
    await plans.approve('alice', old['plan_id'], old['hash'], 'session', source=await read('alice'))
    io.timeout = True
    assert (await plans.apply('alice', old['plan_id'], session_check=live))['status'] == 'unknown'
    calendar.clock = lambda: datetime.fromisoformat('2026-10-21T19:01:00+05:00')
    assert await class_plan(plans, calendar) == old
    assert (await plans.apply('alice', old['plan_id'], session_check=live))['status'] == 'verified'
    assert io.calls == 2


@run_async
async def test_class_refresh_rechecks_possible_write_in_same_transaction(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    old = await class_plan(plans, calendar)
    async def read(owner, *, horizon=None):
        return full_source(calendar, horizon or old['horizon'])
    calendar.read = read
    await plans.approve('alice', old['plan_id'], old['hash'], 'session', source=await read('alice'))
    changed = await read('alice')
    changed['events'].append({'id': 'brief-event', 'etag': 'v1',
        'start': {'dateTime': '2026-10-21T21:00:00+05:00'},
        'end': {'dateTime': '2026-10-21T22:00:00+05:00'}})
    before_save, continue_save = asyncio.Event(), asyncio.Event()
    save_proposal = plans.save_proposal
    async def held(*args, **kwargs):
        before_save.set()
        await continue_save.wait()
        return await save_proposal(*args, **kwargs)
    plans.save_proposal = held
    preparing = asyncio.create_task(plans.propose_classes('alice', changed, class_timezone='Asia/Almaty'))
    await asyncio.wait_for(before_save.wait(), 1)
    io.gate = (asyncio.Event(), asyncio.Event())
    applying = asyncio.create_task(plans.apply('alice', old['plan_id'], session_check=live))
    await asyncio.wait_for(io.gate[0].wait(), 1)
    continue_save.set()
    assert await preparing == old
    assert len(await plans.list('alice')) == 1
    io.gate[1].set()
    assert (await applying)['status'] == 'verified' and io.calls == 2


@run_async
async def test_expired_unstarted_class_approval_requires_new_preview_and_consent(tmp_path):
    plans, calendar, io = await fixture(tmp_path)
    await saved_classes(plans)
    old = await class_plan(plans, calendar)
    await plans.approve('alice', old['plan_id'], old['hash'], 'session',
        source=full_source(calendar, old['horizon']))
    calendar.clock = lambda: NOW + timedelta(minutes=31)
    fresh = await class_plan(plans, calendar)
    assert fresh['plan_id'] != old['plan_id']
    _, state = await plans.get('alice', fresh['plan_id'])
    assert state['status'] == 'draft' and not state.get('approval')
    _, old_state = await plans.get('alice', old['plan_id'])
    assert old_state['status'] == 'stale' and not old_state.get('approval')
    assert io.calls == 0
