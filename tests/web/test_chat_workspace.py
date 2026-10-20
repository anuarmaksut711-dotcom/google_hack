"""Chat folders and isolated histories, using fake identity/model/document IO."""
import asyncio
import pytest

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
            for action, item_id in (('delete_chat', chat_id), ('delete_folder', folder)):
                assert (await post(bob, '/api/chat/workspace', {'action': action, 'request_key': action,
                    'revision': 0, 'item_id': item_id})).status_code == 403
            assert (await alice.get('/api/chat', params={'chat_id': chat_id})).json()['turns'][0]['message'] == 'Alice private'
            alice_revision = (await alice.get('/api/chat/workspace')).json()['revision']
            assert (await post(alice, '/api/chat/workspace', {'action': 'delete_folder', 'request_key': 'alice-delete',
                'revision': alice_revision, 'item_id': folder})).status_code == 200
            assert (await post(bob, '/api/chat/workspace', {**payload, 'revision': 0})).status_code == 403
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


def test_first_message_names_new_chat_and_manual_name_survives_followups():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            created = await change(client, headers, 'create_chat', 'auto-title', title='Новый чат', folder_id=None)
            chat_id = created['item_id']
            payload = {'request_key': 'first-message', 'message': '  Объясни\n  алгоритм Крускала  ', 'chat_id': chat_id}
            assert (await client.post('/api/chat', headers=headers, json=payload)).status_code == 200
            catalog = (await client.get('/api/chat/workspace')).json()
            entry = next(c for c in catalog['chats'] if c['chat_id'] == chat_id)
            assert entry['title'] == 'Объясни алгоритм Крускала'
            assert entry['updated_at'] == h.clock.value.isoformat().replace('+00:00', 'Z')
            assert (await client.post('/api/chat', headers=headers, json=payload)).status_code == 200
            assert (await client.get('/api/chat/workspace')).json() == catalog
            await change(client, headers, 'rename_chat', 'manual-name', item_id=chat_id, title='Мой учебный план')
            from datetime import timedelta
            h.clock.value += timedelta(minutes=1)
            assert (await client.post('/api/chat', headers=headers, json={
                **payload, 'request_key': 'followup-message', 'message': 'Продолжай'})).status_code == 200
            entry = next(c for c in (await client.get('/api/chat/workspace')).json()['chats'] if c['chat_id'] == chat_id)
            assert entry['title'] == 'Мой учебный план'
            assert entry['updated_at'] == h.clock.value.isoformat().replace('+00:00', 'Z')
    run(scenario())


def test_existing_chat_titles_and_histories_are_preserved_on_catalog_upgrade():
    import json
    from campuspulse_recovery.domain.models import canonical_hash, canonical_json
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            chat_id = (await change(client, headers, 'create_chat', 'old-chat', title='Ручное название', folder_id=None))['item_id']
            # Simulate the document written by the previous release, without new metadata.
            for path, saved in h.documents.data.items():
                if path.startswith('cp_chat_workspaces/'):
                    previous = json.loads(saved['body'])
                    for entry in previous['chats']:
                        entry.pop('auto_title', None)
                        entry.pop('updated_at', None)
                        entry.pop('manual_title', None)
                    saved.update(kind='chat-workspace-v1', body=canonical_json(previous).encode(), digest=canonical_hash(previous))
            assert (await client.post('/api/chat', headers=headers, json={
                'chat_id': chat_id, 'request_key': 'after-upgrade', 'message': 'Новый вопрос'})).status_code == 200
            entry = next(c for c in (await client.get('/api/chat/workspace')).json()['chats'] if c['chat_id'] == chat_id)
            assert entry['title'] == 'Ручное название'
            assert (await client.get('/api/chat', params={'chat_id': chat_id})).json()['turns'][0]['message'] == 'Новый вопрос'
    run(scenario())


