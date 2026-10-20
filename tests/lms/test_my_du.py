import asyncio
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from campuspulse_recovery.lms.multiuser import create_multiuser_app
from campuspulse_recovery.lms.my_du import MyDuClient, MyDuUnavailable, TermSettings, credentials, parse_week
from tests.lms.test_multiuser import Agent, ORIGIN, Verifier, client, fetch, login, post, run_async, url

NOW = datetime(2026, 10, 20, 6, tzinfo=UTC)
SETTINGS = {'study_year': 2026, 'term': 1, 'first_week_start': '2026-09-07'}


class Provider:
    def __init__(self):
        self.calls = []
        self.unavailable = False
        self.expired = False
        self.refresh_rejected = False
        self.invalid_week = False
        self.on_schedule = None

    async def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == 'my-du.astanait.edu.kz'
        path = request.url.path
        data = json.loads(request.content) if request.content else None
        if path.endswith('/external-login'):
            assert data['provider'] == 'microsoft'
            return httpx.Response(200, json={'access_token': 'secret-access-' + data['token'], 'refresh_token': 'secret-refresh'})
        if path.endswith('/auth/refresh'):
            if self.refresh_rejected:
                return httpx.Response(401)
            self.expired = False
            return httpx.Response(200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh'})
        assert 'access_token=' in request.headers['cookie']
        if self.expired:
            return httpx.Response(401)
        if self.unavailable:
            return httpx.Response(503, text='secret-provider-details')
        if '/settings/' in path:
            return httpx.Response(200, json={'value': '10'})
        assert path.endswith('/classSchedule/student/me/search')
        week = int(next(f['value'] for f in data['filters'] if f['id'] == 'weekNumber'))
        if self.on_schedule:
            await self.on_schedule()
        return httpx.Response(200, json=week_payload(week, invalid=self.invalid_week))


def week_payload(week, invalid=False):
    return {'studyYear': 2026, 'term': 1, 'weekNumber': week + int(invalid), 'slots': [
        {'weekDay': {'id': 2}, 'items': [{'uid': 1, 'classTime': {'id': 1, 'title': '12:00 – 14:00'},
            'subjectName': 'Algorithms', 'teacherName': 'DO NOT SEND TEACHER', 'online': False}]}]}


def application(tmp_path, provider, calls=None, clock=lambda: NOW):
    return create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=tmp_path, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        agent_factory=lambda: Agent(calls if calls is not None else []),
        my_du_transport=httpx.MockTransport(provider), clock=clock)


async def connection(user, code='alice-code'):
    response = await post(user, '/api/account/my-du/start', {})
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()['url']).query)
    return {**SETTINGS, 'callback_url': 'https://my-du.astanait.edu.kz/login#' + urlencode({
        'state': query['state'][0], 'code': code})}


def test_normalize_week_rejects_wrong_dates_and_preserves_busy_times():
    settings = TermSettings(**SETTINGS)
    meetings = parse_week(week_payload(7), settings, 7)
    assert meetings[0].interval.start == datetime(2026, 10, 20, 7, tzinfo=UTC)
    assert meetings[0].interval.end == datetime(2026, 10, 20, 9, tzinfo=UTC)
    with pytest.raises(MyDuUnavailable):
        parse_week(week_payload(8), settings, 7)
    for title in ('14:00-12:00', '25:00-26:00', '12:00-12:00', 'no time'):
        payload = week_payload(7)
        payload['slots'][0]['items'][0]['classTime']['title'] = title
        with pytest.raises(MyDuUnavailable):
            parse_week(payload, settings, 7)
    with pytest.raises(ValueError):
        TermSettings(**{**SETTINGS, 'first_week_start': '2026-09-08'})


@run_async
async def test_transport_redirect_limits_and_cookie_injection():
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': 'https://evil.example/'})
    adapter = MyDuClient(transport=httpx.MockTransport(redirect))
    with pytest.raises(MyDuUnavailable):
        await adapter.request('/test', tokens={'access_token': 'private-access', 'refresh_token': 'private-refresh'})
    assert len(calls) == 1 and calls[0].url.host == 'my-du.astanait.edu.kz'
    for token in ('x; injected=yes', 'x\r\nOther: value', 'non-ascii-ю', ''):
        with pytest.raises(MyDuUnavailable):
            credentials({'access_token': token, 'refresh_token': 'refresh'})
    adapter = MyDuClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b' ' * 1_500_001)))
    with pytest.raises(MyDuUnavailable):
        await adapter.request('/test')


