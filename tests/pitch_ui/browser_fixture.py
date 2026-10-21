"""Literal-loopback browser fixture; synthetic external edges, never deploy.

From the repository root:
  PYTHONPATH=src:. .venv-pitch/bin/python tests/pitch_ui/browser_fixture.py
  PYTHONPATH=src:. .venv-pitch/bin/python tests/pitch_ui/browser_fixture.py --check

Open http://127.0.0.1:8963/fixture/start for an explicit synthetic Alice login.
Production auth, sessions, grants, ADK, MCP, solver, approval, apply and verification
stay intact. The outer HTTP shim translates origin and fixture-only cookie names;
all identity/model/feed/Calendar edges are synthetic. State is a temporary SQLite
database deleted on shutdown. This is not live Gemini, Moodle, Google or GCP proof.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import re
import socket
import tempfile
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from tests.lms.test_recovery_website import ORIGIN, application, settings, client
from tests.lms.test_multiuser import login, post, url
from tests.lms.test_planning_backend import Backend
from tests.storage.test_plans import NOW, interval


HOST, PORT = '127.0.0.1', 8963
OUTER = f'http://{HOST}:{PORT}'
NOTICE = ('ЛОКАЛЬНАЯ СИНТЕТИЧЕСКАЯ ПРОВЕРКА · Google, Moodle, модель и Calendar заменены тестовыми адаптерами. '
          'Время сценария: 17.10.2026 21:00 Asia/Almaty. Окно для проверки: 17.10 22:00 — 18.10 02:00; трудоёмкость 90 минут.')


@asynccontextmanager
async def fixture_runtime():
    """Use exactly the existing website integration test composition."""
    from campuspulse_recovery.agent_engine import runtime
    from campuspulse_recovery.cloud import agent, engine_transport, mcp, recovery
    from campuspulse_recovery.cloud.accounts import account_settings, account_policy, create_account_service
    from campuspulse_recovery.cloud.planning import planning_inputs
    from campuspulse_recovery.cloud.backend import CloudBackend
    from campuspulse_recovery.domain.ports import PortError

    monkeypatch = pytest.MonkeyPatch()
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def checked_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('browser fixture forbids external network')
        return connect(sock, address)

    def checked_connect_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('browser fixture forbids external network')
        return connect_ex(sock, address)

    def credentials(*args, **kwargs):
        raise AssertionError('browser fixture forbids real credentials')

    monkeypatch.setattr(socket.socket, 'connect', checked_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', checked_connect_ex)
    monkeypatch.setattr('google.auth.default', credentials)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    failures = {'evidence': False}
    original_evidence = CloudBackend.stored_evidence

    async def stored_evidence(backend, *args, **kwargs):
        if failures['evidence']:
            raise PortError('synthetic_evidence_read_unavailable')
        return await original_evidence(backend, *args, **kwargs)

    monkeypatch.setattr(CloudBackend, 'stored_evidence', stored_evidence)
    try:
        with tempfile.TemporaryDirectory(prefix='campuspulse-browser-fixture-') as directory:
            h = Backend(monkeypatch)
            app, docs = await application(Path(directory), monkeypatch)
            cfg = settings().recovery.settings
            h.settings = account_settings(cfg, 'https://accounts.google.com#alice')
            h.policy = account_policy(h.settings)
            h.documents, h.now, h.allow_mutations = docs, NOW, True
            h.inputs = planning_inputs(h.settings, h.policy, docs, clock=lambda: NOW)
            original_mcp = mcp.create_mcp
            monkeypatch.setattr(mcp, 'create_mcp', lambda s, p, d, *, port, close:
                create_account_service(s, d, boundary='mcp', factory=lambda selected, policy, documents, close:
                    original_mcp(selected, policy, documents, port=port, close=close), close=close))
            engine = runtime.AgentEngineRuntime(h.settings.model_dump())

            @asynccontextmanager
            async def worker(parsed):
                yield create_account_service(parsed, docs, boundary='agent', factory=agent.create_agent, close=lambda: None)

            monkeypatch.setattr(runtime, '_worker_app', worker)
            invocations = []

            async def query(request):
                payload = json.loads(request.content)
                assert payload['class_method'] == 'async_query'
                assert request.headers['authorization'] == 'Bearer synthetic-access'
                invocations.append(payload['input']['invocation'])
                return httpx.Response(200, json={'output': await engine.async_query(**payload['input'])})

            async def access_token(seconds):
                return 'synthetic-access'

            def http_client_factory(**options):
                return httpx.AsyncClient(transport=httpx.MockTransport(query), **options)

            async def token(_):
                return {'Authorization': 'Bearer synthetic-service'}

            original_transport = engine_transport.AgentEngineTransport
            monkeypatch.setattr(recovery, 'AgentEngineTransport', lambda target, **options:
                original_transport(target, access_token_factory=access_token,
                    http_client_factory=http_client_factory, **options))
            monkeypatch.setattr(engine_transport, 'RefreshingIdTokenHeaders', lambda audience: token)
            async with h.runtime(monkeypatch):
                # Keep the real bounded extraction adapter; replace only the
                # external model with a labelled, deterministic file response.
                from campuspulse_recovery.agent.conversation import AdkConversationAgent
                from campuspulse_recovery.lms.schedule_import import ScheduleImportDraft
                from google.adk.models.base_llm import BaseLlm
                from google.adk.models.llm_response import LlmResponse
                from google.genai import types
                scripted = AdkConversationAgent._create_model
                class FileAwareModel(BaseLlm):
                    def __init__(self, owner):
                        super().__init__(model='synthetic-not-Gemini')
                        self._delegate = scripted(owner)
                    async def generate_content_async(self, request, stream=False):
                        if request.config.response_schema is ScheduleImportDraft:
                            failures['imports'] = failures.get('imports', 0) + 1
                            draft = {'classes': [{'subject': 'Synthetic university class', 'weekday': 5,
                                'start': '22:00', 'end': '22:20'}], 'valid_from': None, 'valid_until': None}
                            yield LlmResponse(content=types.Content(role='model', parts=[
                                types.Part(text=json.dumps(draft))]), finish_reason=types.FinishReason.STOP)
                        else:
                            async for response in self._delegate.generate_content_async(request, stream=stream):
                                yield response
                monkeypatch.setattr(AdkConversationAgent, '_create_model', lambda owner: FileAwareModel(owner))
                yield outer_app(app, h, invocations, failures)
    finally:
        monkeypatch.undo()


def outer_app(inner, harness, invocations, failures):
    outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @outer.middleware('http')
    async def literal_loopback_only(request, call_next):
        if (request.headers.get('host') != f'{HOST}:{PORT}'
                or request.client is None or request.client.host not in (HOST, '::1')
                or request.headers.get('origin') not in (None, OUTER)
                or request.headers.get('sec-fetch-site') not in (None, 'same-origin', 'none')):
            return JSONResponse({'error': 'literal_loopback_fixture_only'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-CampusPulse-Fixture'] = 'synthetic-local-only'
        return response

    @outer.get('/fixture/start')
    async def start():
        # Explicit fixture entry only. Use normal challenge/signin plus the
        # existing synthetic verifier; never inject a production session record.
        async with client(inner) as browser:
            await login(browser, 'alice')
            connected = await post(browser, '/api/account/moodle', {'url': url(42)})
            if connected.status_code != 200:
                raise RuntimeError('synthetic_feed_connection_failed')
            response = RedirectResponse('/', status_code=303)
            for name, value in browser.cookies.items():
                response.set_cookie(name.replace('__Host-', 'fixture-'), value, path='/', samesite='strict',
                    httponly=name != '__Host-cp-csrf', secure=False)
            return response

    @outer.get('/fixture/health')
    async def health():
        return {'status': 'ready', 'synthetic': True, 'clock': NOW.isoformat(), 'entry': '/fixture/start'}

    @outer.get('/fixture/receipts')
    async def receipts():
        return {'synthetic': True, 'clock': NOW.isoformat(), 'engine_invocations': len(invocations),
            'model_responses': len(harness.model_calls), 'tools': list(harness.tool_calls),
            'schedule_imports': failures.get('imports', 0), 'calendar_mutations': len(harness.api.mutations), 'calendar_reads': len(harness.calendar_reads)}

    @outer.get('/fixture/evidence', response_class=HTMLResponse)
    async def evidence_controls():
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<title>Синтетическая проверка отказа</title><h1>Только локальный тестовый стенд</h1>'
            '<p>Чтение evidence: ' + ('ошибка' if failures['evidence'] else 'доступно') + '</p>'
            '<form method="post"><button name="available" value="0">Отключить тестовое чтение evidence</button> '
            '<button name="available" value="1">Восстановить тестовое чтение evidence</button></form>'
            '<p><a href="/#panel">Вернуться к сохранённому плану</a></p></html>')

    @outer.post('/fixture/evidence')
    async def evidence_failure(request: Request):
        value = await request.body()
        if value not in (b'available=0', b'available=1'):
            return Response('Invalid synthetic control.', status_code=400)
        failures['evidence'] = value == b'available=0'
        return RedirectResponse('/#panel', status_code=303)

    @outer.get('/fixture/viewport', response_class=HTMLResponse)
    async def viewport(request: Request):
        # A deterministic content viewport for browser surfaces whose physical
        # window size cannot be changed by their automation provider.
        width = request.query_params.get('width', '1280')
        page = request.query_params.get('path', '/')
        if (set(request.query_params) - {'width', 'path'} or width not in ('390', '1280', '1440')
                or page not in ('/', '/planning')):
            return Response('Use width=390, 1280 or 1440 and path=/ or /planning.', status_code=400)
        return HTMLResponse('<!doctype html><html><head><meta charset="utf-8">'
            '<title>Synthetic fixture viewport</title><style>html,body{margin:0;padding:0;width:' + width + 'px}'
            'iframe{display:block;border:0}</style></head><body><iframe title="Synthetic product viewport" '
            'src="' + page + '" width="' + width + '" height="1100"></iframe></body></html>',
            headers={'Content-Security-Policy': "default-src 'none'; frame-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"})

    @outer.get('/fixture/notice.css')
    async def notice_style():
        return Response('#fixture-notice{position:fixed;left:12px;right:12px;bottom:6px;z-index:10000;'
            'padding:6px 10px;border:1px solid #8f721f;border-radius:6px;background:#fff3c4;color:#3b2a00;'
            'font:11px/1.35 system-ui;pointer-events:none}body{padding-bottom:36px}', media_type='text/css')

    @outer.api_route('/{path:path}', methods=['GET', 'POST', 'HEAD'])
    async def proxy(request: Request, path: str):
        headers = {name: value for name, value in request.headers.items()
            if name.lower() not in ('host', 'origin', 'cookie', 'content-length', 'connection')}
        headers.update({'Host': urlsplit(ORIGIN).netloc, 'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'})
        if request.headers.get('cookie'):
            # Cookies are shared across ports. An unrelated HTTPS loopback app
            # may also have __Host-cp-* cookies; forwarding both would create
            # duplicate production session names and must never select one.
            cookies = [part.strip() for part in request.headers['cookie'].split(';')
                if part.strip().split('=', 1)[0].startswith('fixture-cp-')]
            headers['Cookie'] = '; '.join(part.replace('fixture-cp-', '__Host-cp-', 1) for part in cookies)
        target = '/' + path + ('?' + request.url.query if request.url.query else '')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=inner), base_url=ORIGIN,
                follow_redirects=False, trust_env=False) as browser:
            result = await browser.request(request.method, target, headers=headers, content=await request.body())
        content = result.content
        content_type = result.headers.get('content-type', '')
        if 'text/html' in content_type:
            html = content.decode('utf-8')
            stylesheet = '<link rel="stylesheet" href="/fixture/notice.css">'
            notice = '<aside id="fixture-notice">' + NOTICE + '</aside>'
            html = (html.replace('</head>', stylesheet + '</head>') if '</head>' in html
                    else html.replace('<main', stylesheet + '<main', 1))
            html = (html.replace('</body>', notice + '</body>') if '</body>' in html
                    else html.replace('</main>', '</main>' + notice, 1))
            content = html.encode('utf-8')
        elif 'javascript' in content_type:
            # Keep cookie-reading settings scripts usable under the HTTP shim.
            content = content.replace(b'__Host-cp-csrf=', b'fixture-cp-csrf=')
        response = Response(content=content, status_code=result.status_code)
        for name, value in result.headers.multi_items():
            if name.lower() in ('content-length', 'content-encoding', 'transfer-encoding', 'connection'):
                continue
            if name.lower() == 'set-cookie':
                value = value.replace('__Host-', 'fixture-')
                value = re.sub(r';\s*Secure(?=;|$)', '', value, flags=re.IGNORECASE)
            if name.lower() == 'location':
                value = value.replace(ORIGIN, OUTER)
            if name.lower() == 'x-frame-options':
                continue
            if name.lower() == 'content-security-policy':
                value = value.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
            response.headers.append(name, value)
        return response

    return outer


async def check_running_fixture():
    """Full HTTP acceptance through the same outer shim used by the browser."""
    async with httpx.AsyncClient(base_url=OUTER, trust_env=False, follow_redirects=True,
            headers={'Origin': OUTER, 'Sec-Fetch-Site': 'same-origin'}) as browser:
        health = await browser.get('/fixture/health')
        assert health.json()['status'] == 'ready'
        assert (await browser.get('/fixture/start')).status_code == 200
        browser.cookies.set('__Host-cp-session', 'unrelated-loopback-session', domain=HOST, path='/')
        browser.cookies.set('__Host-cp-csrf', 'unrelated-loopback-csrf', domain=HOST, path='/')
        metadata = await browser.get('/auth/session', headers={'X-CSRF-Bootstrap': '1'})
        assert metadata.status_code == 200, metadata.text
        headers = {'X-CSRF-Token': metadata.json()['csrf_token']}
        imported = await browser.post('/api/account/classes/import', headers={**headers, 'Content-Type':'application/pdf'},
            content=Path('presentation/pitch-2026-10-22/samples/synthetic-weekly-classes.pdf').read_bytes())
        assert imported.status_code == 200, imported.text
        assert (await browser.get('/api/account/classes')).json()['connected'] is False
        draft = imported.json()['draft']
        assert draft['valid_from'] is draft['valid_until'] is None
        assert (await browser.post('/api/account/classes', headers=headers, json={**draft,
            'valid_from':'2026-10-17', 'valid_until':'2026-10-24'})).status_code == 200
        saved = await browser.post('/api/moodle/planning', headers=headers, json={'expected_revision': 0,
            'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': 90}],
            'working_windows': [interval(NOW + timedelta(hours=1), hours=4).model_dump(mode='json')]})
        assert saved.status_code == 200, saved.text
        proposal = await browser.post('/api/runs', headers=headers, json={'request_key': 'fixture-check-propose'})
        assert proposal.json()['status'] == 'proposed', proposal.text
        plan = proposal.json()['plan']
        approval = await browser.post('/api/approval', headers=headers, json={**plan, 'request_key': 'fixture-check-consent'})
        assert approval.status_code == 200, approval.text
        payload = {'plan': plan, 'request_key': 'fixture-check-apply'}
        applied = await browser.post('/api/runs/continue', headers=headers, json=payload)
        assert applied.json()['status'] == 'verified', applied.text
        assert applied.json()['verified_success'] is True
        before = (await browser.get('/fixture/receipts')).json()
        assert before['tools'].count('apply_plan') == before['tools'].count('verify_plan') == 1
        assert before['calendar_mutations'] > 0
        assert (await browser.post('/api/runs/continue', headers=headers, json=payload)).json() == applied.json()
        assert (await browser.get('/fixture/receipts')).json() == before
        run_id = applied.json()['run_id']
        trace = await browser.get('/api/runs/' + run_id + '/trace?after=0')
        assert trace.status_code == 200
        assert any(row['observation']['event'].get('tool') == 'verify_plan' for row in trace.json()['records'])
        panel = await browser.get('/ui/state', params={'run_id': run_id})
        assert 'data-status="verified"' in panel.text, panel.text
        assert 'Задание: Homework' in panel.text and '22:30' in panel.text
        print(json.dumps({'fixture_check': 'passed', 'run_id': run_id, **before}, ensure_ascii=False), flush=True)


async def main(check=False):
    async with fixture_runtime() as app:
        server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, access_log=False,
            log_level='error', proxy_headers=False))
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(.025)
            if not server.started:
                raise RuntimeError('fixture_start_timeout')
            print(f'SYNTHETIC LOOPBACK FIXTURE READY {OUTER}/fixture/start', flush=True)
            if check:
                await check_running_fixture()
            else:
                await task
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='run full synthetic acceptance and stop')
    args = parser.parse_args()
    asyncio.run(main(args.check))
