"""T26 synthetic support regressions, NOT rendered/browser or cloud proof.

Uses accepted T25 fixture examples. No Google identity or Calendar effects.
Run in the assigned isolated launcher; browser receipts remain separate.
"""
import pytest
import httpx
from tests.web.test_ui_states import Fixture
from tests.web.test_approval import ORIGIN, login, action
from tests.storage.test_plans import run
from campuspulse_recovery.storage.plans import plan_reference


@pytest.mark.parametrize('scheme,host,expected', [
    ('https', 'campus.test', 200),
    ('http', 'campus.test', 403),
    ('https', '127.0.0.1', 403),
])
def test_browser_transport_must_match_configured_https_origin(scheme, host, expected):
    async def scenario():
        h = Fixture()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app()),
                                    base_url=f'{scheme}://{host}') as client:
            response = await client.get('/')
            assert response.status_code == expected
            assert not h.invocations
    run(scenario())


@pytest.mark.parametrize('outcome,label', [
    ('partial', 'Расписание добавлено частично'), ('unknown', 'Результат неизвестен'),
    ('applied_unverified', 'Запись пока не подтверждена'),
    ('verification_failed', 'Проверка выявила расхождения'),
])
def test_synthetic_consent_separate_apply_reload_retry_keeps_outcome(outcome, label):
    async def scenario():
        h = Fixture(mode=outcome)
        plan = await h.seed_current()
        selector = {'plan_id':plan.payload.plan_id, 'version':plan.payload.version}
        async with h.client() as client:
            headers = await login(client)
            before = await client.get('/ui/state', params=selector)
            assert 'data-action="approve"' in before.text
            assert 'data-action="apply"' not in before.text
            approved = await client.post('/api/approval', headers=headers, json=action(plan))
            assert approved.status_code == 200
            assert not h.invocations
            assert 'data-action="apply"' in (await client.get('/ui/state', params=selector)).text
            payload = {'plan':plan_reference(plan).model_dump(mode='json'), 'request_key':'synthetic-apply'}
            applied = await client.post('/api/runs/continue', headers=headers, json=payload)
            assert applied.status_code == 200
            rid = applied.json()['run_id']

            async with h.client(h.app()) as reload:
                reload.cookies.update(client.cookies)
                state = await reload.get('/ui/state', params={'run_id':rid})
                assert label in state.text
                assert 'Расписание проверено' not in state.text
                assert 'data-action="apply"' not in state.text
                retry = await reload.post('/api/runs/continue', headers=headers, json=payload)
                assert retry.status_code == 200 and retry.json()['run_id'] == rid
            assert len(h.invocations) == 1
            assert (await client.get('/api/plan', params=selector)).json()['approval'] == approved.json()['approval']
    run(scenario())


@pytest.mark.parametrize('outcome', ['no_change', 'needs_input'])
def test_non_actionable_synthetic_recovery_reload_has_no_consent_or_apply(outcome):
    async def scenario():
        h = Fixture(mode=outcome)
        async with h.client() as client:
            headers = await login(client)
            result = await client.post('/api/runs', headers=headers, json={'request_key':'synthetic-recovery'})
            assert result.status_code == 200
            rid = result.json()['run_id']
            for _ in range(2):
                state = await client.get('/ui/state', params={'run_id':rid})
                assert f'data-status="{outcome}"' in state.text
                assert 'data-action="approve"' not in state.text and 'data-action="apply"' not in state.text
            retry = await client.post('/api/runs', headers=headers, json={'request_key':'synthetic-recovery'})
            assert retry.json() == result.json() and len(h.invocations) == 1
            assert not any(key.startswith('cp_approvals/') for key in h.documents.data)
    run(scenario())