@run_async
async def test_connect_chat_busy_intervals_restart_isolation_and_disconnect(tmp_path):
    provider, calls = Provider(), []
    app = application(tmp_path, provider, calls)
    async with client(app) as alice, client(app) as bob:
        await login(alice, 'alice')
        await login(bob, 'bob')
        assert (await alice.get('/api/account/my-du')).json()['connected'] is False
        value = await connection(alice)
        response = await post(alice, '/api/account/my-du/connect', value)
        assert response.status_code == 200, response.text
        state = response.json()
        assert state['connected'] and not state['stale'] and state['error'] is None
        assert 'secret-' not in response.text and 'TEACHER' not in response.text
        assert len(state['snapshot']['classes']) == 2
        assert (await bob.get('/api/account/my-du')).json()['connected'] is False
        assert (await post(alice, '/api/account/moodle', {'url': url(42)})).status_code == 200
        response = await post(alice, '/api/chat', {'request_key': 'plan-with-classes', 'message': 'Составь план с учётом пар'})
        assert response.status_code == 200, response.text
        calendar = '\n'.join(calls[0]['sources']['calendar'])
        assert 'Algorithms' in calendar and '2026-10-20T12:00:00+05:00' in calendar
        assert '2026-10-20T14:00:00+05:00' in calendar
        assert 'Не назначай работу на пересекающееся время' in calendar
        assert 'TEACHER' not in calendar and 'secret-' not in calendar
        assert 'Расписание занятий и свободные часы пользователя не подключены' not in calendar
    restarted = application(tmp_path, provider)
    async with client(restarted) as alice:
        await login(alice, 'alice')
        assert (await alice.get('/api/account/my-du')).json()['snapshot'] == state['snapshot']
        assert (await post(alice, '/api/account/my-du/disconnect', {})).json()['connected'] is False
    async with client(app) as alice:
        await login(alice, 'alice')
        assert (await alice.get('/api/account/my-du')).json()['connected'] is False
        assert (await post(alice, '/api/account/my-du/connect', value)).status_code == 400
    # Local persistence encrypts credentials, pending nonces and class subjects.
    raw = (tmp_path / 'documents.sqlite3').read_bytes()
    assert b'secret-access' not in raw and b'secret-refresh' not in raw and b'Algorithms' not in raw


@run_async
async def test_csrf_callback_owner_replay_and_expiry(tmp_path):
    provider = Provider()
    now = NOW
    app = application(tmp_path, provider, clock=lambda: now)
    async with client(app) as alice, client(app) as bob, client(app) as anonymous:
        assert (await anonymous.get('/api/account/my-du')).status_code == 403
        await login(alice, 'alice')
        await login(bob, 'bob')
        assert (await alice.post('/api/account/my-du/start', json={})).status_code == 403
        assert (await post(alice, '/api/account/my-du/start', {'owner_id': 'bob'})).status_code == 400
        assert (await alice.get('/api/account/my-du?owner=bob')).status_code == 400
        value = await connection(alice)
        assert (await post(bob, '/api/account/my-du/connect', value)).status_code == 400
        for callback in (value['callback_url'].replace('my-du.astanait.edu.kz', 'evil.example'),
                         value['callback_url'].replace('https:', 'http:'),
                         value['callback_url'] + '&state=duplicate'):
            assert (await post(alice, '/api/account/my-du/connect', {**value, 'callback_url': callback})).status_code == 400
        assert not provider.calls
        assert (await post(alice, '/api/account/my-du/connect', value)).status_code == 200
        assert (await post(alice, '/api/account/my-du/connect', value)).status_code == 400
        value = await connection(alice)
        now += timedelta(minutes=11)
        assert (await post(alice, '/api/account/my-du/connect', value)).status_code == 400


@run_async
async def test_refresh_rotation_failure_staleness_and_reconnect(tmp_path):
    provider, calls, now = Provider(), [], NOW
    app = application(tmp_path, provider, calls, clock=lambda: now)
    async with client(app) as alice:
        await login(alice, 'alice')
        assert (await post(alice, '/api/account/my-du/connect', await connection(alice))).status_code == 200
        provider.expired = True
        now += timedelta(minutes=2)
        response = await post(alice, '/api/account/my-du/refresh', {})
        assert response.json()['error'] is None
        assert any(r.url.path.endswith('/auth/refresh') for r in provider.calls)
        snapshot = response.json()['snapshot']
        provider.unavailable = True
        now += timedelta(minutes=2)
        response = await post(alice, '/api/account/my-du/refresh', {})
        assert response.json()['error'] == 'unavailable' and response.json()['snapshot'] == snapshot
        assert 'secret-provider-details' not in response.text
        assert (await post(alice, '/api/account/moodle', {'url': url(42)})).status_code == 200
        assert (await post(alice, '/api/chat', {'request_key': 'failed-schedule', 'message': 'Составь план'})).status_code == 200
        calendar = '\n'.join(calls[-1]['sources']['calendar'])
        assert 'не считай часы свободными' in calendar and 'Algorithms' not in calendar
        provider.unavailable = False
        provider.expired = provider.refresh_rejected = True
        now += timedelta(minutes=2)
        assert (await post(alice, '/api/account/my-du/refresh', {})).json()['error'] == 'reconnect'
        before = len(provider.calls)
        now += timedelta(minutes=2)
        assert (await post(alice, '/api/account/my-du/refresh', {})).json()['error'] == 'reconnect'
        assert len(provider.calls) == before


