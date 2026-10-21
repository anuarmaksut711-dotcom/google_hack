"""Local browser fixture for managed study chat and connected Calendar plans.

PYTHONPATH=src:. .venv-pitch/bin/python tests/pitch_ui/connected_browser_fixture.py
PYTHONPATH=src:. .venv-pitch/bin/python tests/pitch_ui/connected_browser_fixture.py --check

Open http://127.0.0.1:8964/fixture/start. --check uses ASGI only, without a server.
The real cloud composition runs against temporary SQLite with synthetic identity,
Moodle HTTP, Engine, OCR and Calendar/MCP edges. No live cloud acceptance is implied.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest
import uvicorn

from campuspulse_recovery.agent.conversation import AdkConversationAgent
from campuspulse_recovery.cloud import calendar_app, connected_calendar, study_conversation
from campuspulse_recovery.cloud.calendar_feed import CloudCalendarFeed
from campuspulse_recovery.cloud.study_grants import StudyGrants
from campuspulse_recovery.lms.manual_schedule import ManualSchedule
from campuspulse_recovery.lms.multiuser import account_config
from campuspulse_recovery.lms.schedule_import import ScheduleImportDraft
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from campuspulse_recovery.web.auth import GoogleSignInVerifier
from tests.lms.test_multiuser import Verifier
from tests.lms.test_recovery_website import ORIGIN, settings
from tests.pitch_ui import browser_fixture as shim
from tests.pitch_ui.test_connected_study_flow import CalendarIO
from tests.storage.test_plans import NOW
from tests.web.test_managed_study_chat import settings as study_settings


HOST, PORT = '127.0.0.1', 8964
OUTER = f'http://{HOST}:{PORT}'
NOTICE = ('ЛОКАЛЬНЫЙ ТЕСТ: Google, Moodle, AI и Calendar заменены тестовыми адаптерами. '
    'Это не проверка облака. Сценарий: 17.10.2026, 21:00 Asia/Almaty; '
    'учёба 22:30–23:30. Добавьте пары, период 17–24 октября, затем запросите расписание в чате.')


@asynccontextmanager
async def fixture_runtime():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    patch = pytest.MonkeyPatch()
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def guarded(method):
        def connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('connected fixture forbids external network')
            return method(sock, address)
        return connect
    def no_credentials(*args, **kwargs):
        raise AssertionError('connected fixture forbids real credentials')

    patch.setattr(socket.socket, 'connect', guarded(original_connect))
    patch.setattr(socket.socket, 'connect_ex', guarded(original_connect_ex))
    patch.setattr('google.auth.default', no_credentials)
    patch.delenv('GEMINI_API_KEY', raising=False)
    patch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    patch.setattr(GoogleSignInVerifier, 'verify', Verifier.verify)
    patch.setattr(shim, 'PORT', PORT)
    patch.setattr(shim, 'OUTER', OUTER)
    patch.setattr(shim, 'NOTICE', NOTICE)
    receipts, calls, tools, reads, failures = [], [], [], [], {'imports': 0, 'evidence': False}
    try:
        with tempfile.TemporaryDirectory(prefix='campuspulse-connected-fixture-') as directory:
            folder = Path(directory)
            raw = settings().model_dump()
            raw['recovery'] = None
            policy = ResourcePolicy(account_config('https://accounts.google.com#alice',
                settings().application), environment='cloud')
            raw['study_agent'] = study_settings(policy).model_dump()
            cfg = calendar_app.CalendarSettings.model_validate(raw)
            documents = SqliteDocuments(folder, project_id=cfg.application.project_id,
                database_id=cfg.application.firestore_database)
            grants = StudyGrants(documents, clock=lambda: NOW)
            classes = ManualSchedule(documents, policy, clock=lambda: NOW)
            io = CalendarIO()
            calendars = []
            original_calendar = connected_calendar.CloudConnectedCalendar

            # Retain the real class used by create_multiuser_app's isinstance
            # boundary; substitute only its external Calendar API edge.
            original_init = original_calendar.__init__
            def calendar_init(calendar, *args, **kwargs):
                def api(*args):
                    reads.append('read_calendar')
                    return {'items': deepcopy(list(io.events.values()))}
                original_init(calendar, *args, **kwargs, api=api)
                calendar.plans.io = io
                calendars.append(calendar)
            patch.setattr(original_calendar, '__init__', calendar_init)

            oauth = {'project_id': cfg.application.project_id, 'database': cfg.application.firestore_database,
                'origin': ORIGIN, 'mcp_origin': cfg.study_agent.mcp_origin, 'key': Fernet.generate_key().decode(),
                'client': {'web': {'client_id': cfg.google_client_id, 'client_secret': 'synthetic-secret',
                    'project_id': cfg.application.project_id, 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                    'redirect_uris': [ORIGIN + '/auth/google-calendar/callback']}}}
            oauth_path = folder / 'synthetic-oauth.json'
            oauth_path.write_text(json.dumps(oauth))
            patch.setenv('CAMPUSPULSE_CALENDAR_OAUTH', str(oauth_path))
            feed = (b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:synthetic-homework\r\n'
                b'SUMMARY:Synthetic algorithms homework\r\nDTSTART:20261018T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
            patch.setattr(calendar_app, 'CloudCalendarFeed', lambda **kwargs: CloudCalendarFeed(**{**kwargs,
                'transport': httpx.MockTransport(lambda request: httpx.Response(200, content=feed))}))

            class Engine:
                async def request(self, reference, *, seconds):
                    invocation = await grants.consume_invocation(reference)
                    if invocation['owner'] != policy.config.student.trusted_user_id:
                        raise AssertionError('fixture is configured only for synthetic Alice')
                    calls.append(invocation)
                    saved = await documents.get('cp_class_schedules/' + invocation['owner'])
                    schedule = classes.planning_schedule({'connected': bool(saved),
                        'schedule': saved['schedule'] if saved else None})
                    if schedule.status == 'ready':
                        reply = {'kind': 'plan', 'message': '', 'blocks': [{'title': 'Synthetic algorithms homework',
                            'start': (NOW + timedelta(hours=1, minutes=30)).isoformat(),
                            'end': (NOW + timedelta(hours=2, minutes=30)).isoformat()}]}
                    else:
                        reply = {'kind': 'message', 'message': 'Добавьте пары и даты действия на странице расписания.', 'blocks': []}
                    trace = [{'tool': 'read_assignments', 'status': 'completed', 'duration_ms': 1, 'result_count': 1},
                        {'tool': 'read_schedule', 'status': 'completed', 'duration_ms': 1, 'result_count': len(schedule.busy)}]
                    receipts.extend(trace)
                    return {'reply': json.dumps(reply, ensure_ascii=False), 'trace': trace,
                        'class_schedule': schedule.model_dump(mode='json'), 'run_id': 'study_fixture_' + uuid4().hex}
            engine = Engine()
            patch.setattr(study_conversation, 'AgentEngineTransport', lambda *args, **kwargs: engine)

            class Model(BaseLlm):
                async def generate_content_async(self, request, stream=False):
                    if request.config.response_schema is ScheduleImportDraft:
                        day = failures['imports'] % 2
                        failures['imports'] += 1
                        value = {'classes': [{'subject': 'Synthetic university class ' + str(day + 1),
                            'weekday': day, 'start': '16:00', 'end': '16:50'}], 'valid_from': None, 'valid_until': None}
                        text = json.dumps(value)
                    else:
                        text = 'Локальная проверка расписания'
                    yield LlmResponse(content=types.Content(role='model', parts=[types.Part(text=text)]),
                        finish_reason=types.FinishReason.STOP)
            patch.setattr(AdkConversationAgent, '_create_model', lambda self: Model(model='synthetic-OCR'))

            class CalendarTools:
                def __init__(self, calendar, owner):
                    self.calendar, self.owner = calendar, owner
                async def read(self, *, session, horizon=None):
                    return await self.call('read_calendar', session=session, horizon=horizon)
                async def call(self, action, *, session, plan_id=None, horizon=None):
                    calendar = self.calendar
                    reference = await calendar.grants.issue(self.owner, session, action=action, plan_id=plan_id,
                        **({'horizon': horizon} if horizon is not None else {}))
                    await calendar.grants.consume(reference, action=action)
                    tools.append(action)
                    async def live():
                        await calendar.grants.session(documents, self.owner, session)
                    await live()
                    if action == 'read_calendar':
                        result = await calendar.read(self.owner, **({'horizon': horizon} if horizon is not None else {}))
                    else:
                        method = calendar.plans.apply if action == 'apply_plan' else calendar.plans.verify
                        result = await method(self.owner, plan_id, session_check=live)
                    return {**result, 'trace': {'tool': action, 'status': 'completed', 'duration_ms': 1,
                        'run_id': 'run_' + uuid4().hex, 'call_id': 'call_' + uuid4().hex}}
            patch.setattr(original_calendar, 'tools', lambda calendar, owner: CalendarTools(calendar, owner))
            inner = calendar_app.create_calendar_cloud_app(cfg, documents, clock=lambda: NOW)
            calendar, = calendars
            await calendar.save(policy.config.student.trusted_user_id, {
                'owner': policy.config.student.trusted_user_id, 'subject': 'synthetic-alice',
                'calendar_id': 'synthetic-calendar@example.com', 'timezone': 'Asia/Almaty',
                'tokens': {'refresh_token': 'synthetic-refresh', 'scopes': [connected_calendar.WRITE_SCOPE]}})
            harness = SimpleNamespace(model_calls=receipts, tool_calls=tools,
                api=SimpleNamespace(mutations=io.creates), calendar_reads=reads)
            yield shim.outer_app(inner, harness, calls, failures)
    finally:
        patch.undo()


async def check(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=OUTER,
            follow_redirects=True, trust_env=False, headers={'Origin': OUTER, 'Sec-Fetch-Site': 'same-origin'}) as browser:
        assert (await browser.get('/fixture/start')).status_code == 200
        metadata = await browser.get('/auth/session', headers={'X-CSRF-Bootstrap': '1'})
        headers = {'X-CSRF-Token': metadata.json()['csrf_token']}
        page = await browser.get('/planning')
        assert 'classes-week-complete' in page.text and 'ЛОКАЛЬНЫЙ ТЕСТ' in page.text
        assert 'effort-form' not in page.text
        imported = await browser.post('/api/account/classes/import', headers={**headers, 'Content-Type': 'image/png'},
            content=b'\x89PNG\r\n\x1a\nsynthetic-fixture')
        assert imported.status_code == 200, imported.text
        assert (await browser.get('/api/account/classes')).json()['connected'] is False
        saved = await browser.post('/api/account/classes', headers=headers, json={**imported.json()['draft'],
            'valid_from': '2026-10-17', 'valid_until': '2026-10-24'})
        assert saved.status_code == 200, saved.text
        reply = await browser.post('/api/chat', headers=headers,
            json={'request_key': 'fixture-connected-plan', 'message': 'Составь расписание: 22:30–23:30, 60 минут.'})
        assert reply.status_code == 200 and reply.json()['turns'][0]['status'] == 'completed', reply.text
        items = (await browser.get('/api/google-calendar/plans')).json()['items']
        assert len(items) == 1 and items[0]['state']['status'] == 'draft', items
        plan = items[0]['plan']
        selection = {'plan_id': plan['plan_id'], 'payload_hash': plan['hash']}
        assert (await browser.post('/api/google-calendar/plans/approve', json=selection)).status_code == 403
        approved = await browser.post('/api/google-calendar/plans/approve', headers=headers, json=selection)
        assert approved.json()['status'] == 'approved', approved.text
        applied = await browser.post('/api/google-calendar/plans/apply', headers=headers, json=selection)
        assert applied.json()['status'] == 'verified', applied.text
        verified = await browser.post('/api/google-calendar/plans/verify', headers=headers, json=selection)
        assert verified.json()['status'] == 'verified', verified.text
        assert (await browser.get('/api/google-calendar/plans')).json()['items'][0]['state']['status'] == 'verified'
        before = (await browser.get('/fixture/receipts')).json()
        assert before['calendar_mutations'] == 1 and before['engine_invocations'] == 1, before
        assert (await browser.post('/api/google-calendar/plans/apply', headers=headers, json=selection)).json()['status'] == 'verified'
        after = (await browser.get('/fixture/receipts')).json()
        assert after['calendar_mutations'] == 1
        print(json.dumps({'connected_fixture_check': 'passed', **after}, ensure_ascii=False), flush=True)


async def main(check_only=False):
    async with fixture_runtime() as app:
        if check_only:
            await check(app)
            return
        print(f'LOCAL SYNTHETIC CONNECTED FIXTURE {OUTER}/fixture/start', flush=True)
        await uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, proxy_headers=False,
            access_log=False, log_level='error')).serve()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check the combined flow via ASGI, without binding a port')
    asyncio.run(main(parser.parse_args().check))
