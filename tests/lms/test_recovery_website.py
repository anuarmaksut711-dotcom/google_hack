"""Published-app composition with real auth/session/storage boundaries."""
import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from campuspulse_recovery.cloud.calendar_app import CalendarSettings, create_calendar_cloud_app
from campuspulse_recovery.demo.adapters import demo_config
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from tests.agent_engine.test_accounts import configured
from tests.lms.test_multiuser import Verifier, login, post, url
from tests.storage.test_plans import NOW


ORIGIN = 'https://campuspulse-web-123.europe-west1.run.app'


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
        headers={'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'})


@pytest.fixture(autouse=True)
def restore_logging():
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)


def settings():
    application = demo_config().model_dump()
    application['mode'] = 'cloud'
    recovery = configured()
    recovery['application']['project_id'] = application['project_id']
    recovery['application']['firestore_database'] = application['firestore_database']
    recovery['application']['identities'] = application['identities']
    recovery['backend_origin'] = ORIGIN
    recovery['application']['agent_region'] = 'us-central1'
    recovery['google_signin_client_id'] = 'test.apps.googleusercontent.com'
    recovery['chat'] = {'provider': 'vertex', 'project_id': application['project_id'],
        'location': 'us-central1', 'model': 'gemini-synthetic-test'}
    return CalendarSettings.model_validate({'application': application, 'origin': ORIGIN,
        'google_client_id': 'test.apps.googleusercontent.com',
        'chat': {'provider': 'gemini_api', 'model': 'gemini-synthetic-test'},
        'scheduler_service_account': 'demo-scheduler@campuspulse-demo.iam.gserviceaccount.com',
        'recovery': {'settings': recovery, 'target': {'project_id': application['project_id'],
            'location': 'us-central1', 'resource_name': 'projects/123/locations/us-central1/reasoningEngines/456'}}})


async def application(tmp_path, monkeypatch, cfg=None):
    from campuspulse_recovery.web.auth import GoogleSignInVerifier
    from campuspulse_recovery.cloud import calendar_app as module
    from campuspulse_recovery.cloud.calendar_feed import CloudCalendarFeed
    monkeypatch.setattr(GoogleSignInVerifier, 'verify', Verifier.verify)
    raw = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:actual-uid@lms\r\n'
           b'SUMMARY:Homework\r\nDTSTART:20261017T220000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
    monkeypatch.setattr(module, 'CloudCalendarFeed', lambda **kwargs:
        CloudCalendarFeed(**{**kwargs, 'clock': lambda: NOW,
            'transport': httpx.MockTransport(lambda request: httpx.Response(200, content=raw))}))
    docs = SqliteDocuments(tmp_path, project_id='campuspulse-demo', database_id='(default)')
    return create_calendar_cloud_app(cfg or settings(), docs, clock=lambda: NOW), docs


def test_two_accounts_have_independent_planning_and_recovery_controls(tmp_path, monkeypatch):
    async def scenario():
        app, docs = await application(tmp_path, monkeypatch)
        async with client(app) as alice, client(app) as bob, client(app) as anonymous:
            assert (await anonymous.get('/api/moodle/planning')).status_code == 403
            await login(alice, 'alice'); await login(bob, 'bob')
            for browser, uid, effort in ((alice, 42, 90), (bob, 43, 30)):
                assert (await post(browser, '/api/account/moodle', {'url': url(uid)})).status_code == 200
                response = await post(browser, '/api/moodle/planning', {'expected_revision': 0,
                    'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': effort}]})
                assert response.status_code == 200, response.text
                assert response.json()['estimates'][0]['remaining_minutes'] == effort
                page = await browser.get('/')
                assert 'data-chat-mode="recovery"' in page.text
                assert '/planning' in page.text
                planning_page = await browser.get('/planning')
                assert planning_page.status_code == 200
                assert "script-src 'self'" in planning_page.headers['content-security-policy']
            assert (await alice.get('/api/moodle/planning')).json()['estimates'][0]['remaining_minutes'] == 90
            assert (await bob.get('/api/moodle/planning')).json()['estimates'][0]['remaining_minutes'] == 30
            assert (await post(alice, '/api/moodle/planning', {'expected_revision': 0})).status_code == 409
            assert (await post(alice, '/api/moodle/planning?owner=bob', {'expected_revision': 1})).status_code == 400
            assert (await alice.post('/api/moodle/planning', json={'expected_revision': 1})).status_code == 403
    asyncio.run(scenario())


def test_unconfigured_user_keeps_calendar_chat_and_cannot_run_recovery(tmp_path, monkeypatch):
    async def scenario():
        raw = settings().model_dump()
        raw['recovery']['settings']['calendar_feed_accounts'] = raw['recovery']['settings']['calendar_feed_accounts'][:1]
        cfg = CalendarSettings.model_validate(raw)
        app, docs = await application(tmp_path, monkeypatch, cfg)
        async with client(app) as bob:
            await login(bob, 'bob')
            assert (await post(bob, '/api/account/moodle', {'url': url(43)})).status_code == 200
            assert 'data-chat-mode="calendar_advice"' in (await bob.get('/')).text
            assert (await post(bob, '/api/runs', {'request_key': 'not-configured'})).status_code == 404
            assert (await bob.get('/api/moodle/planning')).status_code == 404
    asyncio.run(scenario())