def test_topic_title_uses_first_message_and_never_overwrites_manual_title():
    class TopicAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.title_messages = []

        async def suggest_title(self, *, message):
            self.title_messages.append(message)
            return 'Алгоритм Крускала'

    async def scenario():
        h, agent = Setup(), TopicAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            chat_id = (await change(client, headers, 'create_chat', 'topic-chat',
                title='Новый чат', folder_id=None))['item_id']
            message = 'Помоги мне разобраться, как работает алгоритм Крускала на примере графа'
            payload = {'chat_id': chat_id, 'request_key': 'topic-first', 'message': message}
            assert (await client.post('/api/chat', headers=headers, json=payload)).status_code == 200
            catalog = (await client.get('/api/chat/workspace')).json()
            assert next(c for c in catalog['chats'] if c['chat_id'] == chat_id)['title'] == 'Алгоритм Крускала'
            assert agent.title_messages == [message]
            assert (await client.post('/api/chat', headers=headers, json=payload)).status_code == 200
            await change(client, headers, 'rename_chat', 'manual-placeholder', item_id=chat_id, title='Новый чат')
            assert (await client.post('/api/chat', headers=headers, json={
                **payload, 'request_key': 'topic-followup', 'message': 'Ещё один пример'})).status_code == 200
            catalog = (await client.get('/api/chat/workspace')).json()
            assert next(c for c in catalog['chats'] if c['chat_id'] == chat_id)['title'] == 'Новый чат'
            assert agent.title_messages == [message]
    run(scenario())


def test_placeholder_title_from_previous_release_is_repaired_from_first_turn():
    async def scenario():
        h, agent = Setup(), FakeAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            # v2 upgraded a legacy placeholder with auto_title=False.
            chat_id = (await change(client, headers, 'create_chat', 'legacy-placeholder',
                title='Новый чат', folder_id=None))['item_id']
            from campuspulse_recovery.storage.plans import _encode
            from campuspulse_recovery.web.workspace import WorkspaceStore
            for path, saved in list(h.documents.data.items()):
                if path.startswith('cp_chat_workspaces/'):
                    catalog = WorkspaceStore.decode(saved)
                    catalog = catalog.model_copy(update={'chats': tuple(
                        c.model_copy(update={'auto_title': False}) for c in catalog.chats)})
                    h.documents.data[path] = _encode('chat-workspace-v3', catalog)
            assert (await client.post('/api/chat', headers=headers, json={
                'chat_id': chat_id, 'request_key': 'legacy-first', 'message': 'Подготовка к экзамену по ОС'})).status_code == 200
            entry = next(c for c in (await client.get('/api/chat/workspace')).json()['chats'] if c['chat_id'] == chat_id)
            assert entry['title'] == 'Подготовка к экзамену по ОС'
    run(scenario())


def test_title_provider_failure_does_not_fail_message_or_repeat_title_request():
    class FailedTitleAgent(FakeAgent):
        async def suggest_title(self, *, message):
            raise RuntimeError('synthetic title provider failure')

    async def scenario():
        h = Setup()
        async with h.client(app(h, FailedTitleAgent())) as client:
            headers = await login(client)
            response = await client.post('/api/chat', headers=headers, json={
                'request_key': 'title-failure', 'message': 'План на завтра'})
            assert response.status_code == 200 and response.json()['turns'][0]['status'] == 'completed'
            assert (await client.get('/api/chat/workspace')).json()['chats'][0]['title'] == 'План на завтра'
    run(scenario())


def test_draft_capabilities_come_from_configured_agent_without_creating_history():
    class SpeechAgent(FakeAgent):
        async def transcribe(self, **kwargs):
            return 'Тест'

    async def scenario():
        for agent, chat, speech in ((SpeechAgent(), True, True), (FakeAgent(), True, False), (None, False, False)):
            h = Setup()
            async with h.client(app(h, agent)) as client:
                await login(client)
                result = (await client.get('/api/chat/workspace')).json()
                assert result['chat_available'] is chat and result['speech_available'] is speech
                assert not any(path.startswith('cp_conversations/') for path in h.documents.data)
    run(scenario())


