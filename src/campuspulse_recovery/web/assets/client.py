"""Small browser adapter; GET refreshes never consent or dispatch execution."""
SCRIPT = r"""'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let scope = null, activeRun = null, busy = false, cursor = 0, traceBusy = false, generation = 0;
  let chatBusy = false, chatAvailable = false, chatWorking = false, pendingText = null;
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
      if (path === '/api/chat') {
        messages[409] = 'Запрос чата уже выполняется, был изменён или достигнут лимит диалога. Обновите историю.';
        messages[503] = 'Агент недоступен. Проверьте настройку Vertex AI на сервере.';
      }
      throw new Error(messages[response.status] || 'Не удалось получить результат. Обновите состояние.');
    }
    return response;
  }


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
    document.querySelectorAll('[data-action]').forEach(button => { button.disabled = busy || chatBusy; });
    $('start').disabled = busy || chatBusy || !scope || $('panel').querySelector('[data-runtime="true"]') === null;
    $('reload').disabled = busy || chatBusy;
    $('signout').disabled = busy || chatBusy;
    $('trace').disabled = !activeRun || traceBusy;
    $('chat-send').disabled = busy || chatBusy || chatWorking || !scope || !chatAvailable || !$('recovery-message').value.trim();
    $('recovery-message').readOnly = chatBusy;
    $('chat-form').setAttribute('aria-busy', String(chatBusy));
  }
  function showChat(chat) {
    chatAvailable = chat.available;
    chatWorking = chat.turns.some(turn => turn.status === 'working');
    const log = $('chat-history');
    log.replaceChildren();
    for (const turn of chat.turns) {
      const user = document.createElement('article'), label = document.createElement('span'), text = document.createElement('p');
      user.className = 'chat-message chat-user'; label.className = 'chat-author';
      label.textContent = 'Вы'; text.textContent = turn.message; user.append(label, text); log.append(user);
      const reply = document.createElement('article'), author = document.createElement('span'), answer = document.createElement('p');
      reply.className = 'chat-message chat-assistant'; author.className = 'chat-author'; author.textContent = 'CampusPulse';
      if (turn.status === 'completed') answer.textContent = turn.response;
      else {
        reply.classList.add('chat-system');
        author.textContent = 'Статус запроса';
        answer.textContent = {working: 'Агент готовит ответ. Обновите состояние, если вкладка была перезагружена.',
          failed: 'Агент не смог ответить. Проверьте подключение и отправьте сообщение снова.',
          unknown: 'Ответ не получен. Этот запрос не будет отправлен автоматически.'}[turn.status];
      }
      reply.append(author, answer);
      const details = document.createElement('details'), summary = document.createElement('summary'), meta = document.createElement('p');
      summary.textContent = 'Сведения о запросе';
      meta.textContent = 'ID: ' + turn.request_key + ' · ' + new Date(turn.created_at).toLocaleString('ru-RU') +
        (turn.duration_ms === null ? '' : ' · ' + (turn.duration_ms / 1000).toFixed(1) + ' с');
      details.append(summary, meta); reply.append(details); log.append(reply);
    }
    log.hidden = !chat.turns.length;
    $('workspace').classList.toggle('has-chat', chat.turns.length > 0);
    if (chat.turns.length) document.dispatchEvent(new Event('campuspulse:chat-history'));
    $('chat-status').textContent = chatAvailable ?
      'Чат с агентом · изменения календаря применяются через согласование плана.' :
      'Агент не подключён к этому серверу. Для ответов нужно настроить Vertex AI.';
    controls();
  }
  async function loadChat() {
    const revision = generation, session = scope;
    try {
      const chat = await (await api('/api/chat')).json();
      if (revision !== generation || session !== scope) return;
      showChat(chat);
      const pending = stored('pending-chat');
      if (pending && chat.turns.some(turn => turn.request_key === pending)) {
        stored('pending-chat', null); pendingText = null;
      }
    } catch (error) {
      if (revision !== generation || session !== scope) return;
      chatAvailable = false;
      $('chat-status').textContent = 'История чата недоступна. ' + error.message;
      controls();
    }
  }
  async function sendMessage(event) {
    event.preventDefault();
    if ($('chat-send').disabled) return;
    const message = $('recovery-message').value.trim();
    if (pendingText !== null && pendingText !== message) {
      $('chat-status').textContent = 'Ответ предыдущего запроса неизвестен. Нажмите «Обновить состояние» перед новым сообщением.';
      return;
    }
    chatBusy = true; controls();
    $('chat-status').textContent = 'Агент готовит ответ…';
    try {
      const request_key = keyFor('pending-chat'); pendingText = message;
      const chat = await (await api('/api/chat', {request_key, message})).json();
      stored('pending-chat', null); pendingText = null;
      $('recovery-message').value = '';
      showChat(chat);
      $('chat-history').lastElementChild?.scrollIntoView({block: 'nearest', behavior: 'instant'});
    } catch (error) {
      $('chat-status').textContent = 'Не удалось получить ответ. Текст сохранён в поле. ' + error.message + ' Нажмите «Обновить состояние», чтобы проверить историю.';
    } finally { chatBusy = false; controls(); $('recovery-message').focus({preventScroll: true}); }
  }
  async function refresh() {
    const revision = ++generation;
    $('panel').setAttribute('aria-busy', 'true');

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

      const parsed = new DOMParser().parseFromString(text, 'text/html');
      $('panel').replaceChildren(...parsed.body.childNodes);
      $('signout').hidden = false;
      await loadChat();
      if (revision !== generation) return;
      feedback('Сохранённое состояние загружено. Обновление не запускает запись.');
      if (activeRun) {
        stored('run', activeRun);
        const locationQuery = new URLSearchParams({run_id: activeRun});
        history.replaceState(null, '', '/?' + locationQuery);
      }
    } catch (error) {
      if (revision === generation) {
        scope = null; activeRun = null; chatAvailable = false; chatWorking = false;
        $('chat-history').replaceChildren(); $('chat-history').hidden = true;
        $('workspace').classList.remove('has-chat');
        $('chat-status').textContent = 'Войдите, чтобы открыть чат.';
      }
      throw error;
    } finally { $('panel').setAttribute('aria-busy', 'false'); controls(); }
  }
  async function act(action) {
    if (busy || chatBusy) return;
    busy = true; controls();
    feedback(action === 'apply' ? 'Запрошено применение. Ожидаем сохранённый результат…' : 'Отправляем действие…');
    try {
      if (action === 'start') {


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

      $('panel').focus({preventScroll: true});
      $('panel').scrollIntoView({block: 'start', behavior: 'instant'});
    } catch (error) {

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
        const li = document.createElement('li'), details = document.createElement('details'),
          title = document.createElement('summary'), pre = document.createElement('pre');
        title.textContent = (event.tool || 'Запуск') + ' · ' + event.phase + ' · ' + event.status;

        pre.textContent = JSON.stringify({run_id: event.run_id, layer: o.layer, call_id: event.call_id,
          operation_id: event.operation_id, attempt_id: o.attempt_id, sequence: event.sequence,
          occurred_at: event.occurred_at, duration_ms: event.duration_ms,
          result: {item_count: o.summary.item_count, operation_count: o.summary.operation_count,
            completed_count: o.summary.completed_count, complete: o.summary.complete,
            verification_status: o.summary.verification_status}}, null, 2);
        details.append(title, pre); li.append(details); $('trace-records').append(li);
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
  $('chat-form').addEventListener('submit', sendMessage);
  $('recovery-message').addEventListener('input', controls);
  $('recovery-message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!$('chat-send').disabled) $('chat-form').requestSubmit();
    }
  });
  const updateNavigation = () => {
    const selected = location.hash || '#workspace';
    document.querySelectorAll('.nav-link').forEach(link => {
      const current = link.getAttribute('href') === selected;
      link.classList.toggle('current', current);
      if (current) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  };
  window.addEventListener('hashchange', updateNavigation);
  updateNavigation();
  $('reload').addEventListener('click', () => refresh().catch(error => feedback(error.message, true)));
  $('panel').addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (button && !button.disabled) act(button.dataset.action);
  });
  $('trace').addEventListener('click', () => trace(true));
  $('trace-more').addEventListener('click', () => trace(false));
  $('signout').addEventListener('click', async () => {
    if (busy || chatBusy) return;
    try { await api('/auth/signout', {}); ++generation; scope = null; activeRun = null;
      $('panel').replaceChildren(); $('trace-records').replaceChildren(); $('signout').hidden = true;
      $('chat-history').replaceChildren(); $('chat-history').hidden = true;
      $('workspace').classList.remove('has-chat'); $('recovery-message').value = '';
      chatAvailable = false; chatWorking = false; pendingText = null;
      $('chat-status').textContent = 'Войдите, чтобы открыть чат.';
      $('trace-health').textContent = 'Войдите, чтобы загрузить журнал.';
      history.replaceState(null, '', '/'); feedback('Вы вышли. Для просмотра плана требуется вход.'); controls();
    } catch (error) { feedback(error.message, true); }
  });

  refresh().catch(error => feedback(error.message, true));
})();
"""
