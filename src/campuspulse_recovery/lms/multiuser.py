"""Google-authenticated Moodle accounts, composed from unchanged owner policies.

The dispatcher only chooses a scoped application from a verified identity or a
server session. Every private handler then validates that session with its own
ResourcePolicy. No HTTP parameter can select an account, cache, or chat history.
"""
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import Field

from ..cloud.login import install_login
from ..config import AppConfig
from ..demo.adapters import demo_config
from ..domain.models import Contract
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied
from ..security.resources import ResourcePolicy
from ..storage.plans import _decode
from ..storage.sqlite_documents import SqliteDocuments
from ..web.app import SignInBody, _set, body
from ..web.auth import (CHALLENGE_COOKIE, CSRF_COOKIE, SESSION_COOKIE,
    GoogleSignInVerifier, LoginChallenge, WebAuth, WebSession, digest)
from ..web.moodle import install_moodle_routes
from ..web.my_du import ACCOUNT_MY_DU, MY_DU_SCRIPT, install_my_du_routes
from ..web.views.app import SafeSources, create_ui_app
from .calendar_feed import CalendarFeed, FeedState, validate_feed_url
from .calendar_projection import CalendarChatProjection
from .my_du import MyDuAccount


class ConnectFeed(Contract):
    url: str = Field(min_length=1, max_length=4096)


def account_config(principal, template=None):
    if not re.fullmatch(r'https://accounts\.google\.com#[A-Za-z0-9_-]{1,128}', principal):
        raise AccessDenied('google_subject_required')
    values = (demo_config() if template is None else template).model_dump()
    owner = 'google-' + digest(principal)
    values['student'].update(trusted_user_id=owner, allowed_signin_principal=principal,
                             calendar_id=owner, spreadsheet_id=owner)
    return AppConfig.model_validate(values)


class AccountFeed:
    def __init__(self, *, policy, documents, directory, transport=None, feed_factory=None, my_du=None):
        self.policy, self.documents = policy, documents
        self.owner_id = policy.config.student.trusted_user_id
        self.directory, self.transport = directory / self.owner_id, transport
        self.record = 'cp_moodle_accounts/' + self.owner_id
        self.feed = None
        self.lock = asyncio.Lock()
        self.feed_factory = feed_factory
        self.my_du = my_du

    def make(self, url):
        try:
            user_id = int(parse_qs(urlsplit(url).query)['userid'][0])
            validate_feed_url(url, user_id)
            if not 1 <= user_id <= 2**31 - 1:
                raise ValueError()
        except Exception:
            raise ValueError('invalid_moodle_calendar_url') from None
        # A feed change gets a separate cache/status namespace. Old feed data is
        # never accidentally displayed while the new connection is unavailable.
        if self.feed_factory is not None:
            return self.feed_factory(url=url, owner_id=self.owner_id, user_id=user_id,
                                     transport=self.transport)
        folder = self.directory / digest(url)
        folder.mkdir(parents=True, exist_ok=True)
        return CalendarFeed(url=url, cache_file=folder / 'calendar.json',
            owner_id=self.owner_id, user_id=user_id, transport=self.transport)

    async def restore(self):
        saved = await self.documents.get(self.record)
        if saved is not None:
            if set(saved) != {'owner_id', 'url'} or saved['owner_id'] != self.owner_id:
                raise AccessDenied('moodle_account_binding_mismatch')
            self.feed = self.make(saved['url'])

    def authorize(self, context):
        self.policy.require_context(context)
        self.policy.require_owner(context, self.owner_id)

    async def read(self, context):
        self.authorize(context)
        async with self.lock:
            if self.feed_factory is not None:
                await self.restore()
            if self.feed and hasattr(self.feed, 'load'):
                await self.feed.load()
            return self.feed.state() if self.feed else FeedState(owner_id=self.owner_id,
                expected_user_id=1, connected=False, stale=True)

    async def refresh(self, context):
        self.authorize(context)
        async with self.lock:
            if self.feed_factory is not None:
                await self.restore()
            if self.feed:
                await self.feed.refresh()

    async def status(self, context, task_id, done):
        self.authorize(context)
        async with self.lock:
            if self.feed_factory is not None:
                await self.restore()
            if self.feed is None:
                raise ValueError('moodle_not_connected')
            await self.feed.set_local_status(task_id, done)

    async def connect(self, context, url):
        self.authorize(context)
        async with self.lock:
            candidate = self.make(url)
            await candidate.refresh()
            state = candidate.state()
            if state.snapshot is None or state.error:
                raise ValueError('moodle_feed_unavailable')
            async def save(tx):
                tx.set(self.record, {'owner_id': self.owner_id, 'url': url})
            await self.documents.transact(save)
            self.feed = candidate

    async def project(self, context, *, run, plan):
        self.authorize(context)
        async with self.lock:
            if self.feed_factory is not None:
                await self.restore()
            if self.feed is None:
                sources = SafeSources(assignments=('Moodle не подключён. Предложи добавить свою ссылку на странице /account.',),
                    calendar=(f'Сейчас {self.my_du.clock().isoformat()}. Часовой пояс: {self.policy.config.student.timezone}.'
                              if self.my_du is not None else 'Текущее время не передано.',
                              'Личные свободные часы неизвестны. Уточни доступное время.'))
            else:
                sources = await CalendarChatProjection(self.feed, self.policy).project(context, run=run, plan=plan)
        if self.my_du is not None:
            lines, schedule = await self.my_du.calendar_context(context)
            sources = SafeSources(assignments=sources.assignments, calendar=tuple(sources.calendar or ()) + lines,
                                  class_schedule=schedule)
        return sources

    async def current_class_schedule(self, context):
        """Read again after generation, so concurrent sync/disconnect is respected."""
        self.authorize(context)
        return None if self.my_du is None else self.my_du.planning_schedule(await self.my_du.read(context))


