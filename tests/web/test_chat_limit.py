"""Regression for a full conversation: keep history and continue in a new chat."""
from tests.web.test_chat_workspace import app, change
from tests.web.test_chat import FakeAgent
from tests.web.test_approval import Setup, login
from tests.storage.test_plans import run


def test_full_chat_reports_limit_and_new_chat_survives_relogin():
    async def scenario():
        h, agent = Setup(), FakeAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            for index in range(40):
                response = await client.post('/api/chat', headers=headers,
                    json={'request_key': f'message-{index}', 'message': 'Hello'})
                assert response.status_code == 200
            full = response.json()
            assert full['limit_reached'] is True
            rejected = await client.post('/api/chat', headers=headers,
                json={'request_key': 'overflow', 'message': 'One more'})
            assert rejected.status_code == 409
            assert rejected.json()['error'] == 'chat_history_limit'
            created = await change(client, headers, 'create_chat', 'continue-chat',
                title='Новый чат', folder_id=None)
            chat_id = created['item_id']
            fresh = (await client.get('/api/chat', params={'chat_id': chat_id})).json()
            assert fresh['turns'] == [] and fresh['limit_reached'] is False
            reply = await client.post('/api/chat', headers=headers,
                json={'request_key': 'new-message', 'message': 'Fresh question', 'chat_id': chat_id})
            assert reply.status_code == 200
            assert reply.json()['turns'][0]['status'] == 'completed'
            assert agent.calls[-1]['history'] == ()
            assert (await client.get('/api/chat')).json()['turns'] == full['turns']
        async with h.client(app(h, agent)) as restarted:
            await login(restarted)
            assert (await restarted.get('/api/chat')).json()['limit_reached'] is True
            assert len((await restarted.get('/api/chat', params={'chat_id': chat_id})).json()['turns']) == 1
    run(scenario())


def test_size_limit_and_retry_do_not_block_another_dialog():
    class LongAgent(FakeAgent):
        async def reply(self, **values):
            self.calls.append(values)
            return 'я' * 12000

    async def scenario():
        h, agent = Setup(), LongAgent()
        async with h.client(app(h, agent)) as client:
            headers = await login(client)
            for index in range(40):
                payload = {'request_key': f'long-{index}', 'message': 'Hello'}
                response = await client.post('/api/chat', headers=headers, json=payload)
                assert response.status_code == 200
                if response.json()['limit_reached']:
                    break
            assert index < 39
            count = len(agent.calls)
            # Retrying an already completed request at the limit must remain safe.
            duplicate = await client.post('/api/chat', headers=headers, json=payload)
            assert duplicate.json() == response.json()
            assert len(agent.calls) == count
            created = await change(client, headers, 'create_chat', 'after-size-limit',
                title='Новый чат', folder_id=None)
            response = await client.post('/api/chat', headers=headers,
                json={**payload, 'chat_id': created['item_id']})
            assert response.status_code == 200
            assert len(response.json()['turns']) == 1
            assert agent.calls[-1]['history'] == ()
    run(scenario())
