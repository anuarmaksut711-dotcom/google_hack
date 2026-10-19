"""Google Identity Services sign-in with a one-use server nonce and CSRF check."""
from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

SCRIPT = r"""'use strict';
window.addEventListener('load', async () => {
  const status = document.getElementById('login-status');
  try {
    if (!window.google?.accounts?.id) throw new Error('google_unavailable');
    const response = await fetch('/auth/challenge', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('challenge_unavailable');
    const challenge = await response.json();
    google.accounts.id.initialize({
      client_id: document.body.dataset.clientId, nonce: challenge.nonce,
      auto_select: false, ux_mode: 'popup',
      callback: async ({credential}) => {
        status.textContent = 'Проверяем вход…';
        try {
          const result = await fetch('/auth/signin', {method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': challenge.csrf_token},
            body: JSON.stringify({credential})});
          if (!result.ok) throw new Error('signin_rejected');
          window.location.replace('/');
        } catch {
          status.textContent = 'Вход не выполнен. Используйте подключённый к проекту Google-аккаунт и обновите страницу.';
        }
      }
    });
    google.accounts.id.renderButton(document.getElementById('google-signin'),
      {type: 'standard', theme: 'outline', size: 'large', text: 'signin_with', locale: 'ru'});
    status.textContent = 'Войдите, чтобы открыть задания и расписание.';
  } catch {
    status.textContent = 'Сервис входа недоступен. Обновите страницу и попробуйте снова.';
  }
});"""


def install_login(app, auth, client_id, *, moodle=False):
    @app.get('/login', response_class=HTMLResponse)
    async def login(request: Request):
        auth.check_request(request)
        return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Вход · CampusPulse</title><link rel="stylesheet" href="/assets/login.css">'
            '<script src="https://accounts.google.com/gsi/client" defer></script>'
            '<script src="/assets/login.js" defer></script></head>'
            f'<body data-client-id="{escape(client_id, quote=True)}"><main>'
            '<p>CampusPulse</p><h1>Ваш учебный план</h1>'
            + ('<p>Войдите через Google и добавьте свою ссылку календаря Moodle. Здесь будут ваши задания и план.</p>' if moodle else
             '<p>Задания из Google Sheets и расписание в Google Calendar.</p>') +
            '<div id="google-signin"></div><p id="login-status" role="status">Загружаем вход…</p>'
            '<noscript>Для входа включите JavaScript.</noscript></main></body></html>')

    @app.get('/assets/login.js')
    async def script():
        script = SCRIPT.replace('Используйте подключённый к проекту Google-аккаунт и обновите страницу.',
            'Обновите страницу и повторите вход через Google.') if moodle else SCRIPT
        return Response(script, media_type='text/javascript')

    @app.get('/assets/login.css')
    async def css():
        return Response('body{font:17px/1.6 system-ui,sans-serif;background:#f6f5f2;color:#202520;'
            'margin:0;padding:24px}main{max-width:480px;margin:12vh auto;padding:32px;'
            'background:white;border-radius:20px}h1{font-size:32px;line-height:1.2}'
            '#login-status{min-height:3em;color:#505b50}'
            'input{display:block;box-sizing:border-box;width:100%;padding:12px;margin:10px 0 18px;'
            'border:1px solid #788278;border-radius:8px;font:inherit}'
            'button{padding:12px 18px;font:inherit;cursor:pointer}a{color:#245c43}'
            '@media(max-width:520px){body{padding:12px}main{margin:24px auto;padding:20px}}', media_type='text/css')

    @app.middleware('http')
    async def login_headers(request, call_next):
        response = await call_next(request)
        if request.url.path == '/login':
            response.headers['Content-Security-Policy'] = (
                "default-src 'none'; script-src 'self' https://accounts.google.com/gsi/client; "
                "style-src 'self' https://accounts.google.com/gsi/style; "
                "connect-src 'self' https://accounts.google.com/gsi/; "
                "frame-src https://accounts.google.com/gsi/; img-src 'self' https://*.googleusercontent.com; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
        return response
