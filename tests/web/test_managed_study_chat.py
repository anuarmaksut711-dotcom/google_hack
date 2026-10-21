"""Managed chat composition with real session/grant stores and a fake Engine."""
import asyncio
from datetime import timedelta
import json

import pytest

from campuspulse_recovery.cloud.study_conversation import StudyConversation
from campuspulse_recovery.cloud.study_grants import StudyAgentSettings, StudyGrants
from campuspulse_recovery.domain.chat_plan import ClassSchedule
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.web.views.app import create_ui_app
from tests.storage.test_plans import run
from tests.web.test_approval import Setup, ORIGIN, login


def settings(policy):
    config = policy.config
    return StudyAgentSettings(target={'project_id': config.project_id, 'location': config.agent_region,
        'resource_name': f'projects/123/locations/{config.agent_region}/reasoningEngines/456'},
        agent_audience='https://study-agent.run.app', mcp_origin='https://study-mcp.run.app',
        backend_service_account=config.identities.backend_service_account,
        agent_service_account=config.identities.agent_service_account)


class Delegate:
    async def reply(self, **kwargs):
        pytest.fail('Managed failures must never fall back to local Gemini')

    async def suggest_title(self, **kwargs):
        return 'Учебный план'

    async def transcribe(self, **kwargs):
        return 'Составь план'


class Engine:
    def __init__(self, grants):
        self.grants, self.calls = grants, []
        self.entered, self.release = asyncio.Event(), None
        self.invalid_trace = False
        self.schedule = None
        self.reply = {'kind': 'message', 'message': 'Уточните доступные часы.', 'blocks': []}

    async def request(self, reference, *, seconds):
        invocation = await self.grants.consume_invocation(reference)
        self.calls.append(invocation)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        trace = {'tool': 'apply_plan' if self.invalid_trace else 'read_assignments',
                 'status': 'completed', 'duration_ms': 12, 'result_count': 3}
        return {'reply': json.dumps(self.reply, ensure_ascii=False), 'trace': [trace],
                'class_schedule': self.schedule, 'run_id': 'study_test'}


class Projection:
    def __init__(self):
        self.current = ClassSchedule(status='not_connected')
        self.reads = 0

    async def project(self, *args, **kwargs):
        pytest.fail('Managed tools must choose reads; do not prefetch sources in chat')

    async def current_class_schedule(self, context):
        self.reads += 1
        return self.current


def composition(h, engine=None):
    grants = StudyGrants(h.documents, clock=h.clock)
    engine = engine or Engine(grants)
    agent = StudyConversation(settings(h.policy), h.documents, policy=h.policy,
        delegate=Delegate(), clock=h.clock, transport=engine, grants=grants)
    projection = Projection()

    async def calendar(context):
        pytest.fail('Managed study chat must not eagerly read Google Calendar')

    app = create_ui_app(policy=h.policy, documents=h.documents, verifier=h.verifier,
        origin=ORIGIN, environment='local', clock=h.clock, chat_agent=agent,
        projection=projection, chat_mode='calendar_advice', chat_sources=calendar)
    return app, agent, engine, projection


def test_managed_reply_uses_bound_grant_without_eager_sources_and_persists_trace_once():
    async def scenario():
        h = Setup()
        app, agent, engine, projection = composition(h)
        async with h.client(app) as client:
            headers = await login(client)
            payload = {'request_key': 'study_one', 'message': 'Составь план по моим заданиям'}
            response = await client.post('/api/chat', headers=headers, json=payload)
            assert response.status_code == 200, response.text
            turn, = response.json()['turns']
            assert turn['response'] == 'Уточните доступные часы.'
            assert turn['status'] == 'completed' and turn['agent_run_id'] == 'study_test'
            assert turn['tool_trace'][0]['tool'] == 'read_assignments'
            assert turn['tool_trace'][0]['result_count'] == 3
            call, = engine.calls
            assert call['owner'] == h.config.student.trusted_user_id
            assert call['message'] == payload['message'] and call['history'] == []
            assert projection.reads == 1
            assert (await client.post('/api/chat', headers=headers, json=payload)).json() == response.json()
            assert len(engine.calls) == 1
            assert (await client.get('/api/chat')).json() == response.json()
        assert await agent.transcribe(audio=b'fixture') == 'Составь план'
        with pytest.raises(AccessDenied):
            await agent.reply(history=(), message='spoof', user_id='other-owner',
                              session_id='fake', sources={})
        assert len(engine.calls) == 1
    run(scenario())


