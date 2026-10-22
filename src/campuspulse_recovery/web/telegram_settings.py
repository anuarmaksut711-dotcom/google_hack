"""Authenticated Telegram settings. The owner always comes from WebAuth."""
import json

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..domain.ports import BindingConflict


SECTION = '''<section class="settings-telegram" aria-labelledby="telegram-heading">
<h2 id="telegram-heading">Уведомления в Telegram</h2>
<p>Напоминать о дедлайнах за 3 дня (72 часа). Бот проверяет задания каждые 5 минут, даже если сайт закрыт.</p>
<p id="telegram-status" role="status" aria-live="polite">Загружаем настройки Telegram…</p>
<label class="telegram-switch" for="telegram-enabled"><input id="telegram-enabled" type="checkbox" disabled>
<span>Напоминать о дедлайнах</span></label>
<div class="telegram-actions"><button id="telegram-connect" type="button" disabled>Подключить Telegram</button>
<a id="telegram-open" target="_blank" rel="noopener noreferrer" hidden>Открыть бота в Telegram</a>
<button id="telegram-confirm" type="button" hidden>Подтвердить и включить</button></div>
<p class="telegram-help">После подключения нажмите Start у бота, вернитесь сюда и подтвердите свой Telegram. Названия заданий и сроки будут отправляться в этот чат. Выполненные задания пропускаются. Для дедлайнов, до которых при включении осталось меньше 3 дней, напоминание не создаётся.</p>
</section>'''

STYLES = r'''
.settings-telegram { margin: 0 0 32px; padding: 0 0 28px; border-bottom: 1px solid var(--line); }
.settings-telegram h2 { margin: 0 0 8px; font-size: 18px; }
.settings-telegram p { margin: 8px 0 14px; max-width: 72ch; color: var(--muted); font-size: 13px; }
.settings-telegram #telegram-status { color: var(--ink); }
.telegram-switch { display: flex; align-items: center; gap: 12px; width: fit-content; min-height: 44px; margin: 12px 0; cursor: pointer; }
.telegram-switch input { appearance: auto; display: inline-block; width: 20px; height: 20px; min-height: 20px; margin: 0; accent-color: var(--action); cursor: pointer; }
.telegram-switch input:focus-visible { outline: 2px solid var(--focus); outline-offset: 4px; }
.telegram-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.telegram-actions button { min-height: 44px; margin: 0; }
.telegram-actions a { display: inline-flex; align-items: center; min-height: 44px; color: var(--accent); }
.telegram-actions [hidden] { display: none !important; }
.settings-telegram .telegram-help { font-size: 12px; margin-bottom: 0; }
'''

