"""Authenticated Moodle accounts, composed from unchanged owner policies.

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
from pydantic import Field, ValidationError

from ..cloud.login import install_login
from ..config import AppConfig
from ..demo.adapters import demo_config
from ..domain.models import Contract
from ..domain.ports import BindingConflict, PortError
from ..security.context import AccessDenied
from ..security.resources import ResourcePolicy
from ..storage.plans import _decode
from ..storage.sqlite_documents import SqliteDocuments
from ..web.app import SignInBody, _set, body
from ..web.account import render_moodle_settings
from ..web.auth import (CHALLENGE_COOKIE, CHALLENGE_CSRF_COOKIE, CSRF_COOKIE, SESSION_COOKIE,
    GoogleSignInVerifier, WebAuth, WebSession, digest)
from ..web.passwords import (EMAIL_ISSUER, EmailCredentials, EmailRegistrationUnavailable,
    InvalidCredentials, PasswordRateLimited, PasswordValidationError)
from ..web.moodle import install_moodle_routes
from ..web.manual_schedule import install_class_routes
from ..web.views.app import SafeSources, create_ui_app
from .calendar_feed import CalendarFeed, FeedState, validate_feed_url
from .calendar_projection import CalendarChatProjection
from .manual_schedule import ManualSchedule


class ConnectFeed(Contract):
    url: str = Field(min_length=1, max_length=4096)


class EmailSignInBody(Contract):
    email: str = Field(strict=True, min_length=1, max_length=320)
    password: str = Field(strict=True, min_length=1, max_length=128)


def account_config(principal, template=None):
    if re.fullmatch(r'https://accounts\.google\.com#[A-Za-z0-9_-]{1,128}', principal):
        owner = 'google-' + digest(principal)
    elif re.fullmatch(re.escape(EMAIL_ISSUER) + r'#[0-9a-f]{32}', principal):
        owner = 'email-' + digest(principal)
    else:
        raise AccessDenied('immutable_account_subject_required')
    values = (demo_config() if template is None else template).model_dump()
    values['student'].update(trusted_user_id=owner, allowed_signin_principal=principal,
                             calendar_id=owner, spreadsheet_id=owner)
    return AppConfig.model_validate(values)


class AccountFeed:
    def __init__(self, *, policy, documents, directory, transport=None, feed_factory=None, classes=None, recovery=None):
        self.policy, self.documents = policy, documents
        self.owner_id = policy.config.student.trusted_user_id
        self.directory, self.transport = directory / self.owner_id, transport
        self.record = 'cp_moodle_accounts/' + self.owner_id
        self.feed = None
        self._configured_url = None
        self.lock = asyncio.Lock()
        self.feed_factory = feed_factory
        self.classes = classes
        self.recovery = recovery

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
        if saved is None:
            self.feed, self._configured_url = None, None
            return
        if set(saved) != {'owner_id', 'url'} or saved['owner_id'] != self.owner_id:
            raise AccessDenied('moodle_account_binding_mismatch')
        if self.feed is None or saved['url'] != self._configured_url:
            self.feed = self.make(saved['url'])
            self._configured_url = saved['url']

    async def connected(self, context):
        """Saved account binding, independent of a Moodle fetch or snapshot age."""
        self.authorize(context)
        async with self.lock:
            await self.restore()
            return self.feed is not None

    def authorize(self, context):
        self.policy.require_context(context)
        self.policy.require_owner(context, self.owner_id)

    async def read(self, context):
        self.authorize(context)
        async with self.lock:
            await self.restore()
            if self.feed and hasattr(self.feed, 'load'):
                await self.feed.load()
            return self.feed.state() if self.feed else FeedState(owner_id=self.owner_id,
                expected_user_id=1, connected=False, stale=True)

    async def refresh(self, context):
        self.authorize(context)
        async with self.lock:
            await self.restore()
            if self.feed:
                await self.feed.refresh()

    async def status(self, context, task_id, done):
        self.authorize(context)
        async with self.lock:
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
            self._configured_url = url

    async def project(self, context, *, run, plan):
        self.authorize(context)
        async with self.lock:
            await self.restore()
            if self.feed is None:
                sources = SafeSources(assignments=('Moodle не подключён. Предложи добавить свою ссылку на странице /account.',),
                    calendar=(f'Сейчас {self.classes.clock().isoformat()}. Часовой пояс: {self.policy.config.student.timezone}.'
                              if self.classes is not None else 'Текущее время не передано.',
                              'Личные свободные часы неизвестны. Уточни доступное время.'))
            else:
                sources = await CalendarChatProjection(self.feed, self.policy).project(context, run=run, plan=plan)
        if self.classes is not None:
            lines, schedule = await self.classes.calendar_context(context)
            sources = SafeSources(assignments=sources.assignments, calendar=tuple(sources.calendar or ()) + lines,
                                  class_schedule=schedule)
        if self.recovery is not None and run is not None and plan is not None and run.verification_status is not None:
            from ..storage.plans import plan_reference
            evidence = await self.recovery.stored_evidence(context, plan_reference(plan))
            if evidence.run_id == run.run_id:
                sources = SafeSources(assignments=sources.assignments, calendar=sources.calendar,
                                      class_schedule=sources.class_schedule, verification=evidence)
        return sources

    async def current_class_schedule(self, context):
        """Read again after generation, so concurrent sync/disconnect is respected."""
        self.authorize(context)
        return None if self.classes is None else self.classes.planning_schedule(await self.classes.read(context))


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
                         application=None, documents=None, feed_factory=None, my_du_transport=None,
                         recovery_factory=None, recovery_chat_factory=None, local_google_calendar=None,
                         cloud_google_calendar=None, scoped_agent_factory=None):
    environment = 'local' if application is None else 'cloud'
    if local_google_calendar is not None and environment != 'local':
        raise ValueError('local_calendar_requires_local_application')
    if cloud_google_calendar is not None:
        from ..cloud.connected_calendar import CloudConnectedCalendar
        if environment != 'cloud' or local_google_calendar is not None or not isinstance(cloud_google_calendar, CloudConnectedCalendar):
            raise ValueError('explicit_cloud_calendar_required')
        if cloud_google_calendar.client['web']['client_id'] != client_id:
            raise ValueError('matching_calendar_signin_client_required')
        local_google_calendar = cloud_google_calendar
    if environment == 'cloud' and (documents is None or feed_factory is None or test_verifier is not None):
        raise ValueError('explicit_cloud_dependencies_required')
    verifier = GoogleSignInVerifier(client_id) if test_verifier is None else test_verifier
    bootstrap = ResourcePolicy(account_config('https://accounts.google.com#bootstrap', application), environment=environment)
    if documents is None:
        documents = SqliteDocuments(directory, project_id=bootstrap.config.project_id,
                                   database_id=bootstrap.config.firestore_database)
    email_credentials = EmailCredentials(documents, clock=clock)
    public_auth = WebAuth(documents, bootstrap, verifier=verifier, origin=origin,
                          environment=environment, clock=clock, email_credentials=email_credentials)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    apps, lock = {}, asyncio.Lock()
    app.state.documents = documents

    @app.exception_handler(AccessDenied)
    @app.exception_handler(BindingConflict)
    async def denied(request, error):
        return JSONResponse({'error': 'access_denied'}, status_code=403)

    @app.exception_handler(ValueError)
    @app.exception_handler(ValidationError)
    async def invalid(request, error):
        return JSONResponse({'error': 'invalid_request'}, status_code=400)

    @app.exception_handler(PortError)
    async def unavailable(request, error):
        return JSONResponse({'error': 'signin_unavailable'}, status_code=503)

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY'})
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        return response

    install_login(app, public_auth, client_id, moodle=True, email_password=True)
    if local_google_calendar is not None:
        from ..web.google_calendar import install_calendar_public
        install_calendar_public(app, origin=origin)

    async def scoped_app(principal):
        async with lock:
            if principal in apps:
                return apps[principal]
            # Unverified email identifiers cannot acquire a Google Calendar
            # binding merely by matching a Google account's email address.
            recovery = (None if recovery_factory is None or not principal.startswith('https://accounts.google.com#')
                        else recovery_factory(principal, clock=clock))
            policy = (ResourcePolicy(account_config(principal, application), environment=environment)
                      if recovery is None else recovery.policy)
            classes = ManualSchedule(documents, policy, clock=clock)
            feed = AccountFeed(policy=policy, documents=documents, directory=directory / 'accounts', transport=transport,
                               feed_factory=feed_factory, classes=classes, recovery=recovery)
            await feed.restore()
            selected_agent = (recovery_chat_factory if recovery is not None and recovery_chat_factory is not None
                              else agent_factory)
            selected_chat_agent = (scoped_agent_factory(policy=policy) if scoped_agent_factory is not None and recovery is None
                                   else None if selected_agent is None else selected_agent())
            async def calendar_sources(context):
                owner = context.trusted.owner_id
                state = await local_google_calendar.status(owner)
                if not state['connected']:
                    return {'status': 'not_connected'}
                from ..integrations.local_calendar import LocalCalendarTools
                result = (await local_google_calendar.tools(owner).read(session=context.session_id)
                          if hasattr(local_google_calendar, 'tools') else
                          await LocalCalendarTools(local_google_calendar, owner).read())
                if 'error' in result:
                    return {'status': 'unavailable'}
                from ..integrations.calendar_agenda import agenda_source
                return agenda_source(result)
            inner = create_ui_app(policy=policy, documents=documents, verifier=verifier,
                origin=origin, environment=environment, clock=clock, moodle_enabled=True,
                signin_url='/login', chat_agent=selected_chat_agent,
                projection=feed, chat_mode='calendar_advice' if recovery is None else 'recovery',
                runs_factory=None if recovery is None else recovery.runs,
                approval_store=None if recovery is None else recovery.approvals,
                planning_url='/planning',
                owner_chat_history=True, account_url='/settings', email_credentials=email_credentials,
                google_calendar_enabled=local_google_calendar is not None,
                calendar_plan_available=(recovery is None and selected_chat_agent is not None
                    and bool(getattr(local_google_calendar, 'cloud_ready', False))),
                chat_sources=calendar_sources if local_google_calendar is not None else None)
            auth = inner.state.web_auth
            if recovery is not None:
                from ..web.moodle_planning import install_planning_routes
                install_planning_routes(inner, auth=auth, inputs=recovery.planning)
            from ..web.recovery_planning import install_recovery_planning_page
            install_recovery_planning_page(inner, auth=auth, inputs=None if recovery is None else recovery.planning)
            install_moodle_routes(inner, auth=auth, reader=feed.read, refresh=feed.refresh,
                source_kind='calendar_feed', update_local_status=feed.status)
            inner.state.class_schedule_importer = selected_chat_agent
            install_class_routes(inner, auth=auth, account=classes)
            if local_google_calendar is not None:
                from ..web.google_calendar import install_calendar_routes
                install_calendar_routes(inner, auth=auth, calendar=local_google_calendar)

            @inner.get('/account', response_class=HTMLResponse)
            @inner.get('/settings', response_class=HTMLResponse)
            async def account(request: Request):
                credential = await auth.load(request)
                with auth.request_scope(credential) as context:
                    connected = await feed.connected(context)
                if (await auth.load(request)).session != credential.session:
                    raise AccessDenied('account_session_changed')
                from ..web.google_calendar import ACCOUNT_GOOGLE_CALENDAR
                return render_moodle_settings(connected=connected, classes_html=
                    '<section class="settings-card"><h2>Пары и время для учёбы</h2>'
                    '<p>Загрузите PDF или скриншот расписания и проверьте пары перед сохранением.</p>'
                    '<a href="/planning">Подготовить расписание</a></section>',
                    google_calendar_html=ACCOUNT_GOOGLE_CALENDAR if local_google_calendar is not None else '')

            @inner.get('/assets/account.js')
            async def account_script(request: Request):
                await auth.load(request)
                return Response(ACCOUNT_SCRIPT, media_type='text/javascript')

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
                if request.url.path in ('/account', '/settings') or (local_google_calendar is not None and request.url.path == '/google-calendar'):
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
        _set(response, CHALLENGE_CSRF_COOKIE, csrf, 300)
        return response

    @app.post('/auth/signin')
    async def signin(request: Request):
        value = await body(request, SignInBody)
        public_auth.check_request(request, mutation=True)
        if not 1 <= len(value.credential) <= 12000:
            raise ValueError('invalid_credential')
        challenge = await public_auth.challenge_for_signin(request)
        identity = await verifier.verify(value.credential, nonce=challenge.nonce, now=public_auth.now())
        # Only a verified immutable Google subject may provision an account.
        inner = await scoped_app(identity.principal_id)
        auth = inner.state.web_auth
        return signed_in(auth, await auth.signin(request, value.credential))

    def signed_in(auth, result):
        cookie, csrf, session = result
        response = JSONResponse({'status': 'authenticated', 'csrf_token': csrf})
        age = max(1, int((session.expires_at - auth.now()).total_seconds()))
        _set(response, SESSION_COOKIE, cookie, age)
        _set(response, CSRF_COOKIE, csrf, age, httponly=False)
        response.delete_cookie(CHALLENGE_COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        response.delete_cookie(CHALLENGE_CSRF_COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        return response

    if local_google_calendar is not None:
        from ..integrations.local_calendar import CalendarConnectionError
        from ..web.google_calendar import CONNECT_PAGE, Completion

        @app.get('/google-calendar/connect', response_class=HTMLResponse)
        async def calendar_signin_page(request: Request):
            public_auth.check_login_navigation(request, additional_paths=('/google-calendar/connect',))
            return HTMLResponse(CONNECT_PAGE, headers={'Content-Security-Policy':
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})

        @app.post('/auth/google-calendar/signin/start')
        async def calendar_signin_start(request: Request):
            challenge = await public_auth.challenge_for_signin(request)
            if request.query_params:
                raise ValueError('calendar_selectors_forbidden')
            await body(request, Contract)
            provisional = 'signin-' + digest(challenge.nonce)
            url = await local_google_calendar.start(owner=provisional,
                session=challenge.nonce, nonce=challenge.nonce)
            response = JSONResponse({'authorization_url': url})
            response.set_cookie('__Host-cp-calendar-flow', 'signin', secure=True,
                samesite='strict', path='/', max_age=300)
            return response

        @app.post('/auth/google-calendar/signin/complete')
        async def calendar_signin_complete(request: Request):
            challenge = await public_auth.challenge_for_signin(request)
            if request.query_params:
                raise ValueError('calendar_selectors_forbidden')
            value = await body(request, Completion)
            provisional = 'signin-' + digest(challenge.nonce)
            try:
                connection = await local_google_calendar.exchange(owner=provisional,
                    session=challenge.nonce, principal='prelogin', state=value.state, code=value.code,
                    return_id_token=True)
                # The signed Google ID token goes through exactly the existing
                # verifier and one-use challenge/session creation path.
                token = connection.pop('_signin_id_token')
                identity = await verifier.verify(token, nonce=challenge.nonce, now=public_auth.now())
                if identity.principal_id != 'https://accounts.google.com#' + connection['subject']:
                    raise AccessDenied('calendar_subject_mismatch')
                inner = await scoped_app(identity.principal_id)
                auth = inner.state.web_auth
                result = await auth.signin(request, token)
                owner = auth.policy.config.student.trusted_user_id
                await local_google_calendar.save(owner, {**connection, 'owner': owner})
                response = signed_in(auth, result)
                response.delete_cookie('__Host-cp-calendar-flow', secure=True, samesite='strict', path='/')
                return response
            except CalendarConnectionError:
                return JSONResponse({'error': 'calendar_authorization_failed'}, status_code=400)

    async def email_signin(request, *, register):
        # Establish browser intent before parsing or hashing a credential. The
        # final session transaction rechecks and consumes this exact challenge.
        challenge = await public_auth.challenge_for_signin(request)
        if request.query_params:
            raise ValueError('account_selectors_forbidden')
        value = await body(request, EmailSignInBody)
        try:
            action = email_credentials.register if register else email_credentials.authenticate
            proof = await action(value.email, value.password, nonce=challenge.nonce)
            identity = email_credentials.check(proof, nonce=challenge.nonce, now=public_auth.now())
        except PasswordValidationError as error:
            code = error.code if error.code in ('email_invalid', 'password_length') else 'invalid_request'
            return JSONResponse({'error': code}, status_code=400)
        except InvalidCredentials:
            return JSONResponse({'error': 'invalid_credentials'}, status_code=401)
        except EmailRegistrationUnavailable:
            return JSONResponse({'error': 'registration_unavailable'}, status_code=409)
        except PasswordRateLimited as error:
            return JSONResponse({'error': 'rate_limited'}, status_code=429,
                                headers={'Retry-After': str(error.retry_after)})
        inner = await scoped_app(identity.principal_id)
        auth = inner.state.web_auth
        return signed_in(auth, await auth.signin_password(request, proof))

    @app.post('/auth/email/register')
    async def register_email(request: Request):
        return await email_signin(request, register=True)

    @app.post('/auth/email/signin')
    async def signin_email(request: Request):
        return await email_signin(request, register=False)

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
            login_pages = ('/', '/account', '/settings') + (('/google-calendar',) if local_google_calendar is not None else ())
            login_target = '/google-calendar/connect' if request.url.path in ('/', '/google-calendar') and local_google_calendar is not None else '/login'
            response = RedirectResponse(login_target) if request.method == 'GET' and request.url.path in login_pages else JSONResponse({'error': 'access_denied'}, status_code=403)
            await response(scope, receive, send)
            return
        if request.method == 'GET' and request.url.path == '/':
            owner = inner.state.web_auth.policy.config.student.trusted_user_id
            if local_google_calendar is None and await documents.get('cp_moodle_accounts/' + owner) is None:
                await RedirectResponse('/account')(scope, receive, send)
                return
        await inner(scope, receive, send)

    app.mount('/', dispatch)
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Email or Google sign-in and private Moodle accounts on this computer')
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