def test_real_previous_v2_catalog_reads_and_upgrades_without_changing_history():
    from campuspulse_recovery.domain.models import canonical_hash, canonical_json

    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            await change(client, headers, 'create_folder', 'seed-scope', title='Seed')
            # Frozen previous-release wire shape, deliberately without manual_title.
            previous = {'schema_version': '1.0', 'revision': 4, 'chats': [{'schema_version': '1.0', 'chat_id': 'default', 'title': 'Первый чат',
                'folder_id': None, 'creation_digest': None, 'updated_at': None, 'auto_title': False}],
                'folders': [], 'receipts': []}
            saved = {'kind': 'chat-workspace-v2', 'digest': canonical_hash(previous),
                'body': canonical_json(previous).encode()}
            for path in list(h.documents.data):
                if path.startswith('cp_chat_workspaces/'):
                    h.documents.data[path] = saved.copy()
            response = await client.get('/api/chat/workspace')
            assert response.status_code == 200, response.text
            assert response.json()['chats'][0]['title'] == 'Первый чат'
            sent = await client.post('/api/chat', headers=headers, json={
                'request_key': 'v2-first-message', 'message': 'План подготовки к экзамену'})
            assert sent.status_code == 200 and sent.json()['turns'][0]['status'] == 'completed'
            assert (await client.get('/api/chat/workspace')).json()['chats'][0]['title'] == 'План подготовки к экзамену'
            assert next(v for k,v in h.documents.data.items() if k.startswith('cp_chat_workspaces/'))['kind'] == 'chat-workspace-v3'
            assert (await client.get('/api/chat')).json()['turns'][0]['message'] == 'План подготовки к экзамену'
    run(scenario())


@pytest.mark.parametrize('named', [False, True])
def test_delete_chat_erases_history_and_denies_old_links_and_sends_after_restart(named):
    async def scenario():
        h, agent = Setup(), FakeAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            chat_id = (await change(client, headers, 'create_chat', 'delete-me',
                title='Private', folder_id=None))['item_id'] if named else 'default'
            payload = {'chat_id': chat_id, 'request_key': 'secret-question', 'message': 'Private deleted message'}
            assert (await client.post('/api/chat', headers=headers, json=payload)).status_code == 200
            stale = await client.post('/api/chat/workspace', headers=headers, json={
                'action': 'delete_chat', 'request_key': 'stale-delete', 'revision': 0, 'item_id': chat_id})
            assert stale.status_code == 409
            assert (await client.get('/api/chat', params={'chat_id': chat_id})).json()['turns'][0]['message'] == payload['message']
            deleted = await change(client, headers, 'delete_chat', 'remove-chat', item_id=chat_id)
            assert chat_id not in [entry['chat_id'] for entry in deleted['chats']]
            # Deletion removes the stored message body, rather than only hiding its row.
            assert all(b'Private deleted message' not in value.get('body', b'') for value in h.documents.data.values())
            for params in ({'chat_id': chat_id}, *([{}] if not named else [])):
                assert (await client.get('/api/chat', params=params)).status_code == 403
            assert (await client.post('/api/chat', headers=headers, json=payload)).status_code == 403
            assert (await client.post('/api/chat', headers=headers,
                json={**payload, 'request_key': 'new-send'})).status_code == 403
            assert len(agent.calls) == 1
            async with h.client(app(h, agent)) as restarted:
                await login(restarted)
                catalog = (await restarted.get('/api/chat/workspace')).json()
                assert catalog['chats'] == deleted['chats']
                assert (await restarted.get('/api/chat', params={'chat_id': chat_id})).status_code == 403
    run(scenario())


def test_delete_project_moves_its_chats_to_general_list_and_preserves_histories():
    async def scenario():
        h, agent = Setup(), FakeAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            folder_id = (await change(client, headers, 'create_folder', 'delete-folder', title='Remove'))['item_id']
            other_folder = (await change(client, headers, 'create_folder', 'keep-folder', title='Keep'))['item_id']
            await change(client, headers, 'move_chat', 'move-default', item_id='default', folder_id=folder_id)
            child = (await change(client, headers, 'create_chat', 'child',
                title='Named child', folder_id=folder_id))['item_id']
            independent = (await change(client, headers, 'create_chat', 'independent',
                title='Keep this', folder_id=other_folder))['item_id']
            for chat_id in ('default', child):
                assert (await client.post('/api/chat', headers=headers, json={
                    'chat_id': chat_id, 'request_key': 'first', 'message': 'Keep history'})).status_code == 200
            before = (await client.get('/api/chat/workspace')).json()
            histories = {chat_id: (await client.get('/api/chat', params={'chat_id': chat_id})).json()
                         for chat_id in ('default', child)}
            deleted = await change(client, headers, 'delete_folder', 'remove-folder', item_id=folder_id)
            assert [folder['folder_id'] for folder in deleted['folders']] == [other_folder]
            assert deleted['chats'] == [{**chat, 'folder_id': None} if chat['folder_id'] == folder_id else chat
                                      for chat in before['chats']]
            assert next(chat for chat in deleted['chats'] if chat['chat_id'] == independent)['folder_id'] == other_folder
            for chat_id, history in histories.items():
                assert (await client.get('/api/chat', params={'chat_id': chat_id})).json() == history
            assert len(agent.calls) == 2
            assert (await client.post('/api/chat/workspace', headers=headers, json={
                'action': 'move_chat', 'request_key': 'move-back', 'revision': deleted['revision'],
                'item_id': child, 'folder_id': folder_id})).status_code == 403
    run(scenario())


