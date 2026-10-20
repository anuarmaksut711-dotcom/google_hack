"""Explicit single-user, loopback-only Moodle calendar viewer using the product UI."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.requests import Request

from ..config import AppConfig
from ..demo.adapters import MemoryDocuments, demo_config
from ..demo.__main__ import certificate
from ..security.context import AccessDenied
from ..security.resources import ResourcePolicy
from ..web.auth import VerifiedSignIn
from ..web.moodle import install_moodle_routes
from ..web.views.app import create_ui_app
from .calendar_feed import CalendarFeed


def create_local_calendar_app(*, origin: str, url_file: Path, cache_file: Path,
                              user_id: int, transport=None, chat_agent=None):
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('literal_loopback_https_required')
    values = demo_config().model_dump()
    values['student']['trusted_user_id'] = f'local-moodle-{user_id}'
    values['student']['allowed_signin_principal'] = f'https://local.invalid#moodle-{user_id}'
    config = AppConfig.model_validate(values)
    policy = ResourcePolicy(config, environment='local')
    documents = MemoryDocuments(config, demo=True)
    access = secrets.token_urlsafe(32)

    class LocalSignIn:
        async def verify(self, token, *, nonce, now):
            if not secrets.compare_digest(token, access) or not nonce:
                raise AccessDenied('local_signin_rejected')
            return VerifiedSignIn(principal_id=config.student.allowed_signin_principal,
                authenticated_at=now, expires_at=now + timedelta(hours=12))

    feed = CalendarFeed(url_file=url_file, cache_file=cache_file,
        owner_id=config.student.trusted_user_id, user_id=user_id, transport=transport)
    from .calendar_projection import CalendarChatProjection
    projection = CalendarChatProjection(feed, policy)
    app = create_ui_app(policy=policy, documents=documents, verifier=LocalSignIn(),
        origin=origin, environment='local', moodle_enabled=True, signin_url='/local',
        chat_agent=chat_agent, projection=projection, chat_mode='calendar_advice')
    auth = app.state.web_auth

    async def read(context):
        policy.require_context(context)
        policy.require_owner(context, feed.owner_id)
        return feed.state()

    async def refresh(context):
        policy.require_context(context)
        policy.require_owner(context, feed.owner_id)
        await feed.refresh()

    async def update_local_status(context, task_id, done):
        policy.require_context(context)
        policy.require_owner(context, feed.owner_id)
        await feed.set_local_status(task_id, done)

    install_moodle_routes(app, auth=auth, reader=read, refresh=refresh, source_kind='calendar_feed',
        update_local_status=update_local_status)

    @asynccontextmanager
    async def lifespan(app):
        await feed.refresh()
        yield
    app.router.lifespan_context = lifespan

    @app.get('/login')
    async def login():
        return RedirectResponse('/local')

    @app.get('/local', response_class=HTMLResponse)
    async def entry(request: Request):
        auth.check_request(request)
        return ('<!doctype html><html lang="ru"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Google AI · Moodle</title><h1>Google AI · Moodle</h1>'
            '<p>Личный календарь Moodle на этом компьютере. Оценки и статусы сдачи не передаются.</p>'
            '<p>Повторно вводить пароль Moodle не нужно.</p>'
            '<button id="local-signin">Открыть мои дедлайны</button><p id="result" role="status"></p>'
            '<script src="/local-entry.js"></script></html>')

    @app.get('/local-entry.js')
    async def signin_js(request: Request):
        auth.check_request(request)
        # This credential only establishes a local application session. It is not
        # the Moodle calendar key; no Google or Moodle credential reaches HTML/JS.
        import json
        return Response("document.getElementById('local-signin').onclick=async()=>{try{"
            "const c=await(await fetch('/auth/challenge')).json();"
            "const r=await fetch('/auth/signin',{method:'POST',headers:{'Content-Type':'application/json',"
            "'X-CSRF-Token':c.csrf_token},body:JSON.stringify({credential:" + json.dumps(access) + "})});"
            "if(!r.ok)throw Error();location.href='/#moodle';}catch{"
            "document.getElementById('result').textContent='Не удалось открыть локальную сессию. Обновите страницу.';}};",
            media_type='text/javascript')

    @app.middleware('http')
    async def local_pages(request, call_next):
        response = await call_next(request)
        if request.url.path in ('/local', '/local-entry.js'):
            response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        return response
    return app, feed


def main():
    parser = argparse.ArgumentParser(description='Local single-user AITU Moodle calendar')
    parser.add_argument('--url-file', type=Path, required=True)
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--port', type=int, default=8959)
    parser.add_argument('--chat-target', type=Path,
        help='Non-secret Gemini provider/model settings; API key comes from environment.')
    args = parser.parse_args()
    if args.user_id < 1 or not 1024 <= args.port <= 65535:
        parser.error('invalid local port or user id')
    root = Path.cwd()
    origin = f'https://127.0.0.1:{args.port}'
    chat_agent = None
    if args.chat_target is not None:
        from ..agent.conversation import AdkConversationAgent, load_conversation_target, require_chat_credentials
        try:
            target = load_conversation_target(args.chat_target)
            require_chat_credentials(target)
            chat_agent = AdkConversationAgent(target, profile='calendar_advice')
        except ValueError:
            parser.error('Cannot configure Gemini; check provider/model and GEMINI_API_KEY in environment.')
    try:
        app, _ = create_local_calendar_app(origin=origin, url_file=args.url_file,
            cache_file=root / '.campuspulse-local/moodle-calendar-cache.json', user_id=args.user_id,
            chat_agent=chat_agent)
    except Exception:
        parser.error('Cannot load local calendar settings; check URL file and user id.')
    runtime = root / '.campuspulse-local/calendar-runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    cert, key = certificate(runtime)
    import uvicorn
    print(f'Open {origin}/local (local HTTPS certificate).', flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
        ssl_certfile=str(cert), ssl_keyfile=str(key), proxy_headers=False,
        access_log=False, log_level='warning'))
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(server.serve())


if __name__ == '__main__':
    main()
