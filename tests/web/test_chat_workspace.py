"""Chat folders and isolated histories, using fake identity/model/document IO."""
import asyncio

from campuspulse_recovery.web.views.app import create_ui_app
from tests.storage.test_plans import run
from tests.web.test_approval import Setup, login
from tests.web.test_chat import FakeAgent


def app(h, agent):
    return create_ui_app(policy=h.policy, documents=h.documents, verifier=h.verifier,
        origin='https://campus.test', environment='local', clock=h.clock,
        chat_agent=agent, owner_chat_history=True)


async def change(client, headers, action, key, **fields):
    revision = (await client.get('/api/chat/workspace')).json()['revision']
    response = await client.post('/api/chat/workspace', headers=headers,
        json={'action': action, 'request_key': key, 'revision': revision, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def test_legacy_and_named_histories_folders_move_rename_and_restart():
    async def scenario():
        h, agent = Setup(), FakeAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            assert (await client.get('/api/chat/workspace')).json()['chats'][0]['chat_id'] == 'default'
            # Existing history still uses its original storage path.
            legacy = {'request_key': 'same-key', 'message': 'Legacy history'}
            assert (await client.post('/api/chat', headers=headers, json=legacy)).status_code == 200
            folder = await change(client, headers, 'create_folder', 'folder-1', title='Математика')
            folder_id = folder['item_id']
            created = await change(client, headers, 'create_chat', 'chat-1', title='Алгебра', folder_id=folder_id)
            first = created['item_id']
            second = (await change(client, headers, 'create_chat', 'chat-2', title='Алгоритмы', folder_id=None))['item_id']
            for chat_id, message in ((first, 'Matrix question'), (second, 'Graph question')):
                payload = {'request_key': 'same-key', 'message': message, 'chat_id': chat_id}
                response = await client.post('/api/chat', headers=headers, json=payload)
                assert response.status_code == 200
                assert len(response.json()['turns']) == 1
                assert agent.calls[-1]['history'] == ()
                assert (await client.post('/api/chat', headers=headers, json=payload)).json() == response.json()
            assert len(agent.calls) == 3
            await client.post('/api/chat', headers=headers,
                json={'request_key': 'followup', 'message': 'Continue', 'chat_id': first})
            assert [turn.message for turn in agent.calls[-1]['history']] == ['Matrix question']
            await change(client, headers, 'rename_chat', 'rename-1', item_id=first, title='  Линейная алгебра  ')
            await change(client, headers, 'rename_folder', 'rename-folder', item_id=folder_id, title='Учёба')
            await change(client, headers, 'move_chat', 'move-1', item_id=second, folder_id=folder_id)
            await change(client, headers, 'move_chat', 'move-2', item_id=first, folder_id=None)
            expected = (await client.get('/api/chat/workspace')).json()
            assert 'receipts' not in expected
            assert expected['folders'][0]['title'] == 'Учёба'
            entry = next(c for c in expected['chats'] if c['chat_id'] == first)
            assert entry['title'] == 'Линейная алгебра' and entry['folder_id'] is None
            async with h.client(app(h, agent)) as restarted:
                await login(restarted)  # New login, same verified owner.
                assert (await restarted.get('/api/chat/workspace')).json() == expected
                assert len((await restarted.get('/api/chat', params={'chat_id': first})).json()['turns']) == 2
                assert (await restarted.get('/api/chat')).json()['turns'][0]['message'] == legacy['message']
    run(scenario())


def test_duplicate_changed_retry_stale_revision_and_concurrent_mutations():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            payload = {'action': 'create_chat', 'request_key': 'lost-response', 'revision': 0,
                       'title': 'Новый чат', 'folder_id': None}
            first = (await client.post('/api/chat/workspace', headers=headers, json=payload)).json()
            duplicate = await client.post('/api/chat/workspace', headers=headers, json=payload)
            assert duplicate.json() == first
            changed = await client.post('/api/chat/workspace', headers=headers, json={**payload, 'title': 'Changed'})
            assert changed.status_code == 409
            stale = await client.post('/api/chat/workspace', headers=headers,
                json={**payload, 'request_key': 'stale-tab'})
            assert stale.status_code == 409
            responses = await asyncio.gather(*(client.post('/api/chat/workspace', headers=headers,
                json={**payload, 'request_key': key, 'revision': 1}) for key in ('tab-1', 'tab-2')))
            assert sorted(r.status_code for r in responses) == [200, 409]
            current = (await client.get('/api/chat/workspace')).json()
            assert current['revision'] == 2 and len(current['chats']) == 3
            # Replay after a later mutation returns the saved object, with no new chat.
            replay = (await client.post('/api/chat/workspace', headers=headers, json=payload)).json()
            assert replay['item_id'] == first['item_id'] and len(replay['chats']) == 3
    run(scenario())


def test_auth_csrf_validation_and_unregistered_conversation_rejected():
    async def scenario():
        h, agent = Setup(), FakeAgent()
        async with h.client(app(h, agent)) as client:
            assert (await client.get('/api/chat/workspace')).status_code == 403
            headers = await login(client)
            payload = {'action': 'create_folder', 'request_key': 'folder', 'revision': 0, 'title': 'Name'}
            assert (await client.post('/api/chat/workspace', json=payload)).status_code == 403
            for fields in ({'title': ' '}, {'title': 'x'*81}, {'title': 'a\nb'}, {'title': None},
                           {'owner_id': 'another-user'}, {'revision': True}, {'item_id': '../other'}):
                response = await client.post('/api/chat/workspace', headers=headers, json={**payload, **fields})
                assert response.status_code == 400, (fields, response.text)
            unknown = 'a' * 32
            assert (await client.get('/api/chat', params={'chat_id': unknown})).status_code == 403
            assert (await client.post('/api/chat', headers=headers,
                json={'chat_id': unknown, 'request_key': 'bad', 'message': 'private'})).status_code == 403
            assert not agent.calls
            for selector in ('chat_id=../other', 'chat_id=default&chat_id=default', 'owner_id=other'):
                assert (await client.get('/api/chat?' + selector)).status_code == 400
            assert (await client.get('/api/chat/workspace?owner_id=other')).status_code == 400
    run(scenario())


def test_creation_retry_remains_idempotent_after_recent_receipts_expire():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            payload = {'action': 'create_chat', 'request_key': 'original-create', 'revision': 0,
                'title': 'Original', 'folder_id': None}
            first = (await client.post('/api/chat/workspace', headers=headers, json=payload)).json()
            chat_id = first['item_id']
            for index in range(129):
                result = await client.post('/api/chat/workspace', headers=headers, json={
                    'action': 'rename_chat', 'item_id': chat_id, 'title': 'Renamed ' + str(index),
                    'request_key': 'rename-' + str(index), 'revision': index + 1})
                assert result.status_code == 200
            repeated = (await client.post('/api/chat/workspace', headers=headers, json=payload)).json()
            assert repeated['item_id'] == chat_id and len(repeated['chats']) == 2
            assert repeated['chats'][-1]['title'] == 'Renamed 128'
            assert 'creation_digest' not in repeated['chats'][-1]
            assert (await client.post('/api/chat/workspace', headers=headers,
                json={**payload, 'title': 'Changed'})).status_code == 409
    run(scenario())


def test_other_user_cannot_read_or_move_into_a_known_private_chat_or_folder(tmp_path):
    from tests.lms.test_multiuser import application, client, login as user_login, post

    async def scenario():
        calls = []
        service = application(tmp_path, calls)
        async with client(service) as alice, client(service) as bob:
            await user_login(alice, 'alice')
            await user_login(bob, 'bob')
            folder_payload = {'action': 'create_folder', 'request_key': 'folder', 'revision': 0, 'title': 'Alice folder'}
            folder = (await post(alice, '/api/chat/workspace', folder_payload)).json()['item_id']
            payload = {'action': 'create_chat', 'request_key': 'chat', 'revision': 1, 'title': 'Alice chat', 'folder_id': folder}
            chat_id = (await post(alice, '/api/chat/workspace', payload)).json()['item_id']
            assert (await post(alice, '/api/chat', {'request_key': 'hello', 'message': 'Alice private', 'chat_id': chat_id})).status_code == 200
            assert 'Alice' not in (await bob.get('/api/chat/workspace')).text
            assert (await bob.get('/api/chat', params={'chat_id': chat_id})).status_code == 403
            assert (await post(bob, '/api/chat', {'request_key': 'steal', 'message': 'secret', 'chat_id': chat_id})).status_code == 403
            assert (await post(bob, '/api/chat/workspace', {**payload, 'revision': 0})).status_code == 403
            assert (await post(bob, '/api/chat/workspace', {'action': 'rename_chat', 'request_key': 'rename', 'revision': 0,
                'item_id': chat_id, 'title': 'stolen'})).status_code == 403
            assert len(calls) == 1
    run(scenario())


def test_busy_chat_does_not_block_an_independent_chat():
    async def scenario():
        h, agent = Setup(), FakeAgent()
        agent.release = asyncio.Event()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            chat_id = (await change(client, headers, 'create_chat', 'second-chat', title='Second', folder_id=None))['item_id']
            first = asyncio.create_task(client.post('/api/chat', headers=headers,
                json={'request_key': 'first', 'message': 'Question 1'}))
            await agent.entered.wait()
            collision = await client.post('/api/chat', headers=headers,
                json={'request_key': 'same-chat', 'message': 'Question 2'})
            assert collision.status_code == 409
            agent.entered.clear()
            second = asyncio.create_task(client.post('/api/chat', headers=headers,
                json={'request_key': 'other-chat', 'message': 'Independent', 'chat_id': chat_id}))
            await agent.entered.wait()
            agent.release.set()
            responses = await asyncio.gather(first, second)
            assert all(response.status_code == 200 for response in responses)
            assert len(agent.calls) == 2 and all(call['history'] == () for call in agent.calls)
            assert (await client.get('/api/chat')).json()['turns'][0]['message'] == 'Question 1'
            assert (await client.get('/api/chat', params={'chat_id': chat_id})).json()['turns'][0]['message'] == 'Independent'
    run(scenario())
