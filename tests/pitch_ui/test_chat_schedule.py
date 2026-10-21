"""Real chat/editor routes with synthetic model text; no provider or Calendar writes."""
import asyncio
import json
import re

import httpx

from campuspulse_recovery.lms.multiuser import create_multiuser_app
from tests.lms.test_multiuser import client, login, post, run_async, Verifier, ORIGIN, fetch
from tests.lms.test_manual_schedule import NOW
from tests.domain.test_chat_schedule import DRAFT


class TimetableAgent:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None

    async def reply(self, **values):
        self.calls.append(values)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return json.dumps({'kind': 'classes', 'message': '', 'blocks': [], 'timetable': DRAFT})


def application(directory, agent):
    return create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
        directory=directory, test_verifier=Verifier(), transport=httpx.MockTransport(fetch),
        clock=lambda: NOW, agent_factory=lambda: agent)


@run_async
async def test_chat_to_review_draft_is_owner_bound_idempotent_and_requires_explicit_save(tmp_path):
    agent = TimetableAgent()
    app = application(tmp_path, agent)
    async with client(app) as alice, client(app) as bob:
        await login(alice, 'alice'); await login(bob, 'bob')
        request = {'request_key': 'chat-classes', 'message': 'Вторник: Алгоритмы с 12:00 до 14:00'}
        response = await post(alice, '/api/chat', request)
        assert response.status_code == 200, response.text
        turn = response.json()['turns'][-1]
        assert turn['status'] == 'completed'
        match = re.search(r'/planning#draft=([0-9a-f]{64})', turn['response'])
        assert match, turn['response']
        draft_path = '/api/account/classes/drafts/' + match[1]
        assert (await alice.get('/api/account/classes')).json()['connected'] is False
        draft = await alice.get(draft_path)
        assert draft.status_code == 200 and draft.json()['draft'] == DRAFT
        assert (await bob.get(draft_path)).status_code == 403
        assert (await alice.get(draft_path+'?owner=bob')).status_code == 400
        assert (await post(alice, '/api/chat', request)).json() == response.json()
        assert len(agent.calls) == 1
        reviewed = {**draft.json()['draft'], 'valid_from': '2026-10-19', 'valid_until': '2026-11-01'}
        assert (await post(alice, '/api/account/classes', reviewed)).status_code == 200
        await post(alice, '/api/chat', {**request, 'request_key': 'next-request', 'message': 'Учти пары в плане'})
        assert agent.calls[-1]['sources']['class_schedule']['status'] == 'ready'
        assert 'Алгоритмы' in '\n'.join(agent.calls[-1]['sources']['calendar'])
    async with client(application(tmp_path, agent)) as alice:
        await login(alice, 'alice')
        assert (await alice.get(draft_path)).json()['draft'] == DRAFT


@run_async
async def test_revoked_session_does_not_publish_chat_draft(tmp_path):
    agent = TimetableAgent(); agent.release = asyncio.Event()
    app = application(tmp_path, agent)
    async with client(app) as alice:
        await login(alice, 'alice')
        sending = asyncio.create_task(post(alice, '/api/chat', {
            'request_key': 'revoked-classes', 'message': 'Вторник Алгоритмы 12:00–14:00'}))
        await agent.entered.wait()
        assert (await post(alice, '/auth/signout', {})).status_code == 200
        agent.release.set()
        assert (await sending).status_code == 403
