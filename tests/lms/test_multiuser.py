import asyncio
from datetime import timedelta
from functools import wraps
import json

import httpx
import pytest

from campuspulse_recovery.lms.multiuser import create_multiuser_app
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.web.auth import CSRF_COOKIE, VerifiedSignIn

ORIGIN = 'https://127.0.0.1:8961'


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class Verifier:
    async def verify(self, token, *, nonce, now):
        if token not in ('alice:' + nonce, 'bob:' + nonce):
            raise AccessDenied('signin_rejected')
        return VerifiedSignIn(principal_id='https://accounts.google.com#' + token.split(':')[0],
            authenticated_at=now, expires_at=now + timedelta(hours=1))


def url(user):
    return ('https://lms.astanait.edu.kz/calendar/export_execute.php?userid=' + str(user)
        + '&authtoken=' + 'a'*32 + '&preset_what=all&preset_time=recentupcoming')


def fetch(request):
    user = request.url.params['userid']
    return httpx.Response(200, content=('BEGIN:VCALENDAR\r\nVERSION:2.0\r\n'
        'BEGIN:VEVENT\r\nUID:shared-event\r\nSUMMARY:Private homework ' + user + '\r\n'
        'DTSTART:20300101T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode())


class Agent:
    def __init__(self, calls):
        self.calls = calls

    async def reply(self, **arguments):
        self.calls.append(arguments)
        return 'Личный план: ' + json.dumps(arguments['sources'], ensure_ascii=False)


def application(directory, calls):
    return create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=directory, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        agent_factory=lambda: Agent(calls))


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
        headers={'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'})


async def login(client, name):
    challenge = (await client.get('/auth/challenge')).json()
    result = await client.post('/auth/signin', json={'credential': name + ':' + challenge['nonce']},
        headers={'X-CSRF-Token': challenge['csrf_token']})
    assert result.status_code == 200, result.text
    return result.json()['csrf_token']


async def post(client, path, value):
    return await client.post(path, json=value, headers={'X-CSRF-Token': client.cookies[CSRF_COOKIE]})


@run_async
async def test_two_accounts_isolated_status_plan_history_restart(tmp_path):
    calls = []
    app = application(tmp_path, calls)
    async with client(app) as alice, client(app) as bob:
        await login(alice, 'alice')
        await login(bob, 'bob')
        assert (await alice.get('/api/moodle')).json()['connected'] is False
        assert (await alice.get('/account')).status_code == 200
        assert (await alice.get('/')).headers['location'] == '/account'
        for person, user in ((alice, 42), (bob, 43)):
            result = await post(person, '/api/account/moodle', {'url': url(user)})
            assert result.status_code == 200, result.text
        data = (await alice.get('/api/moodle')).json()
        assert 'Моё подключение Moodle' in (await alice.get('/')).text
        assert data['snapshot']['tasks'][0]['title'] == 'Private homework 42'
        assert 'authtoken' not in json.dumps(data)
        assert '43' not in data['snapshot']['tasks'][0]['title']
        assert (await post(alice, '/api/moodle/task-status', {'task_id': 'shared-event', 'done': True})).status_code == 200
        assert (await bob.get('/api/moodle')).json()['snapshot']['tasks'][0]['local_status'] == 'active'
        for person, name in ((alice, 'alice'), (bob, 'bob')):
            response = await post(person, '/api/chat', {'request_key': 'request-' + name, 'message': 'Составь план ' + name})
            assert response.status_code == 200, response.text
            assert response.json()['turns'][0]['status'] == 'completed'
        assert len(calls) == 2
        assert 'Private homework 42' not in json.dumps(calls[0]['sources'])  # locally completed
        assert 'Private homework 43' in json.dumps(calls[1]['sources'])
        assert calls[0]['user_id'] != calls[1]['user_id']
        assert 'alice' not in (await bob.get('/api/chat')).text
        assert 'bob' not in (await alice.get('/api/chat')).text
        assert (await post(alice, '/auth/signout', {})).status_code == 200
        assert (await alice.get('/api/moodle')).status_code == 403
        await login(alice, 'alice')
        assert len((await alice.get('/api/chat')).json()['turns']) == 1
        assert (await alice.get('/api/moodle')).json()['snapshot']['tasks'][0]['local_status'] == 'done'
    # An actual new app and document adapter recover accounts and owner history.
    restarted = application(tmp_path, calls)
    async with client(restarted) as alice:
        await login(alice, 'alice')
        assert len((await alice.get('/api/chat')).json()['turns']) == 1
        assert (await alice.get('/api/moodle')).json()['snapshot']['tasks'][0]['local_status'] == 'done'
    assert b'authtoken' not in (tmp_path / 'documents.sqlite3').read_bytes()
    assert b'Status' not in (tmp_path / 'documents.sqlite3').read_bytes()


@run_async
async def test_auth_csrf_selectors_replay_and_rejected_feed(tmp_path):
    app = application(tmp_path, [])
    async with client(app) as alice, client(app) as anonymous:
        assert (await anonymous.get('/')).status_code == 307
        assert (await anonymous.get('/api/moodle')).status_code == 403
        assert (await anonymous.post('/api/account/moodle', json={'url': url(42)})).status_code == 403
        challenge = (await alice.get('/auth/challenge')).json()
        headers = {'X-CSRF-Token': challenge['csrf_token']}
        credential = {'credential': 'alice:' + challenge['nonce']}
        assert (await alice.post('/auth/signin', json={'credential': 'forged'}, headers=headers)).status_code == 403
        assert (await alice.post('/auth/signin', json=credential, headers=headers)).status_code == 200
        assert (await alice.post('/auth/signin', json=credential, headers=headers)).status_code == 403
        assert (await alice.post('/api/account/moodle', json={'url': url(42)})).status_code == 403
        for bad in ('https://evil.example/?userid=42', url(42).replace('https:', 'http:'), url(42) + '&owner=bob'):
            assert (await post(alice, '/api/account/moodle', {'url': bad})).status_code == 400
        assert (await post(alice, '/api/account/moodle', {'url': url(42), 'owner_id': 'bob'})).status_code == 400
        assert (await post(alice, '/api/account/moodle?owner=bob', {'url': url(42)})).status_code == 400
        assert (await post(alice, '/api/account/moodle', {'url': url(42)})).status_code == 200
        assert (await alice.get('/api/moodle?userid=43')).status_code == 400
        assert (await post(alice, '/api/account/moodle', {'url': 'https://evil.example'})).status_code == 400
        assert (await alice.get('/api/moodle')).json()['snapshot']['tasks'][0]['title'] == 'Private homework 42'
        assert 'calendar' in (await anonymous.get('/login')).text or 'календаря' in (await anonymous.get('/login')).text
