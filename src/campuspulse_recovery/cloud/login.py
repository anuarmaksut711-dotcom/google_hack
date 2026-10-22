"""Public sign-in surfaces sharing a one-use server nonce and CSRF check."""
from html import escape
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from ..web.assets.theme import THEME_HEAD, install_theme

SCRIPT = r"""'use strict';
document.addEventListener('DOMContentLoaded', async () => {
  const status = document.getElementById('login-status');
  const provider = document.getElementById('google-signin');
  const loading = document.getElementById('login-loading');
  const retry = document.getElementById('login-retry');
  const form = document.getElementById('email-auth');
  const submit = document.getElementById('email-submit');
  const providerStatus = document.getElementById('provider-status');
  const signup = document.body.dataset.authMode === 'signup';
  let submitting = false, lastWidth = 0, challenge;
  retry.addEventListener('click', () => window.location.reload());
  const message = (text, state = 'error') => {
    status.dataset.state = state; status.textContent = text;
  };
  const busy = value => {
    submitting = value;
    if (submit) {
      submit.disabled = value;
      submit.textContent = value ? (signup ? 'Создаём аккаунт…' : 'Входим…') : (signup ? 'Создать аккаунт' : 'Войти');
      form.setAttribute('aria-busy', String(value));
    }
    provider.toggleAttribute('inert', value);
  };
  const unavailable = text => {
    loading.hidden = true; provider.hidden = true; retry.hidden = false;
    message(text);
  };
  if (form) {
    const showFieldError = field => {
      const error = document.getElementById(field.id + '-error');
      if (!error) return;
      const invalid = !field.validity.valid;
      field.toggleAttribute('aria-invalid', invalid);
      if (invalid) field.setAttribute('aria-invalid', 'true');
      error.textContent = invalid ? field.validationMessage : '';
      error.hidden = !invalid;
    };
    form.addEventListener('invalid', event => {
      event.preventDefault();
      showFieldError(event.target);
      if (event.target === form.querySelector(':invalid')) event.target.focus();
    }, true);
    form.addEventListener('input', event => {
      if (event.target.hasAttribute('aria-invalid')) showFieldError(event.target);
    });
    form.addEventListener('reset', () => {
      form.querySelectorAll('[aria-invalid]').forEach(field => field.removeAttribute('aria-invalid'));
      form.querySelectorAll('.auth-field-error').forEach(error => {
        error.textContent = ''; error.hidden = true;
      });
    });
  }
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting || !challenge || !form.checkValidity()) return;
    busy(true); message(signup ? 'Создаём аккаунт…' : 'Проверяем вход…', 'working');
    try {
      const result = await fetch(signup ? '/auth/email/register' : '/auth/email/signin', {
        method: 'POST', credentials: 'same-origin', headers: {
          'Content-Type': 'application/json', 'X-CSRF-Token': challenge.csrf_token},
        body: JSON.stringify({email: form.elements.email.value.trim(), password: form.elements.password.value})});
      if (result.ok) { form.reset(); window.location.replace('/'); return; }
      const errors = {
        invalid_credentials: 'Неверная почта или пароль. Проверьте данные и попробуйте снова.',
        registration_unavailable: 'Не удалось создать аккаунт с этой почтой. Попробуйте войти или укажите другую.',
        email_invalid: 'Проверьте адрес электронной почты.',
        password_length: 'Пароль должен содержать от 15 до 128 символов.',
        rate_limited: 'Слишком много попыток. Подождите 15 минут и попробуйте снова.'
      };
      const detail = await result.json().catch(() => ({}));
      if (result.status === 403) {
        challenge = null; retry.hidden = false;
        message('Время входа истекло. Обновите страницу и попробуйте снова.');
      } else message(errors[detail.error] || 'Сейчас не удалось войти. Попробуйте ещё раз.');
    } catch { message('Нет связи с сервером. Проверьте подключение и попробуйте снова.'); }
    finally { busy(false); if (!challenge) submit.disabled = true; }
  });
  try {
    const response = await fetch('/auth/challenge', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('challenge_unavailable');
    challenge = await response.json();
    if (submit) submit.disabled = false;
  } catch {
    unavailable('Сервис входа недоступен. Обновите страницу и попробуйте снова.');
    return;
  }
  try {
    if (!window.google?.accounts?.id) await new Promise((resolve, reject) => {
      const sdk = document.getElementById('google-sdk');
      const timeout = window.setTimeout(() => reject(new Error('google_unavailable')), 10000);
      sdk.addEventListener('load', () => { window.clearTimeout(timeout); resolve(); }, {once: true});
      sdk.addEventListener('error', () => { window.clearTimeout(timeout); reject(new Error('google_unavailable')); }, {once: true});
    });
    if (!window.google?.accounts?.id) throw new Error('google_unavailable');
    google.accounts.id.initialize({
      client_id: document.body.dataset.clientId, nonce: challenge.nonce,
      auto_select: false, button_auto_select: false, ux_mode: 'popup', context: signup ? 'signup' : 'signin',
      callback: async ({credential}) => {
        if (submitting) return;
        busy(true); message('Проверяем вход…', 'working');
        try {
          const result = await fetch('/auth/signin', {method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': challenge.csrf_token},
            body: JSON.stringify({credential})});
          if (!result.ok) throw new Error('signin_rejected');
          window.location.replace('/');
        } catch {
          challenge = null; busy(false); if (submit) submit.disabled = true;
          unavailable('Вход не выполнен. Используйте подключённый к проекту Google-аккаунт и обновите страницу.');
        }
      }
    });
    const renderButton = () => {
      if (submitting || provider.hidden) return;
      const width = Math.floor(Math.min(400, provider.clientWidth));
      if (!width || width === lastWidth) return;
      lastWidth = width; provider.replaceChildren();
      // Google's medium button excludes account-name/email personalization.
      google.accounts.id.renderButton(provider,
        {type: 'standard', theme: 'filled_black', shape: 'pill', size: 'medium',
          text: 'continue_with', locale: 'ru', logo_alignment: 'center', width});
    };
    provider.hidden = false;
    renderButton();
    const observer = new ResizeObserver(renderButton);
    observer.observe(provider);
    window.addEventListener('pagehide', () => observer.disconnect(), {once: true});
    loading.hidden = true;
    if (!form) message(signup ? 'Аккаунт создастся после подтверждения в Google.' : 'Выберите свой Google-аккаунт, чтобы продолжить.', 'ready');
  } catch {
    if (form) {
      loading.hidden = true; provider.hidden = true;
      providerStatus.hidden = false; providerStatus.textContent = 'Google сейчас недоступен. Можно войти по почте.';
    } else unavailable('Сервис входа недоступен. Обновите страницу и попробуйте снова.');
  }
});"""

