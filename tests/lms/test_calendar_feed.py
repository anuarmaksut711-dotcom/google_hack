"""Personal calendar provenance, bounded fetch, stale cache and local auth."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import re

import httpx
import pytest

from campuspulse_recovery.lms.calendar_feed import CalendarFeed, parse_feed, validate_feed_url
from campuspulse_recovery.lms.local_calendar import create_local_calendar_app

NOW = datetime(2026, 10, 19, 15, tzinfo=UTC)
URL = 'https://lms.astanait.edu.kz/calendar/export_execute.php?userid=42&authtoken=' + 'a' * 40 + '&preset_what=all&preset_time=recentupcoming'
ICS = b'''BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:deadline@example\r
SUMMARY:Assignment 5 is due\r
DESCRIPTION:Line one\\nLine two\r
DTSTART:20261024T185900Z\r
DTEND:20261024T185900Z\r
CATEGORIES:Web Technologies | Teacher\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:attendance@example\r
SUMMARY:Attendance (Group)\r
DTSTART:20261024T130000Z\r
END:VEVENT\r
END:VCALENDAR\r
'''


def test_feed_preserves_dates_and_does_not_invent_submission_or_activity_ids():
    snapshot = parse_feed(ICS, user_id=42, fetched_at=NOW)
    assert snapshot.event_count == 2 and snapshot.attendance_count == 1
    task, = snapshot.tasks
    assert task.id == 'deadline@example'
    assert task.due_at.isoformat() == '2026-10-24T18:59:00+00:00'
    assert task.description == 'Line one\nLine two'
    assert task.course_title == 'Web Technologies | Teacher'
    assert task.url is None and task.submission_status == 'unknown'
    assert 'grade_value' not in task.model_dump()


def test_local_completion_is_durable_reversible_and_distinct_from_moodle_status(tmp_path):
    async def scenario():
        now = [datetime(2026, 10, 24, 18, 58, tzinfo=UTC)]
        path, cache = tmp_path/'url.txt', tmp_path/'cache.json'
        path.write_text(URL)
        feed = CalendarFeed(url_file=path,cache_file=cache,owner_id='owner',user_id=42,
            clock=lambda:now[0],transport=httpx.MockTransport(lambda r:httpx.Response(200,content=ICS)))
        await feed.refresh()
        assert feed.state().snapshot.tasks[0].view_group == 'upcoming'
        now[0] += timedelta(minutes=1)
        assert feed.state().snapshot.tasks[0].view_group == 'past'
        await feed.set_local_status('deadline@example',True)
        task=feed.state().snapshot.tasks[0]
        assert task.view_group=='completed' and task.local_status=='done' and task.submission_status=='unknown'
        restarted=CalendarFeed(url_file=path,cache_file=cache,owner_id='owner',user_id=42,clock=lambda:now[0])
        assert restarted.state().snapshot.tasks[0].view_group=='completed'
        assert 'done' not in json.loads(cache.read_text())['snapshot']['tasks'][0]['submission_status']
        await restarted.set_local_status('deadline@example',False)
        assert restarted.state().snapshot.tasks[0].view_group=='past'
        with pytest.raises(ValueError,match='unknown_moodle_calendar_event'):
            await restarted.set_local_status('foreign-event',True)
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', [URL.replace('https:', 'http:'), URL.replace('lms.astanait.edu.kz', 'evil.test'),
    URL.replace('userid=42', 'userid=43'), URL + '&userid=42', URL + '#fragment',
    URL.replace('preset_what=all', 'preset_what=user'), URL + '&unexpected=1'])
def test_secret_url_is_bound_to_site_account_and_allowed_query(bad):
    with pytest.raises(ValueError, match='^invalid_moodle_calendar_url$'):
        validate_feed_url(bad, 42)


@pytest.mark.parametrize('raw', [b'<html>login</html>', ICS.replace(b'UID:deadline@example', b'RRULE:FREQ=DAILY\r\nUID:deadline@example'),
    ICS.replace(b'DTSTART:20261024T185900Z', b'DTSTART:invalid')])
def test_bad_or_recurring_feeds_fail_closed(raw):
    with pytest.raises(ValueError, match='^invalid_moodle_calendar$'):
        parse_feed(raw, user_id=42, fetched_at=NOW)


def test_fetch_cooldown_cached_restart_and_failure_keep_last_success(tmp_path):
    async def scenario():
        now = [NOW]
        responses, calls = [200], []
        def handler(request):
            calls.append(request)
            return httpx.Response(responses[0], content=ICS if responses[0] == 200 else b'')
        url_file, cache = tmp_path / 'url.txt', tmp_path / 'cache.json'
        url_file.write_text(URL)
        feed = CalendarFeed(url_file=url_file, cache_file=cache, owner_id='owner',
            user_id=42, clock=lambda: now[0], transport=httpx.MockTransport(handler))
        await feed.refresh()
        assert feed.state().last_success == NOW and len(calls) == 1
        assert 'authtoken' not in cache.read_text()
        await feed.refresh()
        assert len(calls) == 1
        now[0] += timedelta(minutes=31)
        responses[0] = 302
        await feed.refresh()
        assert feed.state().stale and feed.state().error == 'unavailable'
        assert feed.state().last_success == NOW and len(feed.state().snapshot.tasks) == 1
        restored = CalendarFeed(url_file=url_file, cache_file=cache, owner_id='owner',
            user_id=42, clock=lambda: now[0])
        assert restored.state().last_success == NOW
        url_file.write_text(URL.replace('a' * 40, 'b' * 40))
        rebound = CalendarFeed(url_file=url_file, cache_file=cache, owner_id='owner', user_id=42)
        assert rebound.state().snapshot is None
    asyncio.run(scenario())


def test_local_routes_require_session_csrf_and_never_expose_feed_key(tmp_path):
    async def scenario():
        path = tmp_path / 'url.txt'
        path.write_text(URL)
        origin = 'https://127.0.0.1:8959'
        app, feed = create_local_calendar_app(origin=origin, url_file=path,
            cache_file=tmp_path / 'cache.json', user_id=42,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=ICS)))
        await feed.refresh()
        headers = {'Origin': origin, 'Sec-Fetch-Site': 'same-origin'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers=headers) as client:
            assert (await client.get('/api/moodle')).status_code == 403
            challenge = (await client.get('/auth/challenge')).json()
            script = (await client.get('/local-entry.js')).text
            access = json.loads(re.search(r'credential:("[^"]+")', script)[1])
            response = await client.post('/auth/signin', json={'credential': access},
                headers={'X-CSRF-Token': challenge['csrf_token']})
            assert response.status_code == 200
            response = await client.get('/api/moodle')
            assert response.status_code == 200
            assert response.json()['source_kind'] == 'calendar_feed'
            assert len(response.json()['snapshot']['tasks']) == 1
            assert all(secret not in response.text + script for secret in ('authtoken', 'a' * 40, 'expected_user_id', 'owner_id', 'user_id'))
            assert (await client.post('/api/moodle/sync', json={})).status_code == 403
            csrf = client.cookies.get('__Host-cp-csrf')
            assert (await client.post('/api/moodle/sync', json={}, headers={'X-CSRF-Token': csrf})).status_code == 200
            status_body={'task_id':'deadline@example','done':True}
            assert (await client.post('/api/moodle/task-status',json=status_body)).status_code==403
            marked=await client.post('/api/moodle/task-status',json=status_body,headers={'X-CSRF-Token':csrf})
            assert marked.status_code==200
            assert marked.json()['snapshot']['tasks'][0]['view_group']=='completed'
            assert marked.json()['snapshot']['tasks'][0]['submission_status']=='unknown'
            assert (await client.get('/api/moodle?userid=43')).status_code == 400
            assert (await client.get('/api/moodle', headers={'Origin':'https://evil.test'})).status_code == 403
    asyncio.run(scenario())


def test_local_chat_receives_real_feed_context_but_never_url_description_or_fake_workload(tmp_path):
    async def scenario():
        class RecordingAgent:
            calls = []
            async def reply(self, **kwargs):
                self.calls.append(kwargs)
                return 'Укажи свободные часы и оставшееся время на Assignment 5.'
        agent = RecordingAgent()
        path = tmp_path / 'url.txt'
        path.write_text(URL)
        origin = 'https://127.0.0.1:8959'
        app, feed = create_local_calendar_app(origin=origin, url_file=path,
            cache_file=tmp_path / 'cache.json', user_id=42, chat_agent=agent,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=ICS)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                headers={'Origin':origin, 'Sec-Fetch-Site':'same-origin'}) as client:
            challenge = (await client.get('/auth/challenge')).json()
            script = (await client.get('/local-entry.js')).text
            access = json.loads(re.search(r'credential:("[^"]+")', script)[1])
            await client.post('/auth/signin',json={'credential':access},
                headers={'X-CSRF-Token':challenge['csrf_token']})
            assert (await client.get('/api/chat')).json()['available']
            response = await client.post('/api/chat',json={'request_key':'real_feed_plan','message':'Составь план'},
                headers={'X-CSRF-Token':client.cookies.get('__Host-cp-csrf')})
            assert response.status_code == 200 and response.json()['turns'][0]['status'] == 'completed'
            sources = json.dumps(agent.calls[0]['sources'], ensure_ascii=False)
            assert 'Assignment 5 is due' in sources and '2026-10-24T23:59:00+05:00' in sources
            assert 'Asia/Qyzylorda' in sources and 'статус сдачи неизвестен' in sources
            assert not any(value in sources for value in ('authtoken','a'*40,'Line one','Attendance','Teacher'))
            html = (await client.get('/')).text
            assert 'data-chat-mode="calendar_advice"' in html
            status=await client.post('/api/moodle/task-status',json={'task_id':'deadline@example','done':True},
                headers={'X-CSRF-Token':client.cookies.get('__Host-cp-csrf')})
            assert status.status_code==200
            await client.post('/api/chat',json={'request_key':'after_mark_done','message':'Составь план'},
                headers={'X-CSRF-Token':client.cookies.get('__Host-cp-csrf')})
            next_sources=json.dumps(agent.calls[-1]['sources'],ensure_ascii=False)
            assert 'Assignment 5 is due' not in next_sources
            assert 'исключены из планирования' in next_sources
    asyncio.run(scenario())