ACCOUNT_SCRIPT = r"""'use strict';
const form=document.getElementById('connect-moodle'), status=document.getElementById('account-status');
form.addEventListener('submit',async event=>{
  event.preventDefault();const button=form.querySelector('button');button.disabled=true;
  status.textContent='Проверяем ссылку и загружаем задания…';
  try {
    const cookies=document.cookie.split(';').map(s=>s.trim()).filter(s=>s.startsWith('__Host-cp-csrf='));
    if(cookies.length!==1)throw Error();
    const response=await fetch('/api/account/moodle',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(cookies[0].split('=')[1])},
      body:JSON.stringify({url:document.getElementById('moodle-url').value.trim()})});
    if(!response.ok){if(response.status===403){location.replace('/login');return;}throw Error();}
    form.reset();location.replace('/#moodle');
  }catch{status.textContent='Не удалось подключить календарь. Проверьте ссылку Get calendar URL из Moodle AITU и попробуйте снова.';}
  finally{button.disabled=false;}
});"""


def create_multiuser_app(*, origin, client_id, directory: Path, agent_factory=None,
                         transport=None, test_verifier=None, clock=lambda: datetime.now(UTC),
                         application=None, documents=None, feed_factory=None, my_du_transport=None):
    environment = 'local' if application is None else 'cloud'
    if environment == 'cloud' and (documents is None or feed_factory is None or test_verifier is not None):
        raise ValueError('explicit_cloud_dependencies_required')
    verifier = GoogleSignInVerifier(client_id) if test_verifier is None else test_verifier
    bootstrap = ResourcePolicy(account_config('https://accounts.google.com#bootstrap', application), environment=environment)
    if documents is None:
        documents = SqliteDocuments(directory, project_id=bootstrap.config.project_id,
                                   database_id=bootstrap.config.firestore_database)
    public_auth = WebAuth(documents, bootstrap, verifier=verifier, origin=origin, environment=environment, clock=clock)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    apps, lock = {}, asyncio.Lock()
    app.state.documents = documents

    @app.exception_handler(AccessDenied)
    @app.exception_handler(BindingConflict)
    async def denied(request, error):
        return JSONResponse({'error': 'access_denied'}, status_code=403)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'error': 'invalid_request'}, status_code=400)

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'strict-origin-when-cross-origin', 'X-Frame-Options': 'DENY'})
        return response

    install_login(app, public_auth, client_id, moodle=True)

    async def scoped_app(principal):
        async with lock:
            if principal in apps:
                return apps[principal]
            policy = ResourcePolicy(account_config(principal, application), environment=environment)
            my_du = MyDuAccount(documents, policy, transport=my_du_transport, clock=clock)
            feed = AccountFeed(policy=policy, documents=documents, directory=directory / 'accounts', transport=transport,
                               feed_factory=feed_factory, my_du=my_du)
            await feed.restore()
            inner = create_ui_app(policy=policy, documents=documents, verifier=verifier,
                origin=origin, environment=environment, clock=clock, moodle_enabled=True,
                signin_url='/login', chat_agent=None if agent_factory is None else agent_factory(),
                projection=feed, chat_mode='calendar_advice', owner_chat_history=True, account_url='/account')
            auth = inner.state.web_auth
            install_moodle_routes(inner, auth=auth, reader=feed.read, refresh=feed.refresh,
                source_kind='calendar_feed', update_local_status=feed.status)
            install_my_du_routes(inner, auth=auth, account=my_du)

            @inner.get('/account', response_class=HTMLResponse)
            async def account(request: Request):
                await auth.load(request)
                return ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>Моё подключение · CampusPulse</title><link rel="stylesheet" href="/assets/login.css">'
                    '<main><a href="/">К заданиям и плану</a><h1>Мой Moodle</h1>'
                    '<p>Откройте Moodle AITU → Календарь → Экспорт календаря. Выберите все события, '
                    'нажмите Get calendar URL и вставьте полученную ссылку сюда.</p>'
                    '<p>Ссылка даёт доступ к вашему календарю. Задания, отметки и переписка с Gemini '
                    'будут привязаны к этому Google-аккаунту. При составлении плана Gemini получает названия предметов, заданий и сроки. '
                    'Календарь не сообщает, сдали ли вы работу: выполненные задания отмечайте здесь.</p>'
                    '<form id="connect-moodle"><label for="moodle-url">Ссылка календаря Moodle</label>'
                    '<input id="moodle-url" type="url" required autocomplete="off" maxlength="4096">'
                    '<button type="submit">Подключить мой Moodle</button></form>'
                    '<p id="account-status" role="status"></p>' + ACCOUNT_MY_DU +
                    '</main><script src="/assets/account.js"></script></html>')

            @inner.get('/assets/account.js')
            async def account_script(request: Request):
                await auth.load(request)
                return Response(ACCOUNT_SCRIPT + MY_DU_SCRIPT, media_type='text/javascript')

            @inner.post('/api/account/moodle')
            async def connect(request: Request):
                credential = await auth.load(request, mutation=True)
                if request.query_params:
                    raise ValueError('account_selectors_forbidden')
                value = await body(request, ConnectFeed)
                with auth.request_scope(credential) as context:
                    await feed.connect(context, value.url)
                return JSONResponse({'connected': True})

            @inner.middleware('http')
            async def account_headers(request, call_next):
                response = await call_next(request)
                if request.url.path == '/account':
                    response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                return response

            apps[principal] = inner
            return inner

    @app.get('/auth/challenge')
    async def challenge(request: Request):
        public_auth.check_request(request)
        handle, csrf, nonce = await public_auth.challenge()
        response = JSONResponse({'nonce': nonce, 'csrf_token': csrf})
        _set(response, CHALLENGE_COOKIE, handle, 300)
        _set(response, CSRF_COOKIE, csrf, 300, httponly=False)
        return response

    @app.post('/auth/signin')
    async def signin(request: Request):
        value = await body(request, SignInBody)
        public_auth.check_request(request, mutation=True)
        if not 1 <= len(value.credential) <= 12000:
            raise ValueError('invalid_credential')
        handle = public_auth._cookie(request, CHALLENGE_COOKIE)
        challenge = _decode(await documents.get('cp_web_challenges/' + digest(handle)), 'web_challenge', LoginChallenge)
        public_auth.check_csrf(request, challenge.csrf_digest)
        if challenge.consumed or public_auth.now() >= challenge.expires_at:
            raise AccessDenied('signin_challenge_expired')
        identity = await verifier.verify(value.credential, nonce=challenge.nonce, now=public_auth.now())
        # Only a verified immutable Google subject may provision an account.
        inner = await scoped_app(identity.principal_id)
        auth = inner.state.web_auth
        cookie, csrf, session = await auth.signin(request, value.credential)
        response = JSONResponse({'status': 'authenticated', 'csrf_token': csrf})
        age = max(1, int((session.expires_at - auth.now()).total_seconds()))
        _set(response, SESSION_COOKIE, cookie, age)
        _set(response, CSRF_COOKIE, csrf, age, httponly=False)
        response.delete_cookie(CHALLENGE_COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        return response

    async def dispatch(scope, receive, send):
        request = Request(scope)
        try:
            public_auth.check_request(request)
            cookie = public_auth._cookie(request, SESSION_COOKIE)
            session = _decode(await documents.get('cp_web_sessions/' + digest(cookie)), 'web_session', WebSession)
            if session.revoked or not session.authenticated_at <= public_auth.now() < session.expires_at:
                raise AccessDenied('session_rejected')
            inner = await scoped_app(session.principal_id)
            await inner.state.web_auth.load(request)
        except Exception:
            response = RedirectResponse('/login') if request.method == 'GET' and request.url.path in ('/', '/account') else JSONResponse({'error': 'access_denied'}, status_code=403)
            await response(scope, receive, send)
            return
        if request.method == 'GET' and request.url.path == '/':
            owner = inner.state.web_auth.policy.config.student.trusted_user_id
            if await documents.get('cp_moodle_accounts/' + owner) is None:
                await RedirectResponse('/account')(scope, receive, send)
                return
        await inner(scope, receive, send)

    app.mount('/', dispatch)
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Google sign-in and private Moodle accounts on this computer')
    parser.add_argument('--client-id-file', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8961)
    parser.add_argument('--chat-target', type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('invalid_port')
    try:
        client_id = args.client_id_file.read_text(encoding='utf-8-sig').strip()
        GoogleSignInVerifier(client_id)
    except Exception:
        parser.error('OAuth Web Client ID file is missing or invalid; Gemini API key is not a Client ID.')
    agent_factory = None
    if args.chat_target:
        from ..agent.conversation import AdkConversationAgent, load_conversation_target, require_chat_credentials
        try:
            target = load_conversation_target(args.chat_target)
            require_chat_credentials(target)
        except ValueError:
            parser.error('Gemini settings or GEMINI_API_KEY is missing.')
        agent_factory = lambda: AdkConversationAgent(target, profile='calendar_advice')
    origin = f'https://127.0.0.1:{args.port}'
    directory = Path.cwd() / '.campuspulse-local/multiuser'
    app = create_multiuser_app(origin=origin, client_id=client_id, directory=directory, agent_factory=agent_factory)
    from ..demo.__main__ import certificate
    tls_directory = directory / 'tls'
    tls_directory.mkdir(parents=True, exist_ok=True)
    cert, key = certificate(tls_directory)
    import uvicorn
    print(f'Open {origin}/login. Add this exact origin to the OAuth Web client.', flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
        ssl_certfile=str(cert), ssl_keyfile=str(key), access_log=False, proxy_headers=False, log_level='warning'))
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(server.serve())


if __name__ == '__main__':
    main()