# The account-connection screen also uses this asset. Keep its established
# light form styles; the reference-driven dark treatment is auth-page only.
STYLES = r"""
body{font:17px/1.6 system-ui,sans-serif;background:#f6f5f2;color:#202520;margin:0;padding:24px}
main{max-width:480px;margin:12vh auto;padding:32px;background:white;border-radius:20px}
h1{font-size:32px;line-height:1.2}
input,select{display:block;box-sizing:border-box;width:100%;padding:12px;margin:10px 0 18px;border:1px solid #788278;border-radius:8px;font:inherit}
button{padding:12px 18px;font:inherit;cursor:pointer}a{color:#245c43}
@media(max-width:520px){body{padding:12px}main{margin:24px auto;padding:20px}}
body.auth-page {
  --auth-ground:#111; --auth-ink:#f5f5f5; --auth-muted:#aaa; --auth-focus:#e0e0e0;
  color-scheme:dark; box-sizing:border-box; display:grid; place-items:center;
  width:100%; grid-template-columns:minmax(0,1fr);
  min-height:100svh; padding:32px 20px; margin:0;
  background:var(--auth-ground); color:var(--auth-ink);
  font:14px/1.6 'Segoe UI',system-ui,sans-serif; font-synthesis:none;
  scrollbar-color:#555 var(--auth-ground);
}
.auth-page *, .auth-page *::before, .auth-page *::after{box-sizing:border-box}
.auth-page ::selection{background:#666;color:#fff}
.auth-page .auth-card {
  width:min(100%,480px); max-width:480px; min-width:0; margin:0; padding:44px 40px 34px;
  border-radius:30px; text-align:center;
  background:linear-gradient(105deg,#252525 0%,#1b1b1b 42%,#151515 100%);
  box-shadow:0 24px 64px -28px #000b;
}
.auth-page .auth-logo {
  position:relative; width:128px; height:96px; margin:0 auto 12px;
}
.auth-page .auth-logo canvas { position:absolute; inset:0; display:block; width:100%; height:100%; }
.auth-page .auth-logo #welcome-fallback { inset:auto; top:50%; left:50%; width:50%; height:auto; transform:translate(-50%,-50%); }
.auth-page .auth-logo[data-state=loading] canvas { visibility:hidden; }
.auth-page h1{margin:0;font-size:28px;line-height:1.3;letter-spacing:-.03em;font-weight:600;color:var(--auth-ink)}
.auth-page .auth-title{margin:28px 0 8px;font-size:18px;font-weight:500;color:#e7e7e7}
.auth-page .auth-description{max-width:34ch;margin:0 auto;color:var(--auth-muted);font-size:14px;line-height:1.75;text-wrap:balance}
.auth-page .auth-actions{margin-top:32px}
.auth-page .auth-email-form{margin:30px 0 0;text-align:left}
.auth-page .auth-field{display:block;width:100%;height:60px;margin:0 0 14px;padding:0 22px;border:1px solid transparent;border-radius:18px;background:#373737;color:var(--auth-ink);font:inherit;font-size:15px;caret-color:#fff;transition:background-color 160ms ease-out}
.auth-page .auth-field::placeholder{color:#c3c3c3;opacity:1}
.auth-page .auth-field:hover{background:#3d3d3d}
.auth-page .auth-field:focus-visible{outline:2px solid var(--auth-focus);outline-offset:3px}
.auth-page .auth-field:user-invalid,.auth-page .auth-field[aria-invalid=true]{border-color:#e6a69c}
.auth-page .auth-field-error{display:flex;align-items:flex-start;gap:10px;margin:-4px 0 16px;padding:12px 14px;border:1px solid #e6a69c30;border-radius:14px;background:#ffffff05;color:#f0b6ab;font-size:13px;line-height:1.6;overflow-wrap:anywhere}
.auth-page .auth-field-error::before{content:'!';display:grid;place-items:center;flex:0 0 18px;height:18px;margin-top:1px;border:1px solid currentColor;border-radius:50%;font-size:12px;font-weight:600;line-height:1}
.auth-page .auth-field:-webkit-autofill{-webkit-text-fill-color:#f5f5f5;box-shadow:0 0 0 100px #373737 inset}
.auth-page .auth-field-label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.auth-page .auth-password-hint{margin:0 2px;color:var(--auth-muted);font-size:12px;line-height:1.6}
.auth-page #email-submit{display:block;width:100%;min-height:60px;margin:28px 0 0;border:0;border-radius:999px;background:#414141;color:var(--auth-ink);font:500 15px/1.5 'Segoe UI',system-ui,sans-serif;transition:background-color 160ms ease-out}
.auth-page #email-submit:hover{background:#505050}
.auth-page #email-submit:active{background:#353535}
.auth-page #email-submit:disabled{cursor:wait;opacity:.6}
.auth-page[data-email-password=true] .auth-actions{margin-top:16px}
.auth-page[data-email-password=true] #login-status{min-height:22px;margin-top:16px}
.auth-page[data-email-password=true] .auth-switch{margin-top:16px}
.auth-page #provider-status{margin:10px 0 0;font-size:12px;color:var(--auth-muted)}
.auth-page #google-signin{display:flex;align-items:center;justify-content:center;width:100%;min-height:60px;padding:8px 0;border-radius:999px;background:#202124}
.auth-page #google-signin>div{max-width:100%}
/* Match the GIS document's scheme so its transparent canvas stays transparent.
   The button itself keeps Google's filled_black theme. */
.auth-page #google-signin iframe{color-scheme:light}
.auth-page #login-loading{height:60px;border-radius:999px;background:#303030;display:grid;place-items:center;color:#bbb;font-size:14px}
.auth-page #login-status{min-height:48px;margin:18px auto 0;color:var(--auth-muted);font-size:13px;line-height:1.7;max-width:38ch;overflow-wrap:anywhere}
.auth-page #login-status[data-state=error]{color:#f0b6ab}
.auth-page #login-retry{width:100%;min-height:56px;border:0;border-radius:999px;background:#373737;color:#f5f5f5;font-size:14px;font-weight:500;transition:background-color 160ms ease-out}
.auth-page #login-retry:hover{background:#444}
.auth-page #login-retry:active{background:#2b2b2b}
.auth-page .auth-switch{margin:26px 0 0;color:var(--auth-muted);font-size:13px}
.auth-page a{color:#e2e2e2;text-underline-offset:3px;text-decoration-thickness:1px}
.auth-page a:hover{color:#fff}
.auth-page a:focus-visible,.auth-page button:focus-visible{outline:2px solid var(--auth-focus);outline-offset:5px}
.auth-page .auth-note{display:flex;align-items:center;justify-content:center;gap:7px;margin:26px 0 0;font-size:12px;color:var(--auth-muted)}
.auth-page .auth-note svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.auth-page [hidden]{display:none!important}
@media(max-width:520px){
  body.auth-page{padding:24px 16px}
  .auth-page .auth-card{padding:36px 24px 30px;border-radius:26px}
  .auth-page .auth-logo{width:116px;height:87px;margin-bottom:12px}
  .auth-page h1{font-size:26px}
  .auth-page .auth-title{margin-top:24px}
  .auth-page .auth-description{font-size:13px}
  .auth-page .auth-actions{margin-top:28px}
}
@media(prefers-reduced-motion:reduce){.auth-page button{transition:none!important}}
"""


