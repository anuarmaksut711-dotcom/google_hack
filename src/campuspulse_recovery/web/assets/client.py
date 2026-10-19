"""Small browser adapter; GET refreshes never consent or dispatch execution."""
SCRIPT = r"""'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let scope = null, activeRun = null, busy = false, cursor = 0, traceBusy = false, generation = 0;
  const feedback = (message, error = false) => {
    $('feedback').textContent = message;
    $('feedback').dataset.error = String(error);
  };
  const csrf = () => {
    const values = document.cookie.split(';').map(x => x.trim()).filter(x => x.startsWith('__Host-cp-csrf='));
    if (values.length !== 1) throw new Error('session');
    return decodeURIComponent(values[0].slice('__Host-cp-csrf='.length));
  };
  async function api(path, payload) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error'};
    if (payload !== undefined) Object.assign(options, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf()}, body: JSON.stringify(payload)});
    const response = await fetch(path, options);
    if (!response.ok) {
      const messages = {403: 'Сессия недействительна или действие отклонено. Войдите через настроенный вход.',
        409: 'План или запрос изменился. Обновите состояние и изучите точную версию.',
        503: 'Результат запроса не подтверждён. Обновите сохранённое состояние.'};
      throw new Error(messages[response.status] || 'Не удалось получить результат. Обновите состояние.');
    }
    return response;
  }
  // Storage contains only technical request keys/run IDs; never tokens/source text.
  // A failure to persist disables mutations before sending them.
  function stored(name, value) {
    if (!scope) throw new Error('Сначала требуется вход.');
    const key = 'campuspulse-ui-v1:' + scope + ':' + name;
    if (value !== undefined) localStorage.setItem(key, JSON.stringify(value));
    const raw = localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  }
  function keyFor(name) {
    let key = stored(name);
    if (!key) { key = 'ui_' + crypto.randomUUID(); stored(name, key); }
    return key;
  }
  function reference() {
    const node = $('plan-reference');
    if (!node) throw new Error('Сначала загрузите сохранённую версию плана.');
    return {plan_id: node.dataset.planId, version: Number(node.dataset.version), payload_hash: node.dataset.hash};
  }
  function controls() {
    document.querySelectorAll('[data-action]').forEach(button => { button.disabled = busy; });
    $('start').disabled = busy || !scope || $('panel').querySelector('[data-runtime="true"]') === null;
    $('reload').disabled = busy;
    $('signout').disabled = busy;
    $('trace').disabled = !activeRun || traceBusy;
  }
  async function refresh() {
    const revision = ++generation;
    $('panel').setAttribute('aria-busy', 'true');
    // Clear stale consent buttons before reading any changed plan/source state.
    $('panel').replaceChildren();
    $('trace-records').replaceChildren(); cursor = 0; $('trace-more').hidden = true;
    $('trace-health').textContent = 'Загрузите журнал выбранного запуска. Обновление не подтверждает результат.';
    try {
      const session = await (await api('/auth/session')).json();
      scope = session.session_id;
      const selector = new URLSearchParams(location.search);
      activeRun = selector.get('run_id') || stored('run');
      const query = new URLSearchParams();
      if (activeRun) query.set('run_id', activeRun);
      else if (selector.has('plan_id') && selector.has('version')) {
        query.set('plan_id', selector.get('plan_id')); query.set('version', selector.get('version'));
      }
      const text = await (await api('/ui/state?' + query)).text();
      if (revision !== generation) return;
      // Only the same-origin escaped server renderer supplies markup here.
      const parsed = new DOMParser().parseFromString(text, 'text/html');
      $('panel').replaceChildren(...parsed.body.childNodes);
      $('signout').hidden = false;
      feedback('Сохранённое состояние загружено. Обновление не запускает запись.');
      if (activeRun) {
        stored('run', activeRun);
        const locationQuery = new URLSearchParams({run_id: activeRun});
        history.replaceState(null, '', '/?' + locationQuery);
      }
    } catch (error) {
      if (revision === generation) { scope = null; activeRun = null; }
      throw error;
    } finally { $('panel').setAttribute('aria-busy', 'false'); controls(); }
  }
  async function act(action) {
    if (busy) return;
    busy = true; controls();
    feedback(action === 'apply' ? 'Запрошено применение. Ожидаем сохранённый результат…' : 'Отправляем действие…');
    try {
      if (action === 'start') {
        // Known previous result => a distinct explicit recovery request. Lost
        // response => retain the original key for durable lookup, never auto POST.
        let pending = stored('pending-start');
        if (!pending) { pending = 'ui_' + crypto.randomUUID(); stored('pending-start', pending); }
        const run = await (await api('/api/runs', {request_key: pending})).json();
        stored('run', run.run_id); activeRun = run.run_id;
        stored('pending-start', null);
      } else {
        const plan = reference();
        const suffix = plan.plan_id + ':' + plan.version + ':' + plan.payload_hash;
        const request_key = keyFor(action + ':' + suffix);
        if (action === 'apply') {
          const run = await (await api('/api/runs/continue', {request_key, plan})).json();
          stored('run', run.run_id); activeRun = run.run_id;
        } else if (action === 'approve' || action === 'revoke') {
          await api(action === 'approve' ? '/api/approval' : '/api/approval/revoke', {...plan, request_key});
        }
      }
      if (activeRun) history.replaceState(null, '', '/?' + new URLSearchParams({run_id: activeRun}));
      cursor = 0; $('trace-records').replaceChildren(); $('trace-more').hidden = true;
      await refresh();
    } catch (error) {
      // Lost POST responses never become success or automatic execution retries.
      $('panel').replaceChildren();
      feedback(error.message === 'session' ? 'Войдите через настроенный вход.' :
        error.message + ' Если ответ потерян, результат остаётся неизвестным. Повтор того же действия использует сохранённый ключ.', true);
    } finally { busy = false; controls(); }
  }
  async function trace(reset = false) {
    if (!activeRun || traceBusy) return;
    const revision = generation, run = activeRun;
    traceBusy = true; controls(); $('trace-more').disabled = true;
    try {
      if (reset) { cursor = 0; $('trace-records').replaceChildren(); }
      const page = await (await api('/api/runs/' + encodeURIComponent(activeRun) + '/trace?after=' + cursor)).json();
      if (revision !== generation || run !== activeRun) return;
      for (const record of page.records) {
        const o = record.observation, event = o.event;
        const li = document.createElement('li'), title = document.createElement('strong'), pre = document.createElement('pre');
        title.textContent = (event.tool || 'Запуск') + ' · ' + event.phase + ' · ' + event.status;
        // T23 bounded allowlist projection, never arbitrary result/source bodies.
        pre.textContent = JSON.stringify({run_id: event.run_id, layer: o.layer, call_id: event.call_id,
          operation_id: event.operation_id, attempt_id: o.attempt_id, sequence: event.sequence,
          occurred_at: event.occurred_at, duration_ms: event.duration_ms,
          result: {item_count: o.summary.item_count, operation_count: o.summary.operation_count,
            completed_count: o.summary.completed_count, complete: o.summary.complete,
            verification_status: o.summary.verification_status}}, null, 2);
        li.append(title, pre); $('trace-records').append(li);
      }
      const progressed = page.next > cursor;
      cursor = page.next;
      $('trace-more').hidden = !progressed;
      $('trace-health').textContent = page.records.length ?
        'Показана страница наблюдений. Журнал не заменяет проверку календаря; raw данные источников скрыты.' :
        'На этой странице нет записей. Пустой журнал не подтверждает результат.';
    } catch (error) {
      if (revision === generation && run === activeRun)
        $('trace-health').textContent = 'Журнал недоступен или неполон. ' + error.message;
    } finally { traceBusy = false; controls(); $('trace-more').disabled = false; }
  }
  $('start').addEventListener('click', () => act('start'));
  $('reload').addEventListener('click', () => refresh().catch(error => feedback(error.message, true)));
  $('panel').addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (button && !button.disabled) act(button.dataset.action);
  });
  $('trace').addEventListener('click', () => trace(true));
  $('trace-more').addEventListener('click', () => trace(false));
  $('signout').addEventListener('click', async () => {
    if (busy) return;
    try { await api('/auth/signout', {}); ++generation; scope = null; activeRun = null;
      $('panel').replaceChildren(); $('trace-records').replaceChildren(); $('signout').hidden = true;
      $('trace-health').textContent = 'Войдите, чтобы загрузить журнал.';
      history.replaceState(null, '', '/'); feedback('Вы вышли. Для просмотра плана требуется вход.'); controls();
    } catch (error) { feedback(error.message, true); }
  });
  // Initial load performs ONLY authenticated reads. No timer dispatches actions.
  refresh().catch(error => feedback(error.message, true));
})();
"""