SCRIPT = r'''(() => {
  const root = document.querySelector('.settings-telegram');
  if (!root) return;
  const status = document.getElementById('telegram-status'), enabled = document.getElementById('telegram-enabled');
  const connect = document.getElementById('telegram-connect'), open = document.getElementById('telegram-open');
  const confirm = document.getElementById('telegram-confirm');
  let busy = false, state = null, timer = null;
  const request = async (method, payload) => {
    const headers = {}, options = {method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(15000)};
    if (method === 'POST') {
      const bootstrap = await fetch('/auth/session', {credentials: 'same-origin', cache: 'no-store', headers: {'X-CSRF-Bootstrap': '1'}, signal: AbortSignal.timeout(15000)});
      if (!bootstrap.ok) throw Error('session');
      const session = await bootstrap.json();
      if (typeof session.csrf_token !== 'string') throw Error('session');
      headers['X-CSRF-Token'] = session.csrf_token; headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(payload);
    }
    options.headers = headers;
    const response = await fetch('/api/account/telegram', options);
    if (!response.ok) throw Error(response.status === 403 ? 'session' : 'request');
    return response.json();
  };
  const render = value => {
    state = value; enabled.checked = value.enabled;
    enabled.disabled = busy || !value.configured || !value.connected;
    connect.disabled = busy || !value.configured;
    connect.textContent = value.connected ? 'Подключить другой Telegram' : 'Подключить Telegram';
    confirm.hidden = !value.confirmation_required; confirm.disabled = busy;
    if (!value.pending) open.hidden = true;
    status.textContent = !value.configured ? 'Telegram пока не настроен администратором.' :
      value.confirmation_required ? 'Подтвердите подключение: ' + value.pending_label :
      value.pending ? 'Нажмите Start у бота и вернитесь в настройки. Ссылка действует 10 минут.' :
      value.connected ? (value.enabled ? 'Уведомления включены. ' : 'Уведомления выключены. ') + value.label :
      'Подключите свой Telegram, чтобы получать напоминания.';
    clearTimeout(timer);
    if (value.pending) timer = setTimeout(load, 3000);
  };
  async function load() {
    if (busy) return;
    try { render(await request('GET')); }
    catch { status.textContent = 'Не удалось загрузить Telegram. Обновите страницу и попробуйте снова.'; }
  }
  async function mutate(payload) {
    if (busy) return;
    busy = true; enabled.disabled = true; connect.disabled = true; confirm.disabled = true;
    try {
      const result = await request('POST', payload);
      if (result.url) {
        if (!/^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{4,31}\?start=[A-Za-z0-9_-]{43}$/.test(result.url)) throw Error('url');
        open.href = result.url; open.hidden = false;
      }
      busy = false; render(result.state);
    } catch (error) {
      busy = false;
      if (state) render(state);
      status.textContent = error.message === 'session' ? 'Войдите заново, чтобы изменить Telegram.' :
        'Изменение не подтверждено. Обновите страницу и повторите действие.';
    }
  }
  connect.addEventListener('click', () => mutate({action: 'connect'}));
  confirm.addEventListener('click', () => mutate({action: 'confirm'}));
  enabled.addEventListener('change', () => mutate({action: 'enabled', enabled: enabled.checked}));
  window.addEventListener('pagehide', () => clearTimeout(timer), {once: true});
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !busy) load(); });
  load();
})();'''


async def bounded_json(request, limit=8192):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise ValueError('body_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('object_required')
    return value


def install_user_routes(app, *, auth, telegram):
    def owner(credential):
        with auth.request_scope(credential) as context:
            auth.policy.require_context(context)
            return auth.policy.config.student.trusted_user_id

    async def status(owner_id):
        return (await telegram.status(owner_id) if telegram else
                {'configured': False, 'connected': False, 'enabled': False, 'label': '',
                 'pending': False, 'confirmation_required': False, 'pending_label': ''})

    @app.get('/assets/telegram-settings.js')
    async def script(request: Request):
        await auth.load(request)
        return Response(SCRIPT, media_type='text/javascript')

    @app.get('/api/account/telegram')
    async def read(request: Request):
        credential = await auth.load(request)
        if request.query_params:
            return JSONResponse({'error': 'selectors_forbidden'}, status_code=400)
        return await status(owner(credential))

    @app.post('/api/account/telegram')
    async def change(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            return JSONResponse({'error': 'selectors_forbidden'}, status_code=400)
        try:
            value = await bounded_json(request)
            action = value.get('action')
            expected = {'action', 'enabled'} if action == 'enabled' else {'action'}
            if set(value) != expected or action not in ('connect', 'confirm', 'enabled'):
                raise ValueError('invalid_request')
            if action == 'enabled' and type(value['enabled']) is not bool:
                raise ValueError('invalid_request')
        except (ValueError, TypeError):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        if telegram is None or not telegram.configured:
            return JSONResponse({'error': 'telegram_not_configured'}, status_code=409)
        owner_id = owner(credential)
        try:
            result = {}
            if action == 'connect':
                result = await telegram.connect(owner_id)
            elif action == 'confirm':
                await telegram.confirm(owner_id)
            else:
                await telegram.set_enabled(owner_id, value['enabled'])
            return {**result, 'state': await telegram.status(owner_id)}
        except BindingConflict:
            return JSONResponse({'error': 'telegram_connection_not_confirmed'}, status_code=409)