def render_login(client_id, *, moodle=False, signup=False, style_nonce=None, email_password=False):
    """Only advertise password credentials where the composition supplies them."""
    title = 'Регистрация' if signup else 'Вход'
    description = ('Создайте аккаунт и подключите календарь Moodle.' if signup else
        'Войдите, чтобы открыть задания, события календаря и личный план.') if moodle else (
        'Задания из Google Sheets и расписание в Google Calendar.')
    switch = ('Уже есть аккаунт? <a href="/login">Войти</a>' if signup else
        'Нет аккаунта? <a href="/register">Создать аккаунт</a>') if moodle else (
        'Используйте подключённый к проекту Google-аккаунт.')
    nonce_attribute = '' if style_nonce is None else f' nonce="{escape(style_nonce, quote=True)}"'
    email_form = ''
    if email_password:
        hint = '<p id="password-hint" class="auth-password-hint">От 15 до 128 символов. Можно использовать пробелы.</p>' if signup else ''
        email_form = ('<form id="email-auth" class="auth-email-form" method="post">'
            '<label class="auth-field-label" for="auth-email">Электронная почта</label>'
            '<input class="auth-field" id="auth-email" name="email" type="email" placeholder="Электронная почта" '
            'required maxlength="254" autocomplete="username" inputmode="email" autocapitalize="none" spellcheck="false" aria-describedby="auth-email-error">'
            '<p id="auth-email-error" class="auth-field-error" role="alert" hidden></p>'
            '<label class="auth-field-label" for="auth-password">Пароль</label>'
            '<input class="auth-field" id="auth-password" name="password" type="password" placeholder="Пароль" '
            f'required maxlength="128" minlength="{15 if signup else 1}" autocomplete="{"new-password" if signup else "current-password"}"'
            + f' aria-describedby="auth-password-error{" password-hint" if signup else ""}">'
            '<p id="auth-password-error" class="auth-field-error" role="alert" hidden></p>' + hint +
            f'<button id="email-submit" type="submit" disabled>{"Создать аккаунт" if signup else "Войти"}</button></form>')
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f'<title>{title} · Google AI</title><link rel="stylesheet" href="/assets/login.css">' + THEME_HEAD +
        f'<script id="google-sdk" src="https://accounts.google.com/gsi/client"{nonce_attribute} async></script>'
        '<script src="/assets/login.js" defer></script>'
        '<script src="/assets/logo/vendor/gsap.min.js" defer></script>'
        '<script type="module" src="/assets/logo/welcome.js"></script></head>'
        f'<body class="auth-page" data-email-password="{str(email_password).lower()}" data-auth-mode="{"signup" if signup else "signin"}" data-client-id="{escape(client_id, quote=True)}">'
        '<main class="auth-card" aria-labelledby="auth-heading">'
        '<div id="welcome-logo" class="auth-logo" data-state="loading" aria-hidden="true">'
        '<canvas id="welcome-fallback" width="409" height="413" hidden></canvas>'
        '<canvas id="welcome-canvas"></canvas></div>'
        '<h1 id="auth-heading">Google AI</h1>'
        f'<p class="auth-title">{"Создать аккаунт" if signup else "С возвращением"}</p>'
        + ('' if email_password else f'<p class="auth-description">{description}</p>') + email_form +
        '<div class="auth-actions"><div id="login-loading" aria-hidden="true">Подключаем Google…</div>'
        '<div id="google-signin" hidden></div><button id="login-retry" type="button" hidden>Попробовать снова</button></div>'
        f'<p id="provider-status" hidden></p><p id="login-status" role="status" aria-live="polite" aria-atomic="true">{"" if email_password else "Загружаем вход…"}</p>'
        f'<p class="auth-switch">{switch}</p>'
        + ('' if email_password else '<p class="auth-note"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>Вход и регистрация через Google</p>') +
        '<noscript>Для входа включите JavaScript.</noscript></main></body></html>')