def test_invalid_managed_payload_fails_without_local_fallback_or_private_error():
    async def scenario():
        h = Setup()
        app, _, engine, _ = composition(h)
        engine.invalid_trace = True
        async with h.client(app) as client:
            headers = await login(client)
            response = await client.post('/api/chat', headers=headers,
                json={'request_key': 'study_fail', 'message': 'Запиши всё в календарь'})
            turn, = response.json()['turns']
            assert turn['status'] == 'failed' and turn['response'] is None
            assert 'tool_trace' not in turn and 'apply_plan' not in response.text
            assert response.json()['failure_code'] == 'chat_runtime_failed'
            assert len(engine.calls) == 1
    run(scenario())


def test_managed_request_remains_working_after_90_seconds_and_revocation_blocks_publication():
    async def scenario():
        h = Setup()
        app, _, engine, _ = composition(h)
        engine.release = asyncio.Event()
        async with h.client(app) as client:
            headers = await login(client)
            task = asyncio.create_task(client.post('/api/chat', headers=headers,
                json={'request_key': 'study_slow', 'message': 'План'}))
            await engine.entered.wait()
            h.clock.value += timedelta(seconds=91)
            assert (await client.get('/api/chat')).json()['turns'][0]['status'] == 'working'
            assert (await client.post('/api/chat', headers=headers,
                json={'request_key': 'study_second', 'message': 'Ещё'})).status_code == 409
            assert (await client.post('/auth/signout', headers=headers, json={})).status_code == 200
            engine.release.set()
            assert (await task).status_code == 403
            assert len(engine.calls) == 1
    run(scenario())


def test_schedule_disconnected_during_engine_call_does_not_publish_unchecked_plan():
    async def scenario():
        h = Setup()
        app, _, engine, projection = composition(h)
        start, end = h.clock() + timedelta(hours=1), h.clock() + timedelta(hours=2)
        engine.schedule = ClassSchedule(status='ready',
            coverage={'start': h.clock(), 'end': h.clock() + timedelta(days=7)},
            fetched_at=h.clock()).model_dump(mode='json')
        engine.reply = {'kind': 'plan', 'message': '', 'blocks': [
            {'title': 'PRIVATE_UNCHECKED_TITLE', 'start': start.isoformat(), 'end': end.isoformat()}]}
        async with h.client(app) as client:
            headers = await login(client)
            response = await client.post('/api/chat', headers=headers,
                json={'request_key': 'study_schedule', 'message': 'План на сегодня'})
            turn, = response.json()['turns']
            assert 'расписание пар недоступно' in turn['response']
            assert 'PRIVATE_UNCHECKED_TITLE' not in response.text
            assert projection.reads == 1
    run(scenario())


def test_scoped_factory_keeps_two_account_invocations_and_history_separate(tmp_path):
    from campuspulse_recovery.lms.multiuser import create_multiuser_app
    from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
    from tests.lms.test_multiuser import Verifier, client, login as account_login, post

    async def scenario():
        h = Setup()
        documents = SqliteDocuments(tmp_path / 'db', project_id='campuspulse-demo', database_id='(default)')
        grants = StudyGrants(documents, clock=h.clock)
        engine = Engine(grants)

        def factory(*, policy):
            return StudyConversation(settings(policy), documents, policy=policy,
                delegate=Delegate(), clock=h.clock, transport=engine, grants=grants)

        app = create_multiuser_app(origin='https://127.0.0.1:8961', client_id='test.apps.googleusercontent.com',
            directory=tmp_path / 'app', test_verifier=Verifier(), documents=documents,
            scoped_agent_factory=factory, clock=h.clock)
        async with client(app) as alice, client(app) as bob:
            await account_login(alice, 'alice')
            await account_login(bob, 'bob')
            for person, name in ((alice, 'ALICE_PRIVATE'), (bob, 'BOB_PRIVATE')):
                response = await post(person, '/api/chat', {'request_key': 'same_key', 'message': name})
                assert response.json()['turns'][0]['status'] == 'completed', response.text
            assert engine.calls[0]['owner'] != engine.calls[1]['owner']
            assert 'ALICE_PRIVATE' not in (await bob.get('/api/chat')).text
            assert 'BOB_PRIVATE' not in (await alice.get('/api/chat')).text
            await post(alice, '/api/chat', {'request_key': 'followup', 'message': 'Продолжай'})
            assert [turn['message'] for turn in engine.calls[-1]['history']] == ['ALICE_PRIVATE']
            assert engine.calls[-1]['owner'] == engine.calls[0]['owner']
    run(scenario())