@pytest.mark.parametrize('already_created', [False, True])
def test_first_send_retry_after_project_deletion_keeps_identity_and_lands_in_general(already_created):
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            folder = await change(client, headers, 'create_folder', 'draft-project', title='Project')
            payload = {'action': 'create_chat', 'request_key': 'draft-original-key', 'revision': folder['revision'],
                'title': 'Новый чат', 'folder_id': folder['item_id']}
            if already_created:
                first = await client.post('/api/chat/workspace', headers=headers, json=payload)
                assert first.status_code == 200
                original_id = first.json()['item_id']
            deleted = await change(client, headers, 'delete_folder', 'delete-draft-project', item_id=folder['item_id'])
            retry = await client.post('/api/chat/workspace', headers=headers, json=payload)
            if not already_created:
                assert retry.status_code == 409  # The client must first refresh its revision.
                retry = await client.post('/api/chat/workspace', headers=headers,
                    json={**payload, 'revision': deleted['revision']})
            assert retry.status_code == 200, retry.text
            saved = retry.json()
            chat_id = saved['item_id']
            if already_created:
                assert chat_id == original_id
            assert len(saved['chats']) == 2 and saved['folders'] == []
            assert next(chat for chat in saved['chats'] if chat['chat_id'] == chat_id)['folder_id'] is None
            # Keep the original project in the request fingerprint, even though
            # the server placed this chat in the general list.
            assert (await client.post('/api/chat/workspace', headers=headers, json=payload)).json() == saved
            assert (await client.post('/api/chat/workspace', headers=headers,
                json={**payload, 'folder_id': None})).status_code == 409
            if not already_created:
                for index in range(129):
                    await change(client, headers, 'rename_chat', 'retry-evict-' + str(index),
                        item_id='default', title='Keep ' + str(index))
                replay = await client.post('/api/chat/workspace', headers=headers, json=payload)
                assert replay.status_code == 200
                assert replay.json()['item_id'] == chat_id and len(replay.json()['chats']) == 2
    run(scenario())


def test_create_chat_rejects_unknown_or_deleted_chat_id_as_project():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            chat = await change(client, headers, 'create_chat', 'wrong-type', title='Chat', folder_id=None)
            deleted = await change(client, headers, 'delete_chat', 'remove-wrong-type', item_id=chat['item_id'])
            for index, folder_id in enumerate(('a' * 32, chat['item_id'])):
                response = await client.post('/api/chat/workspace', headers=headers, json={
                    'action': 'create_chat', 'request_key': 'invalid-project-' + str(index),
                    'revision': deleted['revision'], 'title': 'Invalid', 'folder_id': folder_id})
                assert response.status_code == 403
            assert (await client.get('/api/chat/workspace')).json()['chats'] == deleted['chats']
    run(scenario())


