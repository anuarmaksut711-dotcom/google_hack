from datetime import UTC, datetime
import json

import pytest

from campuspulse_recovery.lms.manual_schedule import WeeklySchedule
from campuspulse_recovery.lms.multiuser import create_multiuser_app
from tests.lms.test_multiuser import client, login, post, run_async, url, Verifier, ORIGIN, Agent, fetch
import httpx


VALUE = {'valid_from': '2026-10-19', 'valid_until': '2026-11-01', 'classes': [
    {'subject': 'Алгоритмы', 'weekday': 1, 'start': '12:00', 'end': '14:00'}]}
NOW = datetime(2026, 10, 20, 6, tzinfo=UTC)


def app_at(directory, calls):
    return create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=directory, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        clock=lambda: NOW, agent_factory=lambda: Agent(calls))


@run_async
async def test_saved_weekly_classes_owner_restart_chat_and_removed_mydu(tmp_path):
    calls = []
    app = app_at(tmp_path, calls)
    async with client(app) as alice, client(app) as bob, client(app) as anonymous:
        assert (await anonymous.get('/api/account/classes')).status_code == 403
        await login(alice, 'alice'); await login(bob, 'bob')
        page = (await alice.get('/account')).text
        assert 'Моё расписание занятий' not in page and 'Microsoft' not in page and 'My DU' not in page
        assert 'classes-fields' not in page
        assert '/api/account/classes' not in (await alice.get('/assets/account.js')).text
        assert (await alice.get('/api/account/my-du')).status_code == 404
        assert (await alice.post('/api/account/classes', json=VALUE)).status_code == 403
        assert (await post(alice, '/api/account/classes', {**VALUE, 'owner_id': 'bob'})).status_code == 400
        assert (await alice.get('/api/account/classes?owner=bob')).status_code == 400
        result = await post(alice, '/api/account/classes', VALUE)
        assert result.status_code == 200, result.text
        assert (await bob.get('/api/account/classes')).json()['connected'] is False
        assert (await post(alice, '/api/account/moodle', {'url': url(42)})).status_code == 200
        response = await post(alice, '/api/chat', {'request_key': 'manual-classes', 'message': 'Составь план с учётом пар'})
        assert response.status_code == 200, response.text
        sources = calls[0]['sources']
        assert 'Алгоритмы' in '\n'.join(sources['calendar'])
        schedule = sources['class_schedule']
        assert schedule['status'] == 'ready' and len(schedule['busy']) == 2
        assert schedule['busy'][0]['start'] == '2026-10-20T07:00:00Z'
    async with client(app_at(tmp_path, [])) as alice:
        await login(alice, 'alice')
        saved = (await alice.get('/api/account/classes')).json()['schedule']
        assert saved['classes'][0]['subject'] == 'Алгоритмы'
        assert (await post(alice, '/api/account/classes', {**VALUE, 'classes': []})).status_code == 200
        assert (await alice.get('/api/account/classes')).json()['schedule']['classes'] == []


@pytest.mark.parametrize('change', [
    {'valid_until': '2026-10-18'}, {'valid_until': '2027-12-31'},
    {'classes': [{**VALUE['classes'][0], 'end': '11:00'}]},
    {'classes': [{**VALUE['classes'][0], 'weekday': 7}]},
    {'classes': [{**VALUE['classes'][0], 'subject': '  '}]},
    {'classes': VALUE['classes'] * 2},
])
def test_reject_invalid_schedule(change):
    with pytest.raises(ValueError):
        WeeklySchedule.model_validate({**VALUE, **change})


@run_async
async def test_changed_moodle_feed_refresh_preserves_done_status(tmp_path):
    from datetime import timedelta
    from campuspulse_recovery.lms.calendar_feed import CalendarFeed
    moment = NOW
    title = 'Original'
    def transport(request):
        return httpx.Response(200, content=('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n'
            'UID:stable-event\r\nSUMMARY:' + title + '\r\nDTSTART:20261022T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode())
    feed = CalendarFeed(url=url(42), cache_file=tmp_path/'feed.json', owner_id='alice', user_id=42,
                        transport=httpx.MockTransport(transport), clock=lambda: moment)
    await feed.refresh()
    await feed.set_local_status('stable-event', True)
    title = 'Changed by teacher'
    moment += timedelta(minutes=5)
    await feed.refresh()
    task = feed.state().snapshot.tasks[0]
    assert task.title == title and task.local_status == 'done'


@run_async
async def test_schedule_changed_during_generation_blocks_conflicting_plan(tmp_path):
    other = None
    class ChangingAgent:
        async def reply(self, **kwargs):
            changed = {**VALUE, 'classes': [{**VALUE['classes'][0], 'start': '14:00', 'end': '15:00'}]}
            assert (await post(other, '/api/account/classes', changed)).status_code == 200
            return json.dumps({'kind': 'plan', 'message': '', 'blocks': [
                {'title': 'Study', 'start': '2026-10-20T14:00:00+05:00', 'end': '2026-10-20T15:00:00+05:00'}]})
    app = create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=tmp_path, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        clock=lambda: NOW, agent_factory=ChangingAgent)
    async with client(app) as alice, client(app) as second:
        other = second
        await login(alice, 'alice'); await login(second, 'alice')
        await post(alice, '/api/account/moodle', {'url': url(42)})
        await post(alice, '/api/account/classes', VALUE)
        response = await post(alice, '/api/chat', {'request_key': 'changed-schedule', 'message': 'Составь план'})
        assert response.status_code == 200
        assert 'пересечение с парой' in response.json()['turns'][-1]['response']
