"""Local OAuth authorization, isolation, read-only MCP and failure boundaries."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from campuspulse_recovery.integrations.local_calendar import (
    CALLBACK, CalendarConnectionError, LocalCalendar, LocalCalendarTools, SCOPES, validate_client)
from campuspulse_recovery.lms.local_google_calendar import create_local_app
from campuspulse_recovery.lms.multiuser import create_multiuser_app
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from campuspulse_recovery.web.auth import digest
from tests.lms.test_multiuser import ORIGIN, Verifier, client, login, post, run_async

PROJECT = 'local-calendar-project'
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def config(origin=ORIGIN):
    return {'web': {'client_id': 'calendar-test.apps.googleusercontent.com',
        'client_secret': 'private-client-secret', 'project_id': PROJECT,
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': [origin + CALLBACK]}}


def calendar(tmp_path, api=None):
    docs = SqliteDocuments(tmp_path, project_id=PROJECT, database_id='local')
    return LocalCalendar(docs, client=config(), project_id=PROJECT, origin=ORIGIN, clock=lambda: NOW, api=api)


def event(name='exam', **extra):
    return {'id': name, 'etag': 'v1', 'summary': '<script>untrusted</script>',
        'start': {'dateTime': '2026-10-20T15:00:00+05:00'},
        'end': {'dateTime': '2026-10-20T16:00:00+05:00'}, **extra}


async def connect(cal, owner='alice'):
    await cal.save(owner, {'owner': owner, 'subject': 'alice-google-sub',
        'calendar_id': 'resolved-calendar@example.com', 'timezone': 'Asia/Qyzylorda',
        'tokens': {'refresh_token': 'private-refresh-token'}})


@pytest.mark.parametrize('key,value', [('project_id', 'other-project'),
    ('auth_uri', 'https://evil.example/auth'), ('token_uri', 'https://evil.example/token'),
    ('client_secret', ''), ('redirect_uris', ['https://evil.example/callback'])])
def test_downloaded_client_restricts_project_endpoints_and_redirect(key, value):
    bad = config()
    bad['web'][key] = value
    with pytest.raises(ValueError):
        validate_client(bad, project_id=PROJECT, origin=ORIGIN)


@run_async
async def test_setup_origin_csrf_storage_and_restart(tmp_path):
    app = create_local_app(directory=tmp_path, project_id=PROJECT, origin=ORIGIN)
    async with client(app) as browser:
        page = await browser.get('/google-calendar')
        assert page.status_code == 200 and ORIGIN + CALLBACK in page.text
        assert 'private-client-secret' not in page.text
        assert (await browser.post('/local/calendar/setup', json=config())).status_code == 403
        csrf = browser.cookies['__Host-cp-setup']
        assert (await browser.post('/local/calendar/setup', json=config(),
            headers={'Origin': 'https://evil.example', 'X-CSRF-Token': csrf})).status_code == 403
        response = await browser.post('/local/calendar/setup', json=config(), headers={'X-CSRF-Token': csrf})
        assert response.status_code == 200, response.text
        assert (await browser.get('/health')).json()['calendar_configured'] is True
        login_redirect = await browser.get('/google-calendar')
        assert login_redirect.status_code == 307 and login_redirect.headers['location'] == '/google-calendar/connect'
        assert (await browser.get('/auth/google-calendar/callback?code=secret')).status_code == 200
        assert (await browser.get('/health', headers={'Host': 'evil.example'})).status_code == 403
    assert b'private-client-secret' not in (tmp_path / 'documents.sqlite3').read_bytes()
    restarted = create_local_app(directory=tmp_path, project_id=PROJECT, origin=ORIGIN)
    async with client(restarted) as browser:
        assert (await browser.get('/health')).json()['calendar_configured'] is True


@run_async
async def test_read_mcp_pagination_and_no_owner_selectors(tmp_path):
    calls = []
    def api(tokens, resource, options):
        calls.append((resource, dict(options)))
        if 'pageToken' not in options:
            return {'items': [event()], 'nextPageToken': 'page-two'}
        return {'items': [event('all-day', start={'date': '2026-10-21'}, end={'date': '2026-10-22'},
            htmlLink='javascript:alert(1)', recurringEventId='parent', originalStartTime={'date': '2026-10-21'})]}
    cal = calendar(tmp_path, api)
    await connect(cal)
    tools = LocalCalendarTools(cal, 'alice')
    assert [t.name for t in await tools.server.list_tools()] == ['read_calendar']
    result = await tools.read()
    assert 'error' not in result, result
    assert len(result['events']) == 2 and result['events'][1]['htmlLink'] == ''
    assert result['events'][1]['end']['date'] == '2026-10-22'
    assert result['trace']['status'] == 'completed'
    assert result['trace']['result'] == {'event_count': 2}
    assert result['trace']['run_id'] and result['trace']['duration_ms'] >= 0
    assert all(c[1]['calendarId'] == 'resolved-calendar@example.com' for c in calls)
    assert calls[0][1]['singleEvents'] is True
    assert datetime.fromisoformat(calls[0][1]['timeMax']) - datetime.fromisoformat(calls[0][1]['timeMin']) == timedelta(hours=72)
    with pytest.raises(CalendarConnectionError):
        await cal.read('bob')
    assert b'private-refresh-token' not in (tmp_path / 'documents.sqlite3').read_bytes()


@pytest.mark.parametrize('page', [
    {'items': [event()], 'nextPageToken': 'cycle'}, {'items': [event(etag=None)]}, {'items': {}},
    {'items': [event(start={'dateTime': '2026-10-20T15:00:00'})]},
    {'items': [event(start={'date': '2026-10-22'}, end={'date': '2026-10-21'})]}])
@run_async
async def test_partial_or_malformed_reads_never_report_success(tmp_path, page):
    cal = calendar(tmp_path, lambda *_: page)
    await connect(cal)
    result = await LocalCalendarTools(cal, 'alice').read()
    assert result['error'] == 'calendar_read_failed'
    assert result['trace']['status'] == 'failed' and 'events' not in result


@run_async
async def test_read_cannot_restore_disconnected_credentials(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()
    def api(*_):
        entered.set()
        assert release.wait(5)
        return {'items': []}
    cal = calendar(tmp_path, api)
    await connect(cal)
    task = asyncio.create_task(cal.read('alice'))
    await asyncio.to_thread(entered.wait, 5)
    await cal.disconnect('alice')
    release.set()
    with pytest.raises(CalendarConnectionError, match='calendar_connection_changed'):
        await task
    assert await cal.status('alice') == {'connected': False}
    assert 'tokens' not in await cal.documents.get('cp_local_google_calendars/alice')


@run_async
async def test_routes_auth_csrf_isolation_and_callback_bridge(tmp_path):
    cal = calendar(tmp_path / 'calendar', lambda *_: {'items': [event()]})
    app = create_multiuser_app(origin=ORIGIN, client_id=config()['web']['client_id'],
        directory=tmp_path / 'users', test_verifier=Verifier(), local_google_calendar=cal)
    async with client(app) as alice, client(app) as bob, client(app) as anonymous:
        await login(alice, 'alice')
        await login(bob, 'bob')
        assert (await anonymous.get('/api/account/google-calendar')).status_code == 403
        assert (await alice.get('/api/account/google-calendar')).json() == {'connected': False}
        assert (await alice.post('/api/account/google-calendar/disconnect', json={})).status_code == 403
        assert (await post(alice, '/api/account/google-calendar/disconnect?owner=bob', {})).status_code == 400
        assert (await alice.get('/api/google-calendar/events?calendarId=other')).status_code == 400
        from campuspulse_recovery.lms.multiuser import account_config
        owner = account_config('https://accounts.google.com#alice').student.trusted_user_id
        await connect(cal, owner)
        assert (await alice.get('/api/google-calendar/events')).status_code == 200
        assert (await bob.get('/api/account/google-calendar')).json() == {'connected': False}
        assert (await bob.get('/api/google-calendar/events')).status_code == 503
        page = await alice.get('/settings')
        assert '/#calendar' in page.text
        site = await alice.get('/')
        assert site.status_code == 200
        assert 'data-view="calendar" href="#calendar"' in site.text
        assert 'id="google-calendar"' in site.text
        assert '/assets/google-calendar.js' in site.text
        callback = await anonymous.get(CALLBACK, headers={'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'})
        assert callback.status_code == 200 and 'no-referrer' == callback.headers['referrer-policy']
        calendar_page = await alice.get('/google-calendar')
        assert 'private-refresh-token' not in calendar_page.text
        assert "script-src 'self'" in calendar_page.headers['content-security-policy']
        assert "connect-src 'self'" in calendar_page.headers['content-security-policy']
        assert (await post(alice, '/api/account/google-calendar/disconnect', {})).status_code == 200


class FakeFlow:
    code_verifier = 'pkce-secret'
    def authorization_url(self, **kwargs):
        self.nonce = kwargs['nonce']
        return 'https://accounts.google.com/o/oauth2/auth?state=' + 'x'*32, 'x'*32
    def fetch_token(self, **kwargs):
        assert kwargs['code'] == 'authorization-code'
    @property
    def credentials(self):
        class Tokens:
            refresh_token = 'private-refresh-token'
            id_token = 'signed-id-token'
            def has_scopes(self, scopes):
                return tuple(scopes) == SCOPES
            def to_json(self):
                return json.dumps({'refresh_token': self.refresh_token})
        return Tokens()


@pytest.mark.parametrize('bad_nonce,bad_subject', [(False, False), (True, False), (False, True)])
@run_async
async def test_oauth_state_nonce_subject_and_replay(tmp_path, monkeypatch, bad_nonce, bad_subject):
    cal = calendar(tmp_path, lambda *_: {'primary': True, 'accessRole': 'owner',
        'id': 'actual-user-calendar@example.com', 'timeZone': 'Asia/Qyzylorda'})
    flow = FakeFlow()
    monkeypatch.setattr(cal, 'flow', lambda **kwargs: flow)
    monkeypatch.setattr('google.oauth2.id_token.verify_oauth2_token', lambda *a, **kw: {
        'sub': 'bob' if bad_subject else 'alice', 'nonce': 'wrong' if bad_nonce else flow.nonce})
    await cal.start(owner='alice', session='session-one')
    args = dict(owner='alice', session='session-one', principal='https://accounts.google.com#alice',
        state='x'*32, code='authorization-code')
    with pytest.raises(ValueError):
        await cal.exchange(**{**args, 'session': 'other-session'})
    if bad_nonce or bad_subject:
        with pytest.raises(CalendarConnectionError):
            await cal.exchange(**args)
    else:
        result = await cal.exchange(**args)
        assert result['calendar_id'] == 'actual-user-calendar@example.com'
    with pytest.raises(ValueError):
        await cal.exchange(**args)


@run_async
async def test_expired_oauth_state_does_not_exchange(tmp_path, monkeypatch):
    cal = calendar(tmp_path)
    monkeypatch.setattr(cal, 'flow', lambda **kwargs: FakeFlow())
    await cal.start(owner='alice', session='session-one')
    cal.clock = lambda: NOW + timedelta(minutes=6)
    with pytest.raises(ValueError, match='expired'):
        await cal.exchange(owner='alice', session='session-one', principal='https://accounts.google.com#alice',
            state='x'*32, code='authorization-code')


@pytest.mark.parametrize('wrong_subject', [False, True])
@run_async
async def test_top_level_oauth_uses_real_signin_boundary_and_consumes_challenge(tmp_path, monkeypatch, wrong_subject):
    cal = calendar(tmp_path / 'calendar', lambda *_: {'items': []})
    monkeypatch.setattr(cal, 'flow', lambda **kwargs: FakeFlow())
    async def exchange(**kwargs):
        assert kwargs['return_id_token'] is True and kwargs['principal'] == 'prelogin'
        return {'owner': kwargs['owner'], 'subject': 'alice',
            '_signin_id_token': ('bob:' if wrong_subject else 'alice:') + kwargs['session'],
            'calendar_id': 'resolved-calendar@example.com', 'timezone': 'Asia/Qyzylorda',
            'tokens': {'refresh_token': 'private-refresh-token'}}
    monkeypatch.setattr(cal, 'exchange', exchange)
    app = create_multiuser_app(origin=ORIGIN, client_id=config()['web']['client_id'],
        directory=tmp_path / 'users', test_verifier=Verifier(), local_google_calendar=cal)
    async with client(app) as browser:
        assert (await browser.get('/google-calendar/connect')).status_code == 200
        assert (await browser.get('/google-calendar/connect', headers={
            'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Dest': 'document'})).status_code == 200
        assert (await browser.post('/auth/google-calendar/signin/start', json={}, headers={
            'Sec-Fetch-Site': 'cross-site'})).status_code == 403
        assert (await browser.post('/auth/google-calendar/signin/start', json={})).status_code == 403
        challenge = (await browser.get('/auth/challenge')).json()
        assert (await post(browser, '/auth/google-calendar/signin/start', {})).status_code == 200
        assert browser.cookies['__Host-cp-calendar-flow'] == 'signin'
        response = await post(browser, '/auth/google-calendar/signin/complete', {
            'state': 'x'*32, 'code': 'authorization-code'})
        if wrong_subject:
            assert response.status_code == 403
            assert (await browser.get('/api/account/google-calendar')).status_code == 403
        else:
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'authenticated'
            assert 'private-refresh-token' not in response.text
            assert '__Host-cp-calendar-flow' not in browser.cookies
            assert (await browser.get('/api/account/google-calendar')).json() == {'connected': True}
            assert (await browser.get('/api/google-calendar/events')).status_code == 200
            assert (await post(browser, '/auth/google-calendar/signin/complete', {
                'state': 'x'*32, 'code': 'authorization-code'})).status_code == 403