def install_login(app, auth, client_id, *, moodle=False, email_password=False):
    install_theme(app)
    # The sign-in animation uses the same packaged, non-sensitive assets as chat.
    if not any(getattr(route, 'path', None) == '/assets/logo' for route in app.routes):
        app.mount('/assets/logo', StaticFiles(directory=Path(__file__).resolve().parents[1] / 'web/assets/logo'), name='public-welcome-logo')
    @app.get('/login', response_class=HTMLResponse)
    async def login(request: Request):
        auth.check_login_navigation(request)
        return render_login(client_id, moodle=moodle, style_nonce=request.state.login_style_nonce, email_password=email_password)

    if moodle:
        @app.get('/register', response_class=HTMLResponse)
        async def register(request: Request):
            auth.check_login_navigation(request)
            return render_login(client_id, moodle=True, signup=True, style_nonce=request.state.login_style_nonce, email_password=email_password)

    @app.get('/assets/login.js')
    async def script():
        script = SCRIPT.replace('Используйте подключённый к проекту Google-аккаунт и обновите страницу.',
            'Обновите страницу и повторите вход через Google.') if moodle else SCRIPT
        return Response(script, media_type='text/javascript')

    @app.get('/assets/login.css')
    async def css():
        return Response(STYLES, media_type='text/css')

    @app.middleware('http')
    async def login_headers(request, call_next):
        is_auth_page = request.url.path in ('/login', '/register')
        if is_auth_page:
            request.state.login_style_nonce = secrets.token_urlsafe(24)
        response = await call_next(request)
        if is_auth_page:
            response.headers['Content-Security-Policy'] = (
                "default-src 'none'; script-src 'self' https://accounts.google.com/gsi/client; "
                f"style-src 'self' https://accounts.google.com/gsi/style 'nonce-{request.state.login_style_nonce}'; "
                "connect-src 'self' https://accounts.google.com/gsi/; "
                "frame-src https://accounts.google.com/gsi/; img-src 'self' https://*.googleusercontent.com; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
        return response