def test_deletion_retry_is_idempotent_and_stale_competing_mutation_is_rejected():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            payload = {'action': 'delete_chat', 'request_key': 'lost-delete', 'revision': 0, 'item_id': 'default'}
            deleted = await client.post('/api/chat/workspace', headers=headers, json=payload)
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()['chats'] == []
            assert (await client.post('/api/chat/workspace', headers=headers, json=payload)).json() == deleted.json()
            changed = await client.post('/api/chat/workspace', headers=headers,
                json={**payload, 'item_id': 'a' * 32})
            assert changed.status_code == 409
            stale = await client.post('/api/chat/workspace', headers=headers, json={
                'action': 'create_chat', 'request_key': 'stale-create', 'revision': 0,
                'title': 'Stale', 'folder_id': None})
            assert stale.status_code == 409
            folder = await change(client, headers, 'create_folder', 'folder', title='Project')
            concurrent = await asyncio.gather(*(client.post('/api/chat/workspace', headers=headers, json={
                'action': 'delete_folder', 'request_key': key, 'revision': folder['revision'],
                'item_id': folder['item_id']}) for key in ('delete-tab-one', 'delete-tab-two')))
            assert sorted(response.status_code for response in concurrent) == [200, 409]
            repeated = await client.post('/api/chat/workspace', headers=headers, json=payload)
            assert repeated.status_code == 200 and repeated.json()['chats'] == [] and repeated.json()['folders'] == []
    run(scenario())


def test_deleted_creation_keys_cannot_resurrect_chat_or_folder_after_receipts_expire():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            chat_payload = {'action': 'create_chat', 'request_key': 'original-chat', 'revision': 0,
                'title': 'Delete', 'folder_id': None}
            chat = (await client.post('/api/chat/workspace', headers=headers, json=chat_payload)).json()
            folder_payload = {'action': 'create_folder', 'request_key': 'original-folder',
                'revision': chat['revision'], 'title': 'Delete project'}
            folder = (await client.post('/api/chat/workspace', headers=headers, json=folder_payload)).json()
            await change(client, headers, 'delete_chat', 'delete-original-chat', item_id=chat['item_id'])
            await change(client, headers, 'delete_folder', 'delete-original-folder', item_id=folder['item_id'])
            for original in (chat_payload, folder_payload):
                replay = await client.post('/api/chat/workspace', headers=headers, json=original)
                assert replay.status_code == 403
            for index in range(129):
                await change(client, headers, 'rename_chat', 'evict-' + str(index),
                    item_id='default', title='Keep ' + str(index))
            catalog = (await client.get('/api/chat/workspace')).json()
            for original in (chat_payload, folder_payload):
                replay = await client.post('/api/chat/workspace', headers=headers,
                    json={**original, 'revision': catalog['revision']})
                assert replay.status_code == 403
            assert (await client.get('/api/chat/workspace')).json() == catalog
    run(scenario())


@pytest.mark.parametrize('named', [False, True])
def test_inflight_model_reply_cannot_restore_deleted_history(named):
    async def scenario():
        h, agent = Setup(), FakeAgent()
        agent.release = asyncio.Event()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            chat_id = (await change(client, headers, 'create_chat', 'pending-chat',
                title='Pending', folder_id=None))['item_id'] if named else 'default'
            pending = asyncio.create_task(client.post('/api/chat', headers=headers, json={
                'chat_id': chat_id, 'request_key': 'pending', 'message': 'Delete while generating'}))
            await agent.entered.wait()
            try:
                await change(client, headers, 'delete_chat', 'delete-pending', item_id=chat_id)
            finally:
                agent.release.set()
            response = await pending
            assert response.status_code == 403
            assert (await client.get('/api/chat', params={'chat_id': chat_id})).status_code == 403
            assert all(b'Delete while generating' not in value.get('body', b'') for value in h.documents.data.values())
    run(scenario())


def test_deletion_rejects_extra_fields_missing_identity_and_missing_csrf():
    async def scenario():
        h = Setup()
        async with h.client(app(h, FakeAgent())) as client:
            headers = await login(client)
            for action in ('delete_chat', 'delete_folder'):
                payload = {'action': action, 'request_key': 'delete', 'revision': 0, 'item_id': 'default'}
                assert (await client.post('/api/chat/workspace', json=payload)).status_code == 403
                for extra in ({'title': 'Delete'}, {'folder_id': None}, {'item_id': None}, {'owner_id': 'another-user'}):
                    invalid = await client.post('/api/chat/workspace', headers=headers, json={**payload, **extra})
                    assert invalid.status_code == 400
                missing_id = {key: value for key, value in payload.items() if key != 'item_id'}
                assert (await client.post('/api/chat/workspace', headers=headers, json=missing_id)).status_code == 400
            assert len((await client.get('/api/chat/workspace')).json()['chats']) == 1
    run(scenario())