@run_async
async def test_concurrent_refresh_and_disconnect_fence(tmp_path):
    provider, now = Provider(), NOW
    app = application(tmp_path, provider, clock=lambda: now)
    replica = application(tmp_path, provider, clock=lambda: now)
    async with client(app) as alice, client(replica) as other:
        await login(alice, 'alice')
        await login(other, 'alice')
        assert (await post(alice, '/api/account/my-du/connect', await connection(alice))).status_code == 200
        now += timedelta(minutes=2)
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        provider.on_schedule = wait
        running = asyncio.create_task(post(alice, '/api/account/my-du/refresh', {}))
        await asyncio.wait_for(entered.wait(), timeout=5)
        before = len(provider.calls)
        assert (await post(other, '/api/account/my-du/refresh', {})).status_code == 200
        assert len(provider.calls) == before  # shared lease, no second upstream sync
        assert (await post(other, '/api/account/my-du/disconnect', {})).json()['connected'] is False
        release.set()
        assert (await running).status_code == 409
        assert (await alice.get('/api/account/my-du')).json()['connected'] is False


@run_async
async def test_wrong_week_does_not_become_free_time_and_account_form(tmp_path):
    provider = Provider()
    provider.invalid_week = True
    app = application(tmp_path, provider)
    async with client(app) as alice:
        await login(alice, 'alice')
        response = await post(alice, '/api/account/my-du/connect', await connection(alice))
        assert response.json()['error'] == 'unavailable' and response.json()['snapshot'] is None
        page = await alice.get('/account')
        assert 'Подключить расписание пар' in page.text and 'Понедельник первой учебной недели' in page.text
        assert "script-src 'self'" in page.headers['content-security-policy']
        script = await alice.get('/assets/account.js')
        assert 'textContent=item.subject' in script.text


@run_async
async def test_real_adk_runner_receives_classes_and_exclusion_instruction(tmp_path):
    from campuspulse_recovery.agent.conversation import AdkConversationAgent, ConversationTarget
    from tests.web.test_chat_agent import SyntheticModel
    provider = Provider()
    model = SyntheticModel(model='gemini-synthetic')
    app = create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=tmp_path, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        my_du_transport=httpx.MockTransport(provider), clock=lambda: NOW,
        agent_factory=lambda: AdkConversationAgent(ConversationTarget(provider='gemini_api',
            model='gemini-synthetic'), model_factory=lambda: model, profile='calendar_advice'))
    async with client(app) as alice:
        await login(alice, 'alice')
        assert (await post(alice, '/api/account/moodle', {'url': url(42)})).status_code == 200
        assert (await post(alice, '/api/account/my-du/connect', await connection(alice))).json()['error'] is None
        count = len(provider.calls)
        response = await post(alice, '/api/chat', {'request_key': 'adk-plan', 'message': 'План на день с парами'})
        assert response.json()['turns'][0]['status'] == 'completed'
        request, = model._requests
        instruction = str(request.config.system_instruction)
        assert '2026-10-20T12:00:00+05:00' in instruction and 'Algorithms' in instruction
        assert 'не должны пересекаться с парами' in instruction
        assert 'secret-access' not in instruction and not request.tools_dict
        assert len(provider.calls) == count  # recent successful schedule is reused


@run_async
async def test_outdated_term_is_not_a_successful_empty_schedule(tmp_path):
    provider = Provider()
    app = application(tmp_path, provider)
    async with client(app) as alice:
        await login(alice, 'alice')
        value = await connection(alice)
        value.update(first_week_start='2026-01-05')
        response = await post(alice, '/api/account/my-du/connect', value)
        assert response.json()['error'] == 'unavailable'
        assert response.json()['snapshot'] is None


@run_async
async def test_login_accepts_created_credentials_and_reports_safe_rejections(tmp_path, capsys):
    provider = Provider()
    reject = False
    async def transport(request):
        if request.url.path.endswith('/external-login'):
            if reject:
                return httpx.Response(401, text='secret-auth-code-and-provider-body')
            return httpx.Response(201, json={'access_token': 'created-access', 'refresh_token': 'created-refresh'})
        return await provider(request)
    app = application(tmp_path, transport)
    async with client(app) as alice:
        await login(alice, 'alice')
        value = await connection(alice)
        response = await post(alice, '/api/account/my-du/connect', value)
        assert response.status_code == 200 and response.json()['connected']
        assert (await post(alice, '/api/account/my-du/connect', value)).json()['error'] == 'my_du_login_expired'
        reject = True
        response = await post(alice, '/api/account/my-du/connect', await connection(alice))
        assert response.status_code == 502 and response.json() == {'error': 'my_du_login_rejected'}
        output = capsys.readouterr().out
        assert 'provider_rejected' in output and '401' in output
        assert 'secret-' not in output and 'secret-' not in response.text
