"""Authenticated CSRF recovery with synthetic identity, storage and model IO."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest

from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.web.auth import CSRF_COOKIE, SESSION_COOKIE, WebSession, digest
from tests.storage.test_plans import run
from tests.web.test_approval import ORIGIN, Setup, login
from tests.web.test_chat import app
from tests.web.test_speech import SpeechAgent

BOOTSTRAP = {'X-CSRF-Bootstrap': '1'}


def saved_session(setup, client):
    path = 'cp_web_sessions/' + digest(client.cookies[SESSION_COOKIE])
    return path, _decode(setup.documents.data[path], 'web_session', WebSession)


@pytest.mark.parametrize('cookie_state', ['missing', 'mismatched'])
def test_bootstrap_restores_chat_voice_and_signout_without_extending_session(cookie_state):
    async def scenario():
        setup, agent = Setup(), SpeechAgent()
        async with setup.client(app(setup, agent)) as client:
            old_headers = await login(client)
            path, original = saved_session(setup, client)
            session_cookie = client.cookies[SESSION_COOKIE]
            setup.clock.value += timedelta(minutes=7)
            del client.cookies[CSRF_COOKIE]
            if cookie_state == 'mismatched':
                client.cookies.set(CSRF_COOKIE, 'expired-login-token', domain='campus.test', path='/')
            snapshot = deepcopy(setup.documents.data)
            metadata = await client.get('/auth/session')
            assert metadata.status_code == 200 and 'csrf_token' not in metadata.json()
            assert 'set-cookie' not in metadata.headers and setup.documents.data == snapshot
            response = await client.get('/auth/session', headers=BOOTSTRAP)
            assert response.status_code == 200, response.text
            token = response.json()['csrf_token']
            assert token == client.cookies[CSRF_COOKIE] and token != old_headers['X-CSRF-Token']
            assert client.cookies[SESSION_COOKIE] == session_cookie
            repaired = _decode(setup.documents.data[path], 'web_session', WebSession)
            assert repaired.model_dump(exclude={'csrf_digest'}) == original.model_dump(exclude={'csrf_digest'})
            assert repaired.csrf_digest == digest(token)
            assert token not in repr(setup.documents.data)
            cookie = SimpleCookie(response.headers['set-cookie'])[CSRF_COOKIE]
            assert int(cookie['max-age']) == int((original.expires_at - setup.clock()).total_seconds())
            assert cookie['secure'] and cookie['samesite'] == 'strict' and cookie['path'] == '/'
            assert response.headers['cache-control'] == 'no-store'
            assert 'access-control-allow-origin' not in response.headers
            payload = {'request_key': 'repaired-chat', 'message': 'hello'}
            assert (await client.post('/api/chat', headers=old_headers, json=payload)).status_code == 403
            headers = {'Origin': ORIGIN, 'X-CSRF-Token': token}
            chat = await client.post('/api/chat', headers=headers, json=payload)
            assert chat.status_code == 200 and chat.json()['turns'][-1]['status'] == 'completed'
            voice = await client.post('/api/chat/transcribe', content=b'voice',
                headers={**headers, 'Content-Type': 'audio/webm'})
            assert voice.status_code == 200 and voice.json()['text'] == agent.text.strip()
            assert (await client.post('/auth/signout', headers=headers)).status_code == 200
            assert SESSION_COOKIE not in client.cookies and CSRF_COOKIE not in client.cookies
    run(scenario())


def test_bootstrap_reuses_valid_csrf_without_writes_or_cookie_refresh():
    async def scenario():
        setup = Setup()
        async with setup.client() as client:
            headers = await login(client)
            snapshot = deepcopy(setup.documents.data)
            response = await client.get('/auth/session', headers=BOOTSTRAP)
            assert response.status_code == 200
            assert response.json()['csrf_token'] == headers['X-CSRF-Token']
            assert setup.documents.data == snapshot
            assert 'set-cookie' not in response.headers
    run(scenario())


@pytest.mark.parametrize('headers', [
    {'X-CSRF-Bootstrap': ''},
    {'X-CSRF-Bootstrap': '2'},
    {'X-CSRF-Bootstrap': '1, 1'},
    {**BOOTSTRAP, 'Origin': 'https://foreign.test'},
    {**BOOTSTRAP, 'Sec-Fetch-Site': 'cross-site'},
    {**BOOTSTRAP, 'Sec-Fetch-Site': 'same-site'},
    {**BOOTSTRAP, 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'},
    {**BOOTSTRAP, 'Sec-Fetch-Mode': 'no-cors'},
    {**BOOTSTRAP, 'Host': 'foreign.test'},
])
def test_bootstrap_rejects_invalid_intent_and_foreign_requests(headers):
    async def scenario():
        setup = Setup()
        async with setup.client() as client:
            await login(client)
            del client.cookies[CSRF_COOKIE]
            snapshot = deepcopy(setup.documents.data)
            response = await client.get('/auth/session', headers=headers)
            assert response.status_code == 403
            assert 'csrf_token' not in response.json() and 'set-cookie' not in response.headers
            assert setup.documents.data == snapshot
    run(scenario())


@pytest.mark.parametrize('state', ['anonymous', 'expired', 'revoked', 'foreign_owner', 'duplicate_session', 'duplicate_csrf'])
def test_bootstrap_never_repairs_invalid_or_ambiguous_sessions(state):
    async def scenario():
        setup = Setup()
        async with setup.client() as client:
            await login(client)
            path, session = saved_session(setup, client)
            token = client.cookies[CSRF_COOKIE]
            del client.cookies[CSRF_COOKIE]
            headers = dict(BOOTSTRAP)
            if state == 'anonymous':
                del client.cookies[SESSION_COOKIE]
            elif state == 'expired':
                setup.clock.value = session.expires_at
            elif state == 'revoked':
                setup.documents.data[path] = _encode('web_session', session.model_copy(update={'revoked': True}))
            elif state == 'foreign_owner':
                changed = session.model_copy(update={'binding': session.binding.model_copy(update={'owner_id': 'other'})})
                setup.documents.data[path] = _encode('web_session', changed)
            elif state == 'duplicate_session':
                value = client.cookies[SESSION_COOKIE]
                headers['Cookie'] = f'{SESSION_COOKIE}={value}; {SESSION_COOKIE}={value}'
            else:
                headers['Cookie'] = (f'{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}; '
                    f'{CSRF_COOKIE}={token}; {CSRF_COOKIE}={token}')
            snapshot = deepcopy(setup.documents.data)
            response = await client.get('/auth/session', headers=headers)
            assert response.status_code == 403
            assert 'csrf_token' not in response.json() and 'set-cookie' not in response.headers
            assert setup.documents.data == snapshot
    run(scenario())


@pytest.mark.parametrize('change,status', [('revoked', 403), ('expired', 403), ('binding', 409), ('csrf', 409)])
def test_bootstrap_transaction_rechecks_current_session_before_repair(change, status):
    async def scenario():
        setup = Setup()
        async with setup.client() as client:
            await login(client)
            path, session = saved_session(setup, client)
            del client.cookies[CSRF_COOKIE]
            transact = setup.documents.transact
            replacement = session

            async def raced(callback):
                nonlocal replacement
                if change == 'revoked':
                    replacement = session.model_copy(update={'revoked': True})
                elif change == 'expired':
                    setup.clock.value = session.expires_at
                elif change == 'binding':
                    replacement = session.model_copy(update={
                        'binding': session.binding.model_copy(update={'session_id': 'switched-session'})})
                else:
                    replacement = session.model_copy(update={'csrf_digest': digest('another-bootstrap')})
                setup.documents.data[path] = _encode('web_session', replacement)
                return await transact(callback)

            setup.documents.transact = raced
            response = await client.get('/auth/session', headers=BOOTSTRAP)
            assert response.status_code == status
            assert 'csrf_token' not in response.json() and 'set-cookie' not in response.headers
            assert _decode(setup.documents.data[path], 'web_session', WebSession) == replacement
    run(scenario())


def test_concurrent_bootstraps_do_not_overwrite_the_winning_token():
    async def scenario():
        setup = Setup()
        service = setup.app()
        async with setup.client(service) as first, setup.client(service) as second:
            await login(first)
            path, session = saved_session(setup, first)
            del first.cookies[CSRF_COOKIE]
            second.cookies.update(first.cookies)
            get = setup.documents.get
            ready, reads = asyncio.Event(), 0

            async def paired_get(selected_path):
                nonlocal reads
                value = await get(selected_path)
                if selected_path == path:
                    reads += 1
                    if reads == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 3)
                return value

            setup.documents.get = paired_get
            responses = await asyncio.gather(first.get('/auth/session', headers=BOOTSTRAP),
                second.get('/auth/session', headers=BOOTSTRAP))
            assert sorted(response.status_code for response in responses) == [200, 409]
            winner = next(response for response in responses if response.status_code == 200)
            loser = next(response for response in responses if response.status_code == 409)
            saved = _decode(setup.documents.data[path], 'web_session', WebSession)
            assert saved.csrf_digest == digest(winner.json()['csrf_token'])
            assert saved.expires_at == session.expires_at
            assert 'csrf_token' not in loser.json() and 'set-cookie' not in loser.headers
    run(scenario())


def test_multiuser_bootstrap_repairs_only_the_authenticated_account(tmp_path):
    from tests.lms.test_multiuser import application, client, login as login_account, post

    async def scenario():
        service = application(tmp_path, [])
        async with client(service) as alice, client(service) as bob:
            await login_account(alice, 'alice')
            bob_token = await login_account(bob, 'bob')
            alice_session = (await alice.get('/auth/session')).json()['session_id']
            bob_path = 'cp_web_sessions/' + digest(bob.cookies[SESSION_COOKIE])
            bob_before = await service.state.documents.get(bob_path)
            del alice.cookies[CSRF_COOKIE]
            response = await alice.get('/auth/session', headers=BOOTSTRAP)
            assert response.status_code == 200
            assert response.json()['session_id'] == alice_session
            assert response.json()['csrf_token'] == alice.cookies[CSRF_COOKIE]
            assert response.json()['csrf_token'] != bob_token
            assert await service.state.documents.get(bob_path) == bob_before
            for person in (alice, bob):
                chat = await post(person, '/api/chat', {'request_key': 'after-bootstrap', 'message': 'hello'})
                assert chat.status_code == 200 and chat.json()['turns'][-1]['status'] == 'completed'
    run(scenario())