@pytest.mark.parametrize('change', [None, 'effort', 'completion', 'feed'])
def test_website_custom_engine_mcp_approval_verify_and_replay(tmp_path, monkeypatch, change):
    from contextlib import asynccontextmanager
    from campuspulse_recovery.agent_engine import runtime
    from campuspulse_recovery.cloud import agent, engine_transport, mcp, recovery
    from campuspulse_recovery.cloud.accounts import account_settings, account_policy, create_account_service
    from campuspulse_recovery.cloud.planning import planning_inputs
    from tests.lms.test_planning_backend import Backend
    from tests.storage.test_plans import interval
    from datetime import timedelta

    async def scenario():
        h = Backend(monkeypatch)
        app, docs = await application(tmp_path, monkeypatch)
        cfg = settings().recovery.settings
        h.settings = account_settings(cfg, 'https://accounts.google.com#alice')
        h.policy = account_policy(h.settings)
        h.documents, h.now, h.allow_mutations = docs, NOW, True
        h.inputs = planning_inputs(h.settings, h.policy, docs, clock=lambda: NOW)
        original_mcp = mcp.create_mcp
        monkeypatch.setattr(mcp, 'create_mcp', lambda s, p, d, *, port, close:
            create_account_service(s, d, boundary='mcp',
                factory=lambda selected, policy, documents, close:
                    original_mcp(selected, policy, documents, port=port, close=close), close=close))
        engine = runtime.AgentEngineRuntime(h.settings.model_dump())
        @asynccontextmanager
        async def worker(parsed):
            yield create_account_service(parsed, docs, boundary='agent', factory=agent.create_agent, close=lambda: None)
        monkeypatch.setattr(runtime, '_worker_app', worker)
        calls = []
        async def query(request):
            payload = json.loads(request.content)
            assert payload['class_method'] == 'async_query'
            assert request.headers['authorization'] == 'Bearer synthetic-access'
            calls.append(payload['input']['invocation'])
            return httpx.Response(200, json={'output': await engine.async_query(**payload['input'])})
        def http(**options):
            return httpx.AsyncClient(transport=httpx.MockTransport(query), **options)
        async def access(seconds): return 'synthetic-access'
        async def token(_): return {'Authorization': 'Bearer synthetic-service'}
        original_transport = engine_transport.AgentEngineTransport
        monkeypatch.setattr(recovery, 'AgentEngineTransport', lambda target, **options:
            original_transport(target, access_token_factory=access, http_client_factory=http, **options))
        monkeypatch.setattr(engine_transport, 'RefreshingIdTokenHeaders', lambda audience: token)
        async with h.runtime(monkeypatch), client(app) as alice, client(app) as bob:
            await login(alice, 'alice'); await login(bob, 'bob')
            assert (await post(alice, '/api/account/moodle', {'url': url(42)})).status_code == 200
            changed = await post(alice, '/api/moodle/planning', {'expected_revision': 0,
                'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': 90}],
                'working_windows': [interval(NOW + timedelta(hours=1), hours=4).model_dump(mode='json')]})
            assert changed.status_code == 200, changed.text
            proposal = await post(alice, '/api/runs', {'request_key': 'website-propose'})
            assert proposal.json()['status'] == 'proposed', proposal.text
            reference = proposal.json()['plan']
            if change is not None:
                if change == 'effort':
                    assert (await post(alice, '/api/moodle/planning', {'expected_revision': 1,
                        'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': 30}]})).status_code == 200
                elif change == 'completion':
                    assert (await post(alice, '/api/moodle/task-status', {'task_id': 'actual-uid@lms', 'done': True})).status_code == 200
                else:
                    assert (await post(alice, '/api/account/moodle', {'url': url(43)})).status_code == 200
                rejected = await post(alice, '/api/approval', {**reference, 'request_key': 'stale-consent'})
                assert rejected.status_code == 409, rejected.text
                assert not h.api.mutations
                return
            assert (await bob.get('/api/runs/' + proposal.json()['run_id'])).status_code in (403, 404)
            assert (await post(bob, '/api/approval', {**reference, 'request_key': 'foreign-consent'})).status_code in (403, 409)
            assert (await post(alice, '/api/approval', {**reference, 'request_key': 'website-consent'})).status_code == 200
            request = {'plan': reference, 'request_key': 'website-apply'}
            applied = await post(alice, '/api/runs/continue', request)
            assert applied.json()['status'] == 'verified', applied.text
            assert applied.json()['verified_success'] is True
            mutations, before = deepcopy(h.api.mutations), len(calls)
            assert (await post(alice, '/api/runs/continue', request)).json() == applied.json()
            assert h.api.mutations == mutations and len(calls) == before
            assert h.tool_calls.count('apply_plan') == h.tool_calls.count('verify_plan') == 1
            assert set(h.calendar_reads) == {'alice@group.calendar.google.com'}
            trace = await alice.get('/api/runs/' + applied.json()['run_id'] + '/trace?after=0')
            assert trace.status_code == 200
            assert any(row['observation']['event'].get('tool') == 'apply_plan' for row in trace.json()['records'])
            assert 'service_token' not in trace.text
            panel = await alice.get('/ui/state', params={'run_id': applied.json()['run_id']})
            assert 'Расписание проверено' in panel.text, panel.text
    asyncio.run(scenario())
