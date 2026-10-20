"""Synthetic transcription tests: authorization, limits, isolation and failure."""
import asyncio
from datetime import timedelta

from campuspulse_recovery.web.auth import CHALLENGE_COOKIE, CHALLENGE_CSRF_COOKIE, CSRF_COOKIE, SESSION_COOKIE
from campuspulse_recovery.web.speech import MAX_AUDIO_BYTES
from tests.storage.test_plans import run
from tests.web.test_chat import app, FakeAgent
from tests.web.test_approval import Setup, login


class SpeechAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.audio = []
        self.text = '  Завтра у меня две пары.  '

    async def transcribe(self, **values):
        self.audio.append(values)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise RuntimeError('secret provider details')
        return self.text


def test_audio_requires_session_csrf_valid_type_and_bounded_body():
    async def scenario():
        h, agent = Setup(), SpeechAgent()
        async with h.client(app(h, agent)) as client:
            path = '/api/chat/transcribe'
            assert (await client.post(path, content=b'a')).status_code == 403
            csrf = await login(client)
            headers = {**csrf, 'Content-Type': 'audio/webm;codecs=opus'}
            assert (await client.get('/api/chat')).json()['speech_available'] is True
            assert (await client.post(path, content=b'a', headers={'Content-Type': 'audio/webm'})).status_code == 403
            assert (await client.post(path, content=b'a', headers={**csrf, 'Content-Type': 'text/plain'})).status_code == 415
            assert (await client.post(path, content=b'', headers=headers)).status_code == 400
            assert (await client.post(path, content=b'x' * (MAX_AUDIO_BYTES + 1), headers=headers)).status_code == 413
            assert not agent.audio
            response = await client.post(path, content=b'fake audio', headers=headers)
            assert response.json() == {'text': 'Завтра у меня две пары.'}
            assert agent.audio == [{'audio': b'fake audio', 'mime_type': 'audio/webm', 'language': 'ru-RU'}]
            wrong_language = await client.post(path, content=b'a', headers={**headers, 'X-Speech-Language': 'invalid'})
            assert wrong_language.status_code == 400
            kazakh = await client.post(path, content=b'a', headers={**headers, 'X-Speech-Language': 'kk-KZ'})
            assert kazakh.status_code == 200 and agent.audio[-1]['language'] == 'kk-KZ'
            assert (await client.get('/api/chat')).json()['turns'] == []
            assert not any('fake audio' in str(value) for value in h.documents.data.values())
            agent.error = True
            failed = await client.post(path, content=b'a', headers=headers)
            assert failed.status_code == 503 and 'secret' not in failed.text
    run(scenario())


def test_parallel_transcription_rejected_and_session_rechecked():
    async def scenario():
        h, agent = Setup(), SpeechAgent()
        agent.release = asyncio.Event()
        async with h.client(app(h, agent)) as client:
            headers = {**await login(client), 'Content-Type': 'audio/mp4'}
            first = asyncio.create_task(client.post('/api/chat/transcribe', content=b'a', headers=headers))
            await agent.entered.wait()
            second = await client.post('/api/chat/transcribe', content=b'b', headers=headers)
            assert second.status_code == 429
            assert (await client.post('/auth/signout', json={}, headers=headers)).status_code == 200
            agent.release.set()
            assert (await first).status_code == 403
    run(scenario())


def test_new_failed_login_and_expired_challenge_preserve_chat_and_voice_session():
    async def scenario():
        h, agent = Setup(), SpeechAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            session_cookie = client.cookies[SESSION_COOKIE]
            csrf_cookie = client.cookies[CSRF_COOKIE]
            csrf_expiry = next(cookie.expires for cookie in client.cookies.jar if cookie.name == CSRF_COOKIE)
            challenge = (await client.get('/auth/challenge')).json()
            login_csrf_cookie = next(cookie for cookie in client.cookies.jar if cookie.name == CHALLENGE_CSRF_COOKIE)
            assert login_csrf_cookie.secure and login_csrf_cookie.has_nonstandard_attr('HttpOnly')
            assert login_csrf_cookie.get_nonstandard_attr('SameSite') == 'strict'
            assert login_csrf_cookie.path == '/' and login_csrf_cookie.expires < csrf_expiry
            assert client.cookies[SESSION_COOKIE] == session_cookie
            assert client.cookies[CSRF_COOKIE] == csrf_cookie
            assert next(cookie.expires for cookie in client.cookies.jar if cookie.name == CSRF_COOKIE) == csrf_expiry
            challenge_headers = {**headers, 'X-CSRF-Token': challenge['csrf_token']}
            assert (await client.post('/auth/signin', headers=challenge_headers,
                json={'credential': 'invalid-token'})).status_code == 403
            assert (await client.post('/auth/signin', headers=headers,
                json={'credential': 'synthetic-signed-token'})).status_code == 403
            for index in range(2):
                assert (await client.get('/auth/session')).status_code == 200
                payload = {'request_key': f'after-challenge-{index}', 'message': 'hello'}
                assert (await client.post('/api/chat', json=payload, headers=challenge_headers)).status_code == 403
                assert (await client.post('/api/chat/transcribe', content=b'voice',
                    headers={**challenge_headers, 'Content-Type': 'audio/webm'})).status_code == 403
                reply = await client.post('/api/chat', json=payload, headers=headers)
                assert reply.status_code == 200 and reply.json()['turns'][-1]['status'] == 'completed'
                voice = await client.post('/api/chat/transcribe', content=b'voice',
                    headers={**headers, 'Content-Type': 'audio/webm'})
                assert voice.status_code == 200 and voice.json()['text'] == agent.text.strip()
                h.clock.value += timedelta(minutes=6)
                if index == 0:
                    assert (await client.post('/auth/signin', headers=challenge_headers,
                        json={'credential': 'synthetic-signed-token'})).status_code == 403
                    del client.cookies[CHALLENGE_COOKIE]
                    del client.cookies[CHALLENGE_CSRF_COOKIE]
            assert len(agent.calls) == len(agent.audio) == 2
    run(scenario())


def test_unconfigured_speech_unavailable():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            assert (await client.post('/api/chat/transcribe', content=b'a', headers=headers)).status_code == 503
    run(scenario())