def test_cloud_settings_pin_managed_target_identity_and_separate_services():
    from campuspulse_recovery.cloud.calendar_app import CalendarSettings
    h = Setup()
    application = h.config.model_dump()
    application['mode'] = 'cloud'
    study = settings(h.policy).model_dump()
    values = dict(application=application, origin='https://study-web.run.app',
        google_client_id='test.apps.googleusercontent.com',
        chat={'provider': 'gemini_api', 'model': 'gemini-synthetic'},
        scheduler_service_account='scheduler@campus-test-42.iam.gserviceaccount.com', study_agent=study)
    assert CalendarSettings(**values).study_agent.target.project_id == h.config.project_id
    # The isolated study runtime has its own identity; legacy recovery keeps
    # its configured service accounts and approval protocol unchanged.
    isolated = {**study, 'agent_service_account': 'study-agent@campus-test-42.iam.gserviceaccount.com'}
    assert CalendarSettings(**{**values, 'study_agent': isolated}).study_agent == StudyAgentSettings(**isolated)
    for changed in (
        {**study, 'target': {**study['target'], 'project_id': 'wrong-project'}},
        {**study, 'backend_service_account': h.config.identities.agent_service_account},
        {**study, 'agent_audience': study['mcp_origin']},
    ):
        with pytest.raises(ValueError, match='matching_study_agent_application_required'):
            CalendarSettings(**{**values, 'study_agent': changed})


def test_managed_validated_plan_uses_web_proposer_without_giving_engine_write_tools():
    async def scenario():
        h = Setup()
        app, _, engine, projection = composition(h)
        projection.current = ClassSchedule(status='ready', coverage={
            'start': h.clock(), 'end': h.clock() + timedelta(days=7)}, fetched_at=h.clock())
        engine.reply = {'kind': 'plan', 'message': '', 'blocks': [{'title': 'Учёба',
            'start': (h.clock() + timedelta(hours=1)).isoformat(),
            'end': (h.clock() + timedelta(hours=2)).isoformat()}]}

        saved = []
        async def proposer(blocks, request_key, session):
            saved.extend(blocks)
            assert request_key == 'default:read_only_plan'
            return True
        app.state.calendar_plan_proposer = proposer
        async with h.client(app) as client:
            headers = await login(client)
            response = await client.post('/api/chat', headers=headers,
                json={'request_key': 'read_only_plan', 'message': 'План'})
            turn, = response.json()['turns']
            assert turn['status'] == 'completed'
            assert 'Учёба' in turn['response']
            assert 'План сохранён для вашего подтверждения' in turn['response']
            assert saved == engine.reply['blocks']
            assert [trace['tool'] for trace in turn['tool_trace']] == ['read_assignments']
    run(scenario())


def test_legacy_plan_keeps_existing_confirmation_proposer():
    from campuspulse_recovery.web.views.app import SafeSources

    async def scenario():
        h = Setup()
        calls, sources = [], []
        raw = json.dumps({'kind': 'plan', 'message': '', 'blocks': [{'title': 'Учёба',
            'start': (h.clock() + timedelta(hours=1)).isoformat(),
            'end': (h.clock() + timedelta(hours=2)).isoformat()}]})

        class LegacyAgent:
            async def reply(self, **kwargs):
                sources.append(kwargs['sources'])
                return raw

        class LegacyProjection:
            async def project(self, *args, **kwargs):
                return SafeSources(class_schedule=ClassSchedule(status='not_connected'))

        async def proposer(blocks, request_key, session):
            calls.append((blocks, request_key, session))
            return True

        app = create_ui_app(policy=h.policy, documents=h.documents, verifier=h.verifier,
            origin=ORIGIN, environment='local', clock=h.clock, chat_agent=LegacyAgent(),
            projection=LegacyProjection(), chat_mode='calendar_advice')
        app.state.calendar_plan_proposer = proposer
        async with h.client(app) as client:
            headers = await login(client)
            response = await client.post('/api/chat', headers=headers,
                json={'request_key': 'legacy_plan', 'message': 'План'})
            turn, = response.json()['turns']
            assert turn['status'] == 'completed', response.text
            assert 'План сохранён для вашего подтверждения' in turn['response']
            assert sources[0]['calendar_write']['enabled'] is True
            assert calls[0][0][0]['title'] == 'Учёба'
            assert calls[0][1] == 'default:legacy_plan'
    run(scenario())
