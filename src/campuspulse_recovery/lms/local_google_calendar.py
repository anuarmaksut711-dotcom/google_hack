"""Run the existing application with a local, read-only Google connection."""
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..config import CLOUD_MARKERS
from ..integrations.local_calendar import CALLBACK, LocalCalendar, validate_client
from ..storage.sqlite_documents import SqliteDocuments
from .multiuser import create_multiuser_app


SETUP_SCRIPT = r"""'use strict';
const form=document.getElementById('calendar-setup'),status=document.getElementById('setup-status');
form.addEventListener('submit',async event=>{
  event.preventDefault();const button=form.querySelector('button');button.disabled=true;
  try{
    const file=document.getElementById('google-client-file').files[0];
    if(!file||file.size>16384)throw Error();
    const csrf=document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith('__Host-cp-setup='));
    if(!csrf)throw Error();
    const response=await fetch('/local/calendar/setup',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(csrf.split('=')[1])},body:await file.text()});
    if(!response.ok)throw Error();location.replace('/google-calendar');
  }catch{status.textContent='Не удалось сохранить настройки. Нужен JSON веб-клиента указанного проекта с адресом возврата, показанным выше.';}
  finally{button.disabled=false;}
});"""


def setup_html(project_id, origin):
    from html import escape
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Настройка Calendar · Google AI</title>
<link rel="stylesheet" href="/local/calendar/setup.css"></head><body><main>
<h1>Подключение Google Calendar</h1><p>Локальное приложение готово к настройке.
Сначала добавьте OAuth-клиент Google, затем войдите в приложение и предоставьте календарю доступ для чтения.</p>
<ol><li>В Google Cloud откройте проект <strong>''' + escape(project_id) + '''</strong>.
Включите Google Calendar API.</li><li>В Google Auth Platform → Audience добавьте свой Google-аккаунт
в тестовые пользователи, если приложение находится в тестовом режиме.</li>
<li>Создайте OAuth-клиент типа «Веб-приложение» или используйте существующий.
Добавьте разрешённый JavaScript origin:<br><code>''' + escape(origin) + '''</code><br>
и точный адрес возврата:<br><code>''' + escape(origin + CALLBACK) + '''</code></li>
<li>Скачайте JSON этого OAuth-клиента и выберите файл ниже.</li></ol>
<p><a href="https://console.cloud.google.com/auth/clients?project=''' + escape(project_id, quote=True) + '''"
target="_blank" rel="noopener noreferrer">Открыть OAuth-клиенты проекта</a></p>
<form id="calendar-setup"><label for="google-client-file">JSON OAuth веб-клиента Google</label>
<input id="google-client-file" type="file" accept="application/json,.json" required>
<button type="submit">Сохранить и перейти к подключению</button></form>
<p id="setup-status" role="status" aria-live="polite"></p>
<p>Настройки и токены сохраняются зашифрованно в профиле Windows, вне папки проекта.
Сайт в облаке не изменяется. Секрет клиента и токены не нужно отправлять в чат.</p>
</main><script src="/local/calendar/setup.js"></script></body></html>'''


def create_local_app(*, directory, project_id, origin):
    if (CLOUD_MARKERS.intersection(os.environ) or urlsplit(origin).scheme != 'https'
            or urlsplit(origin).hostname != '127.0.0.1' or urlsplit(origin).path
            or not project_id or not project_id.replace('-', '').isalnum()):
        raise ValueError('explicit_local_calendar_configuration_required')
    directory = Path(directory).resolve()
    # Never store OAuth tokens, client secrets or event contents in a checkout.
    for parent in (directory, *directory.parents):
        if (parent / '.git').exists() or (parent / 'AGENTS.md').exists():
            raise ValueError('calendar_storage_must_be_outside_repository')
    documents = SqliteDocuments(directory, project_id=project_id, database_id='local-calendar')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    active, csrf = None, secrets.token_urlsafe(32)

    async def initialize():
        nonlocal active
        if active is None:
            saved = await documents.get('cp_local_calendar_settings/client')
            if saved:
                config = validate_client(saved, project_id=project_id, origin=origin)
                calendar = LocalCalendar(documents, client=config, project_id=project_id, origin=origin)
                active = create_multiuser_app(origin=origin, client_id=config['web']['client_id'],
                    directory=directory / 'accounts', local_google_calendar=calendar)
        return active

    @app.middleware('http')
    async def local_only(request, call_next):
        if (request.headers.get('host') != urlsplit(origin).netloc
                or request.url.scheme != 'https'
                or request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient')):
            return Response(status_code=403)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'})
        return response

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'mode': 'local', 'calendar_configured': await initialize() is not None}

    @app.get('/local/calendar/setup.css')
    async def css():
        return Response('body{font:16px/1.6 system-ui;margin:0;background:#f6f5f2;color:#202520}'
            'main{max-width:660px;margin:5vh auto;padding:28px;background:white;border-radius:20px}'
            'li{margin:12px 0}code{overflow-wrap:anywhere}input{display:block;margin:14px 0;max-width:100%}'
            'button{font:inherit;padding:10px 16px}a{color:#245c43}'
            '@media(max-width:700px){main{margin:12px;padding:20px}}', media_type='text/css')

    @app.get('/local/calendar/setup.js')
    async def script():
        return Response(SETUP_SCRIPT, media_type='text/javascript')

    @app.post('/local/calendar/setup')
    async def setup(request: Request):
        if (request.headers.get('origin') != origin or request.query_params
                or request.headers.get('sec-fetch-site') not in (None, 'same-origin')
                or request.headers.get('x-csrf-token') != csrf
                or request.cookies.get('__Host-cp-setup') != csrf):
            return Response(status_code=403)
        if await initialize() is not None:
            return JSONResponse({'error': 'already_configured'}, status_code=409)
        try:
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                raise ValueError()
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 16384:
                    raise ValueError()
            value = validate_client(json.loads(raw), project_id=project_id, origin=origin)
            async def save(tx):
                previous = await tx.get('cp_local_calendar_settings/client')
                if previous is not None:
                    raise ValueError('already_configured')
                tx.create('cp_local_calendar_settings/client', value)
            await documents.transact(save)
            await initialize()
            return {'configured': True}
        except (ValueError, TypeError, KeyError):
            return JSONResponse({'error': 'invalid_google_web_client'}, status_code=400)

    async def dispatch(scope, receive, send):
        inner = await initialize()
        if inner is not None:
            return await inner(scope, receive, send)
        response = HTMLResponse(setup_html(project_id, origin), headers={
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})
        response.set_cookie('__Host-cp-setup', csrf, secure=True, samesite='strict', path='/')
        await response(scope, receive, send)

    app.mount('/', dispatch)
    # A supplied downloaded client is imported by main before requests start.
    app.state.documents, app.state.initialize = documents, initialize
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Local Google AI Google Calendar connection')
    parser.add_argument('--project', required=True)
    parser.add_argument('--port', type=int, default=8981)
    parser.add_argument('--client-file', type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('invalid_port')
    if os.name != 'nt' or not os.environ.get('LOCALAPPDATA'):
        parser.error('This local launcher requires a Windows user profile.')
    directory = Path(os.environ['LOCALAPPDATA']) / 'Google AI' / 'google-calendar' / args.project
    origin = f'https://127.0.0.1:{args.port}'
    app = create_local_app(directory=directory, project_id=args.project, origin=origin)
    if args.client_file:
        try:
            value = validate_client(json.loads(args.client_file.read_text(encoding='utf-8-sig')),
                project_id=args.project, origin=origin)
            async def save_client():
                async def save(tx):
                    previous = await tx.get('cp_local_calendar_settings/client')
                    if previous is not None and previous != value:
                        raise ValueError('calendar_client_already_configured')
                    tx.set('cp_local_calendar_settings/client', value)
                await app.state.documents.transact(save)
            asyncio.run(save_client())
        except Exception:
            parser.error('Client file is invalid or does not match this project and callback.')
    from ..demo.__main__ import certificate
    import uvicorn
    tls = directory / 'tls'
    tls.mkdir(parents=True, exist_ok=True)
    cert, key = certificate(tls)
    # Google OAuth libraries can log tokens at DEBUG; this local entry point
    # disables logs and access URLs (callback query includes an auth code).
    logging.disable(logging.CRITICAL)
    print(f'Local Calendar: {origin}/google-calendar', flush=True)
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port,
            ssl_certfile=str(cert), ssl_keyfile=str(key), access_log=False,
            proxy_headers=False, log_level='critical')).serve())


if __name__ == '__main__':
    main()
