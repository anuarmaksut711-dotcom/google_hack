"""Small browser adapter; GET refreshes never consent or dispatch execution."""
SCRIPT = r"""'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let scope = null, activeRun = null, busy = false, cursor = 0, traceBusy = false, generation = 0;
  let chatBusy = false, chatAvailable = false, chatWorking = false, pendingText = null;
  const moodleEnabled = document.body.dataset.moodleEnabled === 'true';
  let moodleRevision = 0, moodleBusy = false;
  const views = {
    assistant: ['Ассистент', 'Спланируем учёбу вместе'],
    tasks: ['Мои задания', 'Задания и ближайшие сроки'],
    plan: ['Мой план', 'Изменения календаря и ваше подтверждение'],
    journal: ['Журнал', 'Действия агента и результаты запуска']
  };
  function setNavigationOpen(open) {
    document.body.dataset.navOpen = String(open);
    $('mobile-nav-toggle')?.setAttribute('aria-expanded', String(open));
  }
  function updateNavigation() {
    const selector = new URLSearchParams(location.search);
    const selected = location.hash || (selector.has('plan_id') && selector.has('version') ? '#panel' : '#workspace');
    let view = {'#workspace': 'assistant', '#moodle': 'tasks', '#sources': 'plan',
      '#diff-heading': 'plan', '#panel': 'plan', '#review-heading': 'plan', '#journal-heading': 'journal'}[selected] || 'assistant';
    if (view === 'tasks' && !moodleEnabled) view = 'plan';
    document.body.dataset.view = view;
    if ($('view-title')) $('view-title').textContent = views[view][0];
    if ($('view-subtitle')) $('view-subtitle').textContent = views[view][1];
    document.title = 'CampusPulse · ' + views[view][0];
    document.querySelectorAll('.nav-link, .rail-link').forEach(link => {
      const current = link.dataset.view === view;
      link.classList.toggle('current', current);
      if (current) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
      if (current) {
        const group = link.closest('details');
        if (group) group.open = true;
      }
    });
    setNavigationOpen(false);
    requestAnimationFrame(resizeComposer);
  }
  function focusPanel() {
    if (location.hash !== '#panel') location.hash = 'panel';
    updateNavigation();
    $('panel').focus({preventScroll: true});
    $('panel').scrollIntoView({block: 'start', behavior: 'instant'});
  }
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
    if (path.startsWith('/api/moodle')) options.signal = AbortSignal.timeout(110000);
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
      const error = new Error(messages[response.status] || 'Не удалось получить результат. Обновите состояние.');
      error.status = response.status;
      throw error;
    }
    return response;
  }

  function clearMoodle(message) {
    if (!moodleEnabled) return;
    $('moodle-tasks').replaceChildren(); $('moodle-courses').replaceChildren();
    $('moodle-status').textContent = message;
  }
  function moodleDate(value) {
    return new Date(value).toLocaleString('ru-RU', {timeZone: document.body.dataset.timezone,
      dateStyle: 'medium', timeStyle: 'short'});
  }
  function moodleGrade(item) {
    // A published zero is a score; an absent score is never inferred as zero.
    if (item.grade_value !== null && item.grade_value !== undefined)
      return String(item.grade_value) + (item.grade_max === null || item.grade_max === undefined ? '' : ' / ' + item.grade_max);
    if (item.grade_display !== null && item.grade_display !== undefined && item.grade_display !== '')
      return item.grade_display + (item.grade_max === null || item.grade_max === undefined ? '' : ' (максимум ' + item.grade_max + ')');
    return 'Ещё не оценено';
  }
  function moodleLink(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' || url.hostname !== 'lms.astanait.edu.kz' || url.port ||
          url.username || url.password || url.hash || !/^\/mod\/(assign|quiz)\/view\.php$/.test(url.pathname) ||
          [...url.searchParams.keys()].some(key => key !== 'id') ||
          url.searchParams.getAll('id').length !== 1 || !/^\d+$/.test(url.searchParams.get('id'))) return null;
      return url.href;
    } catch { return null; }
  }
  function showMoodle(state) {
    const tasks = $('moodle-tasks'), courses = $('moodle-courses');
    tasks.replaceChildren(); courses.replaceChildren();
    const snapshot = state.snapshot;
    const calendarFeed = state.source_kind === 'calendar_feed';
    if (calendarFeed) {
      $('moodle-heading').textContent = 'Дедлайны из Moodle';
      const sourceLink = document.querySelector('.nav-link[href="#moodle"]');
      if (sourceLink) {
        const label = sourceLink.querySelector('.nav-label'), short = sourceLink.querySelector('.nav-short');
        if (label) label.textContent = 'Мои задания';
        if (short) short.textContent = 'Задания';
        sourceLink.setAttribute('aria-label', 'Мои задания');
      }
    }
    let message;
    if (!state.connected) message = 'Moodle ещё не подключён. Обратитесь к команде CampusPulse, чтобы настроить доступ к своему аккаунту.';
    else if (!snapshot) message = state.error ?
      'Задания пока не получены. Проверьте доступ к Moodle и повторите обновление.' :
      'Задания ещё не загружены. Ожидаем первое обновление.';
    else {
      message = 'Последнее обновление: ' + moodleDate(state.last_success || snapshot.fetched_at) + '.';
      if (state.error) message += ' Не удалось обновить задания. Показаны ранее полученные данные.';
      else if (state.stale) message += ' Данные могли измениться. Нажмите «Обновить задания».';
      if (state.syncing) message += ' Обновление выполняется.';
    }
    if (state.error === 'auth_failed' || state.error === 'identity_mismatch')
      message += ' Доступ к аккаунту Moodle нужно подключить заново через команду CampusPulse.';
    if (calendarFeed) message += ' Календарь передаёт события и сроки; оценки и статусы сдачи недоступны.';
    $('moodle-status').textContent = message;
    if (!snapshot || !state.connected) return;
    if (snapshot.courses.length) {
      const heading = document.createElement('h3'), list = document.createElement('ul');
      heading.textContent = 'Баллы по предметам'; list.className = 'moodle-course-list';
      for (const course of snapshot.courses) {
        const item = document.createElement('li'), title = document.createElement('strong'), grade = document.createElement('span');
        title.textContent = course.title; grade.textContent = moodleGrade(course);
        item.append(title, grade); list.append(item);
      }
      courses.append(heading, list);
    }
    const heading = document.createElement('h3'); heading.textContent = calendarFeed ? 'Будущие сроки' : 'Задания'; tasks.append(heading);
    if (!snapshot.tasks.length) {
      const empty = document.createElement('p'); empty.textContent = 'В полученных данных Moodle заданий нет.';
      tasks.append(empty); return;
    }
    const list = document.createElement('ul'); list.className = 'moodle-task-list';
    const pastList = document.createElement('ul'), doneList = document.createElement('ul');
    pastList.className = doneList.className = 'moodle-task-list';
    for (const task of snapshot.tasks) {
      const group = !calendarFeed ? 'upcoming' : task.view_group ||
        (new Date(task.due_at).getTime() <= Date.now() ? 'past' : 'upcoming');
      const item = document.createElement('li'), subject = document.createElement('p'), title = document.createElement('h4'),
        description = document.createElement('p'), details = document.createElement('dl'),
        descriptionBox = document.createElement('details'), descriptionSummary = document.createElement('summary');
      subject.className = 'moodle-subject'; subject.textContent = task.course_title;
      title.textContent = task.title; description.className = 'moodle-description';
      description.textContent = task.description || 'Описание не предоставлено в Moodle.';
      descriptionBox.className = 'moodle-description-details';
      descriptionSummary.textContent = 'Описание задания';
      descriptionBox.append(descriptionSummary, description);
      const values = calendarFeed ? [
        ['Срок', moodleDate(task.due_at)],
        ['Статус', group === 'completed' ? 'Выполнено — ваша отметка в CampusPulse' :
          group === 'past' ? 'Срок прошёл; статус сдачи неизвестен' : 'Статус сдачи неизвестен'],
      ] : [
        ['Срок сдачи', task.due_at ? moodleDate(task.due_at) : 'Не указан'],
        ['Сдача', {todo: 'Не сдано', draft: 'Черновик', submitted: 'Сдано', graded: 'Оценено', unknown: 'Статус недоступен'}[task.submission_status] || 'Статус недоступен'],
        ['Баллы', moodleGrade(task)],
      ];
      for (const [label, value] of values) {
        const term = document.createElement('dt'), detail = document.createElement('dd');
        term.textContent = label; detail.textContent = value; details.append(term, detail);
      }
      item.append(subject, title, details, descriptionBox);
      const href = moodleLink(task.url);
      if (href) {
        const link = document.createElement('a'); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.textContent = 'Открыть задание в Moodle'; item.append(link);
      }
      if (calendarFeed) {
        const button = document.createElement('button'); button.type = 'button';
        button.dataset.taskId = task.id; button.dataset.done = String(group !== 'completed');
        button.textContent = group === 'completed' ? 'Вернуть в работу' : 'Отметить выполненным';
        item.append(button);
      }
      (group === 'completed' ? doneList : group === 'past' ? pastList : list).append(item);
    }
    if (calendarFeed && !list.children.length) {
      const empty = document.createElement('p'); empty.textContent = 'Будущих сроков в текущем экспорте нет.';
      tasks.append(empty);
    }
    tasks.append(list);
    for (const [label, archived] of [['Прошедшие сроки', pastList], ['Выполнено вами', doneList]]) {
      if (!archived.children.length) continue;
      const section = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = label + ' (' + archived.children.length + ')';
      section.append(summary, archived); tasks.append(section);
    }
  }
  async function loadMoodle() {
    if (!moodleEnabled || moodleBusy) return;
    const revision = ++moodleRevision;
    moodleBusy = true; $('moodle-refresh').disabled = true; $('moodle').setAttribute('aria-busy', 'true');
    try {
      const saved = await (await api('/api/moodle')).json();
      if (revision !== moodleRevision) return;
      showMoodle(saved);
      if (saved.connected) {
        const updated = await (await api('/api/moodle/sync', {})).json();
        if (revision === moodleRevision) showMoodle(updated);
      }
    } catch (error) {
      if (revision !== moodleRevision) return;
      if (error.status === 403) clearMoodle('Войдите в CampusPulse, чтобы увидеть свои задания и баллы.');
      else $('moodle-status').textContent = 'Не удалось обновить задания. Если показаны сохранённые данные, они могли измениться. Повторите обновление.';
    } finally {
      if (revision === moodleRevision) {
        moodleBusy = false; $('moodle-refresh').disabled = false; $('moodle').setAttribute('aria-busy', 'false');
      }
    }
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
  function resizeComposer() {
    const field = $('recovery-message');
    if (!field.getClientRects().length) return;
    field.style.height = '0px';
    const limit = parseFloat(getComputedStyle(field).maxHeight);
    const height = Math.min(field.scrollHeight, limit);
    field.style.height = height + 'px';
    field.style.overflowY = field.scrollHeight > Math.ceil(height) ? 'auto' : 'hidden';
  }
  function controls() {
    resizeComposer();
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
    const index = $('conversation-index');
    index.replaceChildren();
    for (const turn of chat.turns) {
      const user = document.createElement('article'), label = document.createElement('span'), text = document.createElement('p');
      user.className = 'chat-message chat-user'; user.id = 'chat-entry-' + turn.request_key; label.className = 'chat-author';
      const entry = document.createElement('button');
      entry.type = 'button'; entry.className = 'conversation-entry'; entry.dataset.chatEntry = user.id;
      entry.textContent = turn.message; entry.title = turn.message; index.append(entry);
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
    const calendarAdvice = document.body.dataset.chatMode === 'calendar_advice';
    $('chat-status').textContent = calendarAdvice ? (chatAvailable ?
      'Составьте план в чате по дедлайнам Moodle. Укажите свободные часы и время на задания.' :
      'Чат сейчас недоступен. Попробуйте обновить состояние.') : (chatAvailable ?
      'Чат с агентом · изменения календаря применяются через согласование плана.' :
      'Чат сейчас недоступен. Если проверка расписания доступна, начните с неё.');
    const providerErrors = {
      provider_request_rejected: 'Gemini отклонил запрос. Проверьте API-ключ и настройки модели.',
      provider_auth_failed: 'Gemini не принял API-ключ. Проверьте ключ при запуске сервера.',
      provider_access_denied: 'У ключа нет доступа к Gemini. Проверьте проект и ограничения ключа в AI Studio.',
      provider_model_unavailable: 'Выбранная модель Gemini недоступна. Проверьте модель в настройках сервера.',
      provider_quota: 'Gemini сообщил об ограничении квоты. Проверьте бесплатные лимиты проекта в AI Studio.',
      provider_unavailable: 'Gemini временно недоступен. Попробуйте отправить сообщение позже.',
      chat_runtime_failed: 'Ошибка подключения чата. Проверьте состояние локального сервера.'
    };
    if (providerErrors[chat.failure_code]) $('chat-status').textContent = providerErrors[chat.failure_code];
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
      const conversation = $('conversation-scroll');
      conversation.scrollTo({top: conversation.scrollHeight, behavior: 'instant'});
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
      feedback('Состояние обновлено. Изменения календаря не запускались.');
      if (activeRun) {
        stored('run', activeRun);
        const locationQuery = new URLSearchParams({run_id: activeRun});
        history.replaceState(null, '', '/?' + locationQuery + location.hash);
      }
    } catch (error) {
      if (revision === generation) {
        scope = null; activeRun = null; chatAvailable = false; chatWorking = false;
        $('chat-history').replaceChildren(); $('chat-history').hidden = true; $('conversation-index').replaceChildren();
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
      if (activeRun) history.replaceState(null, '', '/?' + new URLSearchParams({run_id: activeRun}) + location.hash);
      cursor = 0; $('trace-records').replaceChildren(); $('trace-more').hidden = true;
      await refresh();

      focusPanel();
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
  window.addEventListener('resize', resizeComposer);
  $('recovery-message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!$('chat-send').disabled) $('chat-form').requestSubmit();
    }
  });
  window.addEventListener('hashchange', updateNavigation);
  window.addEventListener('popstate', updateNavigation);
  $('mobile-nav-toggle')?.addEventListener('click', () => {
    const open = document.body.dataset.navOpen !== 'true';
    setNavigationOpen(open);
    if (open) requestAnimationFrame(() => {
      if (document.body.dataset.navOpen === 'true') $('sidebar')?.querySelector('.nav-link')?.focus();
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.dataset.navOpen === 'true') {
      setNavigationOpen(false);
      $('mobile-nav-toggle')?.focus();
    }
  });
  document.addEventListener('click', event => {
    const prompt = event.target.closest('[data-prompt]');
    if (prompt && !$('recovery-message').readOnly) {
      setNavigationOpen(false);
      prompt.closest('details')?.removeAttribute('open');
      $('recovery-message').value = prompt.dataset.prompt;
      $('recovery-message').dispatchEvent(new Event('input', {bubbles: true}));
      $('recovery-message').focus({preventScroll: true});
    }
    const entry = event.target.closest('[data-chat-entry]');
    if (entry) {
      location.hash = 'workspace'; updateNavigation();
      requestAnimationFrame(() => $(entry.dataset.chatEntry)?.scrollIntoView({block: 'start'}));
    }
    const link = event.target.closest('.nav-link');
    if (link) {
      const wasOpen = document.body.dataset.navOpen === 'true';
      setNavigationOpen(false);
      if (wasOpen) requestAnimationFrame(() => {
        const title = $('view-title');
        if (title) { title.tabIndex = -1; title.focus({preventScroll: true}); }
      });
    }
    if (document.body.dataset.navOpen === 'true' && !event.target.closest('#sidebar, #mobile-nav-toggle'))
      setNavigationOpen(false);
  });
  updateNavigation();
  $('focus-composer')?.addEventListener('click', () => {
    location.hash = 'workspace'; updateNavigation();
    requestAnimationFrame(() => $('recovery-message').focus({preventScroll: true}));
  });
  $('reload').addEventListener('click', () => {
    loadMoodle(); refresh().catch(error => feedback(error.message, true));
  });
  if (moodleEnabled) {
    $('moodle-refresh').addEventListener('click', loadMoodle);
    $('moodle-tasks').addEventListener('click', async event => {
      const button = event.target.closest('button[data-task-id]');
      if (!button || button.disabled) return;
      button.disabled = true;
      try {
        const state = await (await api('/api/moodle/task-status', {
          task_id: button.dataset.taskId, done: button.dataset.done === 'true'
        })).json();
        showMoodle(state);
      } catch {
        button.disabled = false;
        $('moodle-status').textContent = 'Не удалось сохранить вашу отметку. Повторите действие.';
      }
    });
  }
  $('panel').addEventListener('click', event => {
    const button = event.target.closest('button[data-action]');
    if (button && !button.disabled) act(button.dataset.action);
  });
  $('trace').addEventListener('click', () => trace(true));
  $('trace-more').addEventListener('click', () => trace(false));
  $('signout').addEventListener('click', async () => {
    if (busy || chatBusy) return;
    try { await api('/auth/signout', {}); ++generation; scope = null; activeRun = null;
      ++moodleRevision; moodleBusy = false;
      clearMoodle('Войдите в CampusPulse, чтобы увидеть свои задания и баллы.');
      if (moodleEnabled) { $('moodle-refresh').disabled = true; $('moodle').setAttribute('aria-busy', 'false'); }
      $('panel').replaceChildren(); $('trace-records').replaceChildren(); $('signout').hidden = true;
      $('chat-history').replaceChildren(); $('chat-history').hidden = true; $('conversation-index').replaceChildren();
      $('workspace').classList.remove('has-chat'); $('recovery-message').value = '';
      chatAvailable = false; chatWorking = false; pendingText = null;
      $('chat-status').textContent = 'Войдите, чтобы открыть чат.';
      $('trace-health').textContent = 'Войдите, чтобы загрузить журнал.';
      history.replaceState(null, '', '/'); updateNavigation();
      feedback('Вы вышли. Для просмотра плана требуется вход.'); controls();
      if (document.body.dataset.signinUrl === '/login') window.location.assign('/login');
    } catch (error) { feedback(error.message, true); }
  });

  loadMoodle();
  refresh().catch(error => feedback(error.message, true));
})();
"""
