"""A failed optional display projection must not erase a saved, authorized plan."""
import asyncio
from datetime import timedelta

import httpx
import pytest

from campuspulse_recovery.cloud.backend import CloudBackend
from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.plans import plan_reference
from tests.pitch_ui.browser_fixture import OUTER, fixture_runtime
from tests.storage.test_plans import NOW, interval, run
from tests.web.test_approval import login
from tests.web.test_ui_states import Fixture


def test_projection_outage_keeps_exact_saved_plan_without_mutation_controls():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()

        class Projection:
            async def project(self, context, **kwargs):
                raise PortError('private-adapter-response')

        async with h.client(h.app(projection=Projection())) as c:
            await login(c)
            result = await c.get('/ui/state', params={'plan_id': plan_reference(plan).plan_id, 'version': plan_reference(plan).version})
            assert result.status_code == 200
            assert plan_reference(plan).payload_hash in result.text
            assert 'role="alert"' in result.text
            assert 'Обновить состояние' in result.text
            assert 'data-action=' not in result.text
            assert 'Расписание проверено' not in result.text
            assert 'private-adapter-response' not in result.text
            assert not h.invocations
    run(scenario())


@pytest.mark.parametrize('error,status', [(AccessDenied, 403), (BindingConflict, 409)])
def test_projection_authority_and_binding_errors_are_not_availability_fallback(error, status):
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()

        class Projection:
            async def project(self, context, **kwargs):
                raise error('private-binding-details')

        async with h.client(h.app(projection=Projection())) as c:
            await login(c)
            result = await c.get('/ui/state', params={'plan_id': plan_reference(plan).plan_id, 'version': plan_reference(plan).version})
            assert result.status_code == status
            assert plan_reference(plan).payload_hash not in result.text
            assert 'private-binding-details' not in result.text
    run(scenario())


def test_projection_outage_still_checks_session_revocation_after_await():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()

        class Projection:
            async def project(self, context, **kwargs):
                await c.post('/auth/signout', headers=headers, json={})
                raise PortError('private-after-revocation')

        async with h.client(h.app(projection=Projection())) as c:
            headers = await login(c)
            result = await c.get('/ui/state', params={'plan_id': plan_reference(plan).plan_id, 'version': plan_reference(plan).version})
            assert result.status_code == 403
            assert plan_reference(plan).payload_hash not in result.text
            assert 'private-after-revocation' not in result.text
    run(scenario())


def test_projection_outage_without_plan_does_not_claim_a_saved_version():
    async def scenario():
        h = Fixture()

        class Projection:
            async def project(self, context, **kwargs):
                raise PortError('private-adapter-response')

        async with h.client(h.app(projection=Projection())) as c:
            await login(c)
            result = await c.get('/ui/state')
            assert result.status_code == 200
            assert 'role="alert"' in result.text
            assert 'Показана сохранённая версия плана' not in result.text
            assert 'id="plan-reference"' not in result.text
            assert 'data-action=' not in result.text
    run(scenario())


def test_evidence_outage_downgrades_verified_display_and_recovers_without_writes(monkeypatch):
    async def scenario():
        async with fixture_runtime() as app:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 49000)),
                    base_url=OUTER, follow_redirects=True,
                    headers={'Origin': OUTER, 'Sec-Fetch-Site': 'same-origin'}) as c:
                assert (await c.get('/fixture/start')).status_code == 200
                session = await c.get('/auth/session', headers={'X-CSRF-Bootstrap': '1'})
                headers = {'X-CSRF-Token': session.json()['csrf_token']}
                saved = await c.post('/api/moodle/planning', headers=headers, json={'expected_revision': 0,
                    'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': 90}],
                    'working_windows': [interval(NOW + timedelta(hours=1), hours=4).model_dump(mode='json')]})
                assert saved.status_code == 200
                proposed = await c.post('/api/runs', headers=headers, json={'request_key': 'outage-propose'})
                plan = proposed.json()['plan']
                assert (await c.post('/api/approval', headers=headers,
                    json={**plan, 'request_key': 'outage-consent'})).status_code == 200
                applied = await c.post('/api/runs/continue', headers=headers,
                    json={'plan': plan, 'request_key': 'outage-apply'})
                assert applied.json()['status'] == 'verified'
                selector = {'run_id': applied.json()['run_id']}
                before = await c.get('/ui/state', params=selector)
                assert 'data-status="verified"' in before.text
                receipts = (await c.get('/fixture/receipts')).json()

                async def unavailable(*args, **kwargs):
                    raise PortError('private-evidence-worker-body')

                with monkeypatch.context() as patch:
                    patch.setattr(CloudBackend, 'stored_evidence', unavailable)
                    failed = await c.get('/ui/state', params=selector)
                assert failed.status_code == 200
                assert plan['payload_hash'] in failed.text
                assert 'data-status="applied_unverified"' in failed.text
                assert 'Проверка записи: не подтверждена' in failed.text
                assert 'role="alert"' in failed.text
                assert 'Расписание проверено' not in failed.text
                assert 'Событие в Google Calendar' not in failed.text
                assert 'data-action=' not in failed.text
                assert 'private-evidence-worker-body' not in failed.text
                restored = await c.get('/ui/state', params=selector)
                assert 'data-status="verified"' in restored.text
                assert (await c.get('/fixture/receipts')).json()['calendar_mutations'] == receipts['calendar_mutations']
    asyncio.run(scenario())
