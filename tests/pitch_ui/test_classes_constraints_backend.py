"""Synthetic backend/ADK/MCP runs prove class conflicts and stale consent guards."""
import asyncio
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from campuspulse_recovery.domain.models import Interval
from campuspulse_recovery.lms.manual_schedule import WeeklySchedule
from tests.lms.test_planning_backend import Backend, loopback_only
from tests.storage.test_plans import NOW


def class_record(h, *, end='22:20', subject='University class'):
    local = NOW.astimezone(ZoneInfo(h.policy.config.student.timezone))
    return WeeklySchedule(valid_from=local.date(), valid_until=local.date() + timedelta(days=6),
        classes=({'subject': subject, 'weekday': local.weekday(), 'start': '22:00', 'end': end},))


def save_classes(h, schedule):
    owner = h.policy.config.student.trusted_user_id
    h.documents.data['cp_class_schedules/' + owner] = {
        'owner_id': owner, 'schedule': schedule.model_dump(mode='json')}


async def propose(h, browser, headers):
    assert (await browser.post('/api/moodle/planning', headers=headers, json=h.update())).status_code == 200
    started = await browser.post('/api/runs', headers=headers, json={'request_key': 'classes-proposal'})
    assert started.status_code == 200 and started.json()['status'] == 'proposed', started.text
    return started.json()['plan']


def test_proposal_excludes_saved_classes_across_backend_agent_and_mcp(monkeypatch):
    async def scenario():
        h = Backend(monkeypatch)
        await h.seed()
        save_classes(h, class_record(h))
        async with h.runtime(monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            reference = await propose(h, browser, headers)
            response = await browser.get('/api/plan', params={
                'plan_id': reference['plan_id'], 'version': reference['version']})
            assert response.status_code == 200, response.text
            payload = response.json()['plan']['payload']
            lesson = Interval(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=1, minutes=20))
            for block in payload['blocks']:
                interval = Interval.model_validate(block['interval'])
                assert not (interval.start < lesson.end and lesson.start < interval.end)
            assert min(Interval.model_validate(b['interval']).start for b in payload['blocks']) >= (
                NOW + timedelta(hours=1, minutes=30))
            public = (await browser.get('/api/moodle/planning')).json()
            assert public['working_windows'] == h.update()['working_windows']
            assert (await browser.post('/api/approval', headers=headers, json={
                **reference, 'request_key': 'class-aware-approval'})).status_code == 200
            assert not h.api.mutations
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['approval', 'continue', 'dispatch_cas', 'final_io'])
@pytest.mark.parametrize('edit', ['busy_time', 'basis_only'])
def test_saved_class_edit_rejects_stale_consent_through_final_write(monkeypatch, boundary, edit):
    async def scenario():
        from types import SimpleNamespace
        from campuspulse_recovery.cloud.writers import WRITER, WriterLifetime
        from campuspulse_recovery.domain.models import OperationState, PlanReference
        from campuspulse_recovery.domain.ports import BindingConflict
        from campuspulse_recovery.security.context import AuthenticatedCaller, RequestContextFactory, ServerRunBinding

        h = Backend(monkeypatch)
        await h.seed()
        save_classes(h, class_record(h))
        async with h.runtime(monkeypatch), h.browser() as browser:
            headers = await h.login(browser)
            reference = PlanReference.model_validate(await propose(h, browser, headers))
            if boundary != 'approval':
                approved = await browser.post('/api/approval', headers=headers, json={
                    **reference.model_dump(mode='json'), 'request_key': 'classes-approval'})
                assert approved.status_code == 200, approved.text

            def edit_classes():
                save_classes(h, class_record(h, end='22:45') if edit == 'busy_time' else
                             class_record(h, subject='Edited university class'))

            if boundary in {'approval', 'continue'}:
                edit_classes()
                path = '/api/approval' if boundary == 'approval' else '/api/runs/continue'
                value = ({**reference.model_dump(mode='json'), 'request_key': 'stale-classes-approval'}
                         if boundary == 'approval' else
                         {'request_key': 'stale-classes-apply', 'plan': reference.model_dump(mode='json')})
                result = await browser.post(path, headers=headers, json=value)
                assert not h.api.mutations
                if boundary == 'continue' and edit == 'basis_only':
                    # Identical effective constraints reach MCP, where the
                    # transactional source-version check still denies writes.
                    assert result.status_code == 200 and not result.json()['verified_success'], result.text
                else:
                    assert result.status_code == 409, result.text
                    assert 'apply_plan' not in h.tool_calls
                return

            cfg = h.policy.config
            binding = ServerRunBinding(owner_id=cfg.student.trusted_user_id,
                signin_principal_id=cfg.student.allowed_signin_principal, session_id='class-guard-session',
                run_id='class-guard-run', config_revision=cfg.config_revision, expires_at=NOW + timedelta(hours=1))
            caller = AuthenticatedCaller(principal_id=cfg.identities.agent_service_account,
                session_id=binding.session_id, run_id=binding.run_id, authenticated_at=NOW,
                expires_at=binding.expires_at)
            contexts = RequestContextFactory(h.policy, boundary='mcp', clock=lambda: NOW,
                authenticator=SimpleNamespace(authenticate=lambda *args, **kwargs: caller),
                bindings=SimpleNamespace(resolve=lambda **kwargs: binding))
            service = h.mcp_services['apply']
            token = WRITER.set(WriterLifetime())
            try:
                with contexts.request(object()) as context:
                    plan = await service.plans.get(context, reference.plan_id, reference.version)
                    lease = await service.coordination.acquire(context, holder_id='class-guard-writer',
                        now=NOW, expires_at=NOW + timedelta(minutes=1))
                    state = await service.executions.bind_request(context, request_key='class-guard-request',
                        plan=reference, at=NOW)
                    state = await service.executions.compare_and_set(context, expected_revision=0, lease=lease,
                        execution=state.model_copy(update={'status': 'approved', 'revision': 1}))
                    intent = OperationState(operation_id=state.operations[0].operation_id,
                        status='in_flight', attempts=1, updated_at=NOW, fencing_generation=lease.generation)
                    if boundary == 'final_io':
                        await service.executions.record_operation(context, request_key=state.request_key,
                            expected_revision=1, operation=intent, lease=lease)
                    edit_classes()
                    original_get = h.documents.get
                    async def outside_transaction(path):
                        if path.startswith('cp_class_schedules/'):
                            raise AssertionError('Consent class read must use caller transaction')
                        return await original_get(path)
                    with monkeypatch.context() as guards:
                        guards.setattr(h.documents, 'get', outside_transaction)
                        with pytest.raises(BindingConflict, match='moodle_planning_changed'):
                            if boundary == 'dispatch_cas':
                                await service.executions.record_operation(context, request_key=state.request_key,
                                    expected_revision=1, operation=intent, lease=lease)
                            else:
                                await service._send_guard(context, plan, lease, state.request_key)
                    assert not h.api.mutations
            finally:
                WRITER.reset(token)
    asyncio.run(scenario())
