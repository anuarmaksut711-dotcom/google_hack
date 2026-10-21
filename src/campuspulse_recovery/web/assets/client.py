"""Small browser adapter; GET refreshes never consent or dispatch execution."""
from .workspace import SCRIPT as WORKSPACE_SCRIPT
from .answer_reveal import SCRIPT as ANSWER_REVEAL_SCRIPT
from .study_trace import SCRIPT as STUDY_TRACE_SCRIPT
SCRIPT = r"""'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const answerReveal = new window.CampusPulseAnswerReveal();
  let scope = null, activeRun = null, busy = false, cursor = 0, traceBusy = false, generation = 0;
  let chatBusy = false, chatAvailable = false, chatWorking = false, chatLimitReached = false, pendingText = null;
  let renderedChat = null, chatPoller = null;
  let speechAvailable = false, voiceState = 'idle', voiceSession = null;
  const voiceSupported = !!(window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  const voiceMime = voiceSupported ? ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus']
    .find(type => MediaRecorder.isTypeSupported(type)) : null;
  const currentVoice = session => voiceSession === session && session.revision === generation && !!scope;
  function releaseMicrophone(session) {
    if (!session) return;
    clearTimeout(session.timer);
    cancelAnimationFrame(session.frame);
    const audio = session.audio;
    session.audio = null;
    if (audio) audio.close().catch(() => {});
    session.stream?.getTracks().forEach(track => track.stop());
    session.stream = null;
    if (voiceSession === session) $('voice-level-wrap').hidden = true;
  }
  function finishVoice(session) {
    releaseMicrophone(session);
    if (voiceSession !== session) return;
    voiceSession = null; voiceState = 'idle'; controls();
  }
  function voiceControls() {
    const idle = voiceState === 'idle';
    const button = $('voice-toggle');
    button.disabled = !voiceMime || !speechAvailable || !scope || busy || chatBusy || chatWorking || chatLimitReached ||
      (!idle && voiceState !== 'recording');
    const label = voiceState === 'recording' ? 'Остановить запись' :
      voiceState === 'transcribing' ? 'Распознаём…' : voiceState === 'requesting' ? 'Открываем микрофон…' : 'Голосовой ввод';
    $('voice-toggle-label').textContent = label;
    button.setAttribute('aria-label', label);
    button.setAttribute('title', label);
    button.setAttribute('aria-pressed', String(voiceState === 'recording'));
    button.dataset.state = voiceState;
    $('chat-form').dataset.voiceState = voiceState;
  }
  $('voice-stop').addEventListener('click', () => $('voice-toggle').click());
  $('voice-cancel').addEventListener('click', () => {
    if (voiceState !== 'recording' || !voiceSession) return;
    const session = voiceSession;
    session.cancelled = true;
    if (session.recorder?.state === 'recording') session.recorder.stop();
    finishVoice(session);
    $('voice-status').textContent = '';
    $('recovery-message').focus({preventScroll: true});
  });
  $('voice-toggle').addEventListener('click', async () => {
    if (voiceState === 'recording') {
      const session = voiceSession;
      voiceState = 'stopping'; controls(); session.recorder.stop(); releaseMicrophone(session); return;
    }
    if ($('voice-toggle').disabled) return;
    const session = {revision: generation, stream: null, audio: null, recorder: null, timer: null, frame: null};
    voiceSession = session;
    let chunks = [], size = 0, tooLarge = false, signalFrames = 0, startedAt = 0;
    voiceState = 'requesting'; controls();
    try {
      session.stream = await navigator.mediaDevices.getUserMedia({audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
      if (!currentVoice(session)) { finishVoice(session); return; }
      const audio = new (window.AudioContext || window.webkitAudioContext)();
      session.audio = audio;
      await audio.resume();
      if (!currentVoice(session)) { finishVoice(session); return; }
      const analyser = audio.createAnalyser();
      analyser.fftSize = 2048;
      audio.createMediaStreamSource(session.stream).connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      const dots = Array.from($('voice-waveform').children);
      const levels = Array(dots.length).fill(0);
      let lastWaveAt = 0;
      dots.forEach(dot => { dot.style.height = '4px'; dot.style.opacity = '0.25'; });
      const measure = () => {
        if (!currentVoice(session) || voiceState !== 'recording') return;
        analyser.getFloatTimeDomainData(samples);
        let power = 0;
        for (const sample of samples) power += sample * sample;
        const rms = Math.sqrt(power / samples.length);
        if (rms > 0.003) ++signalFrames;
        const now = performance.now();
        if (now - lastWaveAt >= 65) {
          levels.shift(); levels.push(Math.min(1, rms * 12));
          const visibleDots = Math.max(1, Math.floor($('voice-waveform').clientWidth / 8));
          dots.forEach((dot, index) => {
            dot.hidden = index < dots.length - visibleDots;
            dot.style.height = (4 + levels[index] * 24) + 'px';
            dot.style.opacity = String(0.25 + levels[index] * 0.6);
          });
          lastWaveAt = now;
        }
        session.frame = requestAnimationFrame(measure);
      };
      const recorder = new MediaRecorder(session.stream, {mimeType: voiceMime});
      session.recorder = recorder;
      recorder.ondataavailable = event => {
        if (!currentVoice(session)) return;
        size += event.data.size;
        if (size > 4 * 1024 * 1024) {
          tooLarge = true;
          if (recorder.state !== 'inactive') recorder.stop();
          releaseMicrophone(session);
        } else if (!tooLarge) chunks.push(event.data);
      };
      recorder.onerror = () => {
        tooLarge = true; chunks = [];
        if (currentVoice(session)) $('voice-status').textContent = 'Запись прервалась. Попробуйте снова.';
        finishVoice(session);
      };
      recorder.onstop = async () => {
        releaseMicrophone(session);
        if (!currentVoice(session) || session.cancelled || tooLarge) {
          if (currentVoice(session) && tooLarge) $('voice-status').textContent = 'Не удалось записать аудио. Попробуйте более короткую запись.';
          chunks = []; finishVoice(session); return;
        }
        if (performance.now() - startedAt < 1200 || signalFrames < 5) {
          chunks = []; finishVoice(session);
          $('voice-status').textContent = 'Не удалось записать достаточно звука. Проверьте микрофон, дождитесь начала записи и произнесите фразу целиком.';
          return;
        }
        voiceState = 'transcribing'; controls();
        $('voice-status').textContent = 'Распознаём речь…';
        try {
          const token = await csrf();
          if (!currentVoice(session)) return;
          const response = await fetch('/api/chat/transcribe', {method: 'POST', credentials: 'same-origin',
            cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(60000),
            headers: {'Content-Type': voiceMime, 'X-CSRF-Token': token},
            body: new Blob(chunks, {type: voiceMime})});
          if (!response.ok) {
            const error = new Error('transcription');
            error.status = response.status;
            throw error;
          }
          const {text} = await response.json();
          if (!currentVoice(session)) return;
          const input = $('recovery-message');
          const draft = input.value + (input.value && text ? '\n' : '') + text;
          if (draft.length > input.maxLength) {
            $('voice-status').textContent = 'Текст не поместился в сообщение. Сократите черновик и повторите запись.';
          } else {
            input.value = draft;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            $('voice-status').textContent = text ? '' :
              'Речь не распознана. Попробуйте говорить ближе к микрофону.';
          }
        } catch (error) {
          if (currentVoice(session)) $('voice-status').textContent = error.status === 403 ?
            'Сессия входа недействительна. Войдите заново, затем повторите запись.' :
            'Не удалось распознать речь. Попробуйте снова или введите сообщение вручную.';
        } finally {
          const focus = currentVoice(session);
          chunks = []; finishVoice(session);
          if (focus) $('recovery-message').focus({preventScroll: true});
        }
      };
      recorder.start(1000); startedAt = performance.now(); voiceState = 'recording'; controls();
      $('voice-level-wrap').hidden = false; measure();
      $('voice-status').textContent = 'Идёт запись. Нажмите «Остановить запись», когда закончите (до 1 минуты).';
      session.timer = setTimeout(() => {
        if (!currentVoice(session)) return;
        if (recorder.state === 'recording') { voiceState = 'stopping'; controls(); recorder.stop(); }
        releaseMicrophone(session);
      }, 60000);
    } catch (error) {
      if (currentVoice(session)) $('voice-status').textContent = error.name === 'NotAllowedError' ?
        'Разрешите доступ к микрофону в настройках браузера и попробуйте снова.' :
        'Микрофон недоступен. Проверьте подключение и попробуйте снова.';
      finishVoice(session);
    }
  });
  window.addEventListener('pagehide', () => {
    saveDraft();
    ++generation;
    const session = voiceSession;
    if (session?.recorder?.state === 'recording') session.recorder.stop();
    finishVoice(session);
  });
  if (!voiceMime) $('voice-status').textContent = 'Голосовой ввод недоступен в этом браузере. Используйте современный браузер и HTTPS.';
  const moodleEnabled = document.body.dataset.moodleEnabled === 'true';
  let moodleRevision = 0, moodleBusy = false;
  const draftChat = id => /^draft(?:-[a-f0-9]{32})?$/.test(id);
  const chatHash = id => id === 'default' ? '#workspace' : draftChat(id) ? '#new' + (id === 'draft' ? '' : '/' + id.slice(6)) : '#chat/' + id;
  const hashChat = hash => /^#chat\/([a-f0-9]{32})$/.exec(hash)?.[1] ||
    (/^#new(?:\/([a-f0-9]{32}))?$/.test(hash) ? 'draft' + (hash.slice(5) ? '-' + hash.slice(5) : '') : 'default');
  let activeChat = hashChat(location.hash), chatGeneration = 0;
  const chatDrafts = new Map();
  const chats = new window.CampusPulseChats({api, selected: () => activeChat, session: () => scope,
    navigate: id => { location.hash = chatHash(id); if (id === activeChat) $('recovery-message').focus({preventScroll: true}); },
    removed: removedWorkspaceItem,
    changed: () => updateNavigation({preserveNavigation: true}), blocked: () => !scope || busy || chatBusy || voiceState !== 'idle'});
  $('chat-limit-new').addEventListener('click', () => $('chat-new').click());
  const pendingChatKey = () => activeChat === 'default' ? 'pending-chat' : 'pending-chat:' + activeChat;
  const draftPrefix = () => 'campuspulse-draft-v1:' + scope + ':';
  function saveDraft() {
    if (!scope) return;
    const draft = {text: $('recovery-message').value, pendingText};
    chatDrafts.set(activeChat, draft);
    try {
      if (draft.text || draft.pendingText !== null) sessionStorage.setItem(draftPrefix() + activeChat, JSON.stringify(draft));
      else sessionStorage.removeItem(draftPrefix() + activeChat);
    } catch { /* Keep the in-memory draft when browser storage is unavailable. */ }
  }
  function restoreDraft(id) {
    let draft = chatDrafts.get(id);
    if (!draft && scope) {
      try {
        const saved = JSON.parse(sessionStorage.getItem(draftPrefix() + id));
        if (saved && typeof saved.text === 'string' && saved.text.length <= 4000 &&
            (saved.pendingText === null || typeof saved.pendingText === 'string' && saved.pendingText.length <= 4000)) draft = saved;
      } catch { /* An invalid saved draft must not prevent opening a chat. */ }
    }
    pendingText = draft?.pendingText ?? null;
    $('recovery-message').value = draft?.text || '';
  }
  function clearDrafts() {
    chatDrafts.clear();
    if (!scope) return;
    try {
      const prefix = draftPrefix();
      for (const key of Object.keys(sessionStorage)) if (key.startsWith(prefix)) sessionStorage.removeItem(key);
    } catch { /* Storage can be disabled by browser policy. */ }
  }
  function removedWorkspaceItem(change) {
    if (change.action !== 'delete_chat') return;
    const id = change.item_id;
    chatDrafts.delete(id);
    try { sessionStorage.removeItem(draftPrefix() + id); } catch { /* Browser storage is optional. */ }
    stored(id === 'default' ? 'pending-chat' : 'pending-chat:' + id, null);
    if (id !== activeChat) return;
    activeChat = 'draft'; ++chatGeneration;
    restoreDraft(activeChat);
    history.replaceState(null, '', location.pathname + location.search + chatHash(activeChat));
    $('chat-history').replaceChildren(); $('chat-history').hidden = true;
    $('conversation-index')?.replaceChildren(); $('workspace').classList.remove('has-chat');
    loadChat();
  }
  function selectChat(id) {
    if (id === activeChat) { chats.selection(); return; }
    if (busy || chatBusy || voiceState !== 'idle') {
      history.replaceState(null, '', location.pathname + location.search + chatHash(activeChat));
      const status = voiceState !== 'idle' ? $('voice-status') : $('chat-status');
      status.textContent = voiceState !== 'idle' ? 'Завершите голосовой ввод, затем откройте другой чат.' : 'Дождитесь завершения текущего запроса, затем откройте другой чат.';
      return;
    }
    answerReveal.finish();
    saveDraft();
    activeChat = id; ++chatGeneration;
    $('conversation-index')?.replaceChildren();
    restoreDraft(id);
    $('chat-history').replaceChildren(); $('chat-history').hidden = true;
    $('workspace').classList.remove('has-chat');
    chatAvailable = false; chatWorking = false; chatLimitReached = false; chats.selection(); controls();
    if (scope) loadChat().then(() => $('recovery-message').focus({preventScroll: true}));
  }
  const views = {
    assistant: ['Ассистент', 'Ваши задачи и планы'],
    tasks: ['Мои задания', 'Задания и ближайшие сроки'],
    calendar: ['Google Calendar', 'Ваш календарь на ближайшие три дня'],
    plan: ['Мой план', 'Изменения календаря и ваше подтверждение'],
    journal: ['Журнал', 'Действия агента и результаты запуска']
  };
  function setNavigationOpen(open) {
    document.body.dataset.navOpen = String(open);
    $('mobile-nav-toggle')?.setAttribute('aria-expanded', String(open));
  }
  function updateNavigation(options = {}) {
    const selector = new URLSearchParams(location.search);
    const selected = location.hash || (selector.has('plan_id') && selector.has('version') ? '#panel' : '#workspace');
    const chatMatch = /^#chat\/([a-f0-9]{32})$/.exec(selected);
    if (chatMatch || selected === '#workspace' || /^#new(?:\/[a-f0-9]{32})?$/.test(selected)) selectChat(hashChat(selected));
    let view = {'#workspace': 'assistant', '#moodle': 'tasks', '#sources': 'plan',
      '#diff-heading': 'plan', '#panel': 'plan', '#review-heading': 'plan', '#journal-heading': 'journal',
      '#calendar': document.getElementById('google-calendar') ? 'calendar' : 'assistant'}[selected] || 'assistant';
    if (view === 'tasks' && !moodleEnabled) view = 'plan';
    document.body.dataset.view = view;
    if ($('view-title')) $('view-title').textContent = view === 'assistant' ? chats.title() : views[view][0];
    if ($('view-subtitle')) $('view-subtitle').textContent = views[view][1];
    const chatTarget = chatHash(activeChat);
    document.querySelectorAll('.nav-link[data-view=assistant], .rail-link[data-view=assistant], .brand, .rail-brand').forEach(link => { link.href = chatTarget; });
    document.title = 'Google AI · ' + (view === 'assistant' ? chats.title() : views[view][0]);
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
    if (!options.preserveNavigation) setNavigationOpen(false);
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
  const csrf = async () => {
    const sessionId = scope, revision = generation;
    try {
      for (let attempt = 0; attempt < 2; ++attempt) {
        const response = await fetch('/auth/session', {credentials: 'same-origin', cache: 'no-store',
          redirect: 'error', signal: AbortSignal.timeout(15000), headers: {'X-CSRF-Bootstrap': '1'}});
        if (response.status === 409 && attempt === 0) continue;
        if (!response.ok) {
          const error = new Error();
          error.status = response.status;
          throw error;
        }
        const session = await response.json();
        if (!sessionId || scope !== sessionId || session.session_id !== sessionId) {
          const error = new Error();
          error.status = 403;
          throw error;
        }
        if (generation !== revision) throw new Error();
        if (typeof session.csrf_token !== 'string' || !/^[A-Za-z0-9_-]{20,256}$/.test(session.csrf_token))
          throw new Error();
        return session.csrf_token;
      }
    } catch (cause) {
      const error = new Error(cause.status === 403 ? 'Сессия входа недействительна. Войдите заново.' :
        'Не удалось проверить вход. Повторите попытку.');
      error.status = cause.status;
      error.code = cause.status === 403 ? 'session_required' : 'request_not_sent';
      throw error;
    }
  };
  async function api(path, payload) {
    const options = {credentials: 'same-origin', cache: 'no-store', redirect: 'error'};
    if (path.startsWith('/api/moodle')) options.signal = AbortSignal.timeout(110000);
    if (path.startsWith('/api/chat?')) options.signal = AbortSignal.timeout(15000);
    if (payload !== undefined) Object.assign(options, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': await csrf()}, body: JSON.stringify(payload)});
    const response = await fetch(path, options);
    if (!response.ok) {
      const messages = {403: 'Сессия недействительна или действие отклонено. Войдите через настроенный вход.',
        409: 'План или запрос изменился. Обновите состояние и изучите точную версию.',
        503: 'Результат запроса не подтверждён. Обновите сохранённое состояние.'};
      if (path.startsWith('/api/chat')) {
        messages[409] = 'Запрос чата уже выполняется, был изменён или достигнут лимит диалога. Обновите историю.';
        messages[503] = 'Агент недоступен. Проверьте настройку Vertex AI на сервере.';
      }
      const error = new Error(messages[response.status] || 'Не удалось получить результат. Обновите состояние.');
      error.status = response.status;
      if (path === '/api/chat' && response.status === 409 ||
          path === '/api/chat/workspace' && response.status === 403) {
        const detail = await response.json().catch(() => ({}));
        if (detail.error === 'chat_history_limit' || detail.error === 'workspace_item_deleted') error.code = detail.error;
      }
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
    if (!state.connected) message = 'Moodle ещё не подключён. Обратитесь к команде Google AI, чтобы настроить доступ к своему аккаунту.';
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
      message += ' Доступ к аккаунту Moodle нужно подключить заново через команду Google AI.';
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
        ['Статус', group === 'completed' ? 'Выполнено — ваша отметка в Google AI' :
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
      if (error.status === 403) clearMoodle('Войдите в Google AI, чтобы увидеть свои задания и баллы.');
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
    const storage = name.startsWith('pending-chat:draft') ? sessionStorage : localStorage;
    if (value !== undefined) storage.setItem(key, JSON.stringify(value));
    const raw = storage.getItem(key);
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
    voiceControls();
    resizeComposer();
    document.querySelectorAll('[data-action]').forEach(button => { button.disabled = busy || chatBusy; });
    $('start').disabled = voiceState !== 'idle' || busy || chatBusy || !scope || $('panel').querySelector('[data-runtime="true"]') === null;
    $('reload').disabled = busy || chatBusy || voiceState !== 'idle';
    $('signout').disabled = busy || chatBusy || voiceState !== 'idle';
    $('trace').disabled = !activeRun || traceBusy;
    $('chat-send').disabled = voiceState !== 'idle' || busy || chatBusy || chatWorking || chatLimitReached || !scope || !chatAvailable || !$('recovery-message').value.trim();
    $('recovery-message').readOnly = chatBusy;
    $('chat-form').setAttribute('aria-busy', String(chatBusy));
    $('chat-limit').hidden = !chatLimitReached;
    $('chat-limit-new').disabled = !scope || busy || chatBusy || !chats.data || chats.mutating;
    chats.controls();
  }
  // Small, deliberately limited Markdown subset. Model text never becomes HTML.
  function appendChatInline(node, value) {
    const tokens = /(\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`|\*([^*\n]+)\*)/g;
    let offset = 0;
    for (const match of value.matchAll(tokens)) {
      node.append(document.createTextNode(value.slice(offset, match.index)));
      const element = document.createElement(match[2] || match[3] ? 'strong' : match[4] ? 'code' : 'em');
      element.textContent = match[2] || match[3] || match[4] || match[5];
      node.append(element);
      offset = match.index + match[0].length;
    }
    node.append(document.createTextNode(value.slice(offset)));
  }
  function renderChatAnswer(node, value) {
    node.replaceChildren();
    let paragraph = null, list = null, code = null;
    for (const raw of String(value || '').replace(/\r\n?/g, '\n').split('\n')) {
      const line = raw.trim();
      if (line.startsWith('```')) {
        paragraph = list = null;
        if (code) code = null;
        else {
          const pre = document.createElement('pre');
          code = document.createElement('code'); pre.append(code); node.append(pre);
        }
        continue;
      }
      if (code) { code.append(document.createTextNode(raw + '\n')); continue; }
      if (!line) { paragraph = list = null; continue; }
      const heading = line.match(/^#{1,6}\s+(.+?)(?:\s+#+)?$/);
      const item = line.match(/^(?:[-*+]|\d+[.)])\s+(.+)$/);
      if (heading) {
        paragraph = list = null;
        const title = document.createElement('p'); title.className = 'chat-heading';
        appendChatInline(title, heading[1]); node.append(title);
      } else if (item) {
        paragraph = null;
        const type = /^\d/.test(line) ? 'ol' : 'ul';
        if (!list || list.localName !== type) {
          list = document.createElement(type);
          if (type === 'ol') list.start = parseInt(line, 10);
          node.append(list);
        }
        const entry = document.createElement('li'); appendChatInline(entry, item[1]); list.append(entry);
      } else {
        list = null;
        if (!paragraph) { paragraph = document.createElement('p'); node.append(paragraph); }
        else paragraph.append(document.createTextNode('\n'));
        appendChatInline(paragraph, line.replace(/^>\s?/, ''));
      }
    }
  }
  function showChat(chat, {animateRequestKey = null} = {}) {
    answerReveal.finish();
    renderedChat = chat;
    let revealAnswer = null;
    chatLimitReached = chat.limit_reached === true;
    speechAvailable = chat.speech_available === true;
    chatAvailable = chat.available;
    chatWorking = chat.turns.some(turn => turn.status === 'working');
    const log = $('chat-history');
    const expanded = new Map([...log.querySelectorAll('.chat-assistant')].map(node =>
      [node.dataset.requestKey, node.querySelector('.agent-work details')?.open]));
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
      const reply = document.createElement('article'), author = document.createElement('span'), answer = document.createElement('div');
      answer.className = 'chat-answer';
      reply.className = 'chat-message chat-assistant'; author.className = 'chat-author'; author.textContent = 'Google AI';
      reply.dataset.requestKey = turn.request_key;
      if (turn.status === 'completed') {
        renderChatAnswer(answer, turn.response);
        if (turn.request_key === animateRequestKey) revealAnswer = answer;
      }
      else {
        reply.classList.add('chat-system');
        author.textContent = 'Статус запроса';
        answer.textContent = {working: '',
          failed: 'Агент не смог ответить. Проверьте подключение и отправьте сообщение снова.',
          unknown: 'Ответ не получен. Этот запрос не будет отправлен автоматически.'}[turn.status];
      }
      const work = window.CampusPulseStudyTrace.create(turn);
      if (expanded.has(turn.request_key) && expanded.get(turn.request_key) !== undefined)
        work.querySelector('details').open = expanded.get(turn.request_key);
      reply.append(author, work, answer);
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
    if (revealAnswer) answerReveal.start(revealAnswer, $('conversation-scroll'), log);
  }
  function watchChatProgress(requestKey) {
    chatPoller?.stop();
    const revision = generation, session = scope, chatId = activeChat, selection = chatGeneration;
    const valid = () => revision === generation && session === scope && chatId === activeChat && selection === chatGeneration;
    chatPoller = new window.CampusPulseStudyTrace.Poller({
      read: async () => (await api('/api/chat?chat_id=' + chatId)).json(), valid,
      update: (chat, turn) => {
        const log = $('chat-history'), conversation = $('conversation-scroll');
        const nearEnd = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 90;
        const reply = [...log.querySelectorAll('.chat-assistant')].find(node => node.dataset.requestKey === requestKey);
        if (turn.status === 'working' && reply) {
          window.CampusPulseStudyTrace.update(reply.querySelector('.agent-work'), turn);
          renderedChat = chat; chatWorking = true;
        } else {
          const top = conversation.scrollTop;
          showChat(chat, {animateRequestKey: turn.status === 'completed' ? requestKey : null});
          if (!nearEnd) conversation.scrollTop = top;
          if (!chatBusy && stored(pendingChatKey()) === requestKey) {
            if (turn.status === 'completed' && $('recovery-message').value.trim() === turn.message.trim())
              $('recovery-message').value = '';
            stored(pendingChatKey(), null); pendingText = null; saveDraft(); controls();
          }
        }
        if (nearEnd) conversation.scrollTo({top: conversation.scrollHeight, behavior: 'instant'});
      },
      failed: error => {
        if (error.status !== 403) $('chat-status').textContent =
          'Не удалось обновить ход работы. Ответ ещё ожидается.';
      }
    });
    chatPoller.start(requestKey);
  }
  async function loadChat() {
    // A bookmarked chat can have been deleted in another tab. Preserve any
    // unsent text as an independent local draft; never recreate the old chat.
    if (!draftChat(activeChat) && Array.isArray(chats.data?.chats) &&
        !chats.data.chats.some(chat => chat.chat_id === activeChat)) {
      const previous = activeChat;
      saveDraft();
      activeChat = 'draft-' + (previous === 'default' ? crypto.randomUUID().replaceAll('-', '') : previous);
      ++chatGeneration; pendingText = null; saveDraft();
      chatDrafts.delete(previous);
      try { sessionStorage.removeItem(draftPrefix() + previous); } catch { /* Browser storage is optional. */ }
      stored(previous === 'default' ? 'pending-chat' : 'pending-chat:' + previous, null);
      history.replaceState(null, '', location.pathname + location.search + chatHash(activeChat));
      $('chat-history').replaceChildren(); $('chat-history').hidden = true;
      $('conversation-index')?.replaceChildren(); $('workspace').classList.remove('has-chat');
      updateNavigation({preserveNavigation: true});
    }
    const revision = generation, session = scope, chatId = activeChat, selection = chatGeneration;
    if (draftChat(chatId)) {
      showChat({available: chats.data?.chat_available === true,
        speech_available: chats.data?.speech_available === true, turns: [], limit_reached: false});
      return;
    }
    const pending = stored(pendingChatKey());
    try {
      const chat = await (await api('/api/chat?chat_id=' + chatId)).json();
      if (revision !== generation || session !== scope || selection !== chatGeneration) return;
      showChat(chat, {animateRequestKey: pending});
      const working = chat.turns.find(turn => turn.status === 'working');
      if (working) watchChatProgress(working.request_key);
      else chatPoller?.stop();
      const accepted = pending && chat.turns.find(turn => turn.request_key === pending);
      if (!chatBusy && pending === stored(pendingChatKey()) && accepted && accepted.status !== 'working') {
        if (accepted.status === 'completed' && $('recovery-message').value.trim() === accepted.message.trim())
          $('recovery-message').value = '';
        stored(pendingChatKey(), null); pendingText = null;
        saveDraft();
      }
    } catch (error) {
      if (revision !== generation || session !== scope || selection !== chatGeneration) return;
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
    const hadPending = !!stored(pendingChatKey());
    chatBusy = true; controls();
    let submitted = null;
    $('chat-status').textContent = 'Агент готовит ответ…';
    try {
      const request_key = keyFor(pendingChatKey()); pendingText = message;
      saveDraft();
      if (draftChat(activeChat)) {
        const draftId = activeChat;
        const project = draftId === 'draft' ? null : draftId.slice(6);
        const creationKey = pendingChatKey() + ':creation', previousCreation = stored(creationKey);
        const folderId = previousCreation?.requestKey === request_key ? previousCreation.folderId :
          Array.isArray(chats.data?.folders) && !chats.data.folders.some(folder => folder.folder_id === project) ? null : project;
        // A lost response may already have created this chat. Its retry must
        // use the same project even if that project has since been deleted.
        stored(creationKey, {requestKey: request_key, folderId});
        const chatId = await chats.materialize(folderId, request_key);
        stored(creationKey, null);
        stored(pendingChatKey(), null);
        activeChat = chatId; ++chatGeneration;
        stored(pendingChatKey(), request_key); saveDraft();
        chatDrafts.delete(draftId);
        try { sessionStorage.removeItem(draftPrefix() + draftId); } catch { /* Browser storage is optional. */ }
        history.replaceState(null, '', location.pathname + location.search + chatHash(chatId));
        chats.selection(); updateNavigation({preserveNavigation: true});
      }
      const turn = {request_key, message, status: 'working', created_at: new Date().toISOString(), response: null, duration_ms: null};
      submitted = {chatId: activeChat, session: scope, selection: chatGeneration, requestKey: request_key,
        previous: renderedChat};
      showChat({...renderedChat, available: true, turns: [...(renderedChat?.turns || []).filter(item => item.request_key !== request_key), turn]});
      const conversation = $('conversation-scroll');
      conversation.scrollTo({top: conversation.scrollHeight, behavior: 'instant'});
      const response = api('/api/chat', {request_key, message, chat_id: activeChat});
      watchChatProgress(request_key);
      const chat = await (await response).json();
      if (submitted.chatId !== activeChat || submitted.session !== scope || submitted.selection !== chatGeneration) return;
      const settled = chat.turns.find(item => item.request_key === request_key);
      if (settled?.status === 'working') {
        showChat(chat); watchChatProgress(request_key); return;
      }
      chatPoller?.stop();
      stored(pendingChatKey(), null); pendingText = null;
      $('recovery-message').value = '';
      saveDraft();
      const displayed = renderedChat?.turns?.find(item => item.request_key === request_key);
      if (displayed?.status !== 'completed' || displayed.response !== settled?.response)
        showChat(chat, {animateRequestKey: request_key});
      conversation.scrollTo({top: conversation.scrollHeight, behavior: 'instant'});
      await chats.load();
      updateNavigation({preserveNavigation: true});
    } catch (error) {
      if (submitted && (submitted.chatId !== activeChat || submitted.session !== scope || submitted.selection !== chatGeneration)) return;
      if (draftChat(activeChat)) { pendingText = null; saveDraft(); }
      if (draftChat(activeChat) && error.code === 'workspace_item_deleted') {
        stored(pendingChatKey() + ':creation', null); stored(pendingChatKey(), null);
        $('chat-status').textContent = 'Этот чат уже удалён. Текст сохранён. Нажмите «Отправить», чтобы начать новый чат.';
        return;
      }
      if (!hadPending && (error.code === 'session_required' || error.code === 'request_not_sent')) {
        chatPoller?.stop();
        if (submitted?.previous) showChat(submitted.previous);
        pendingText = null; stored(pendingChatKey(), null); saveDraft();
      }
      if (error.status === 403) {
        chatPoller?.stop();
        if (submitted && renderedChat && (hadPending || !['session_required', 'request_not_sent'].includes(error.code))) showChat({...renderedChat,
          turns: renderedChat.turns.map(turn => turn.request_key === submitted.requestKey ? {...turn, status: 'unknown'} : turn)});
        $('chat-status').textContent = 'Сессия входа недействительна. Войдите заново — текст сохранён в поле.';
        return;
      }
      if (error.code === 'chat_history_limit') {
        chatPoller?.stop();
        if (submitted?.previous) showChat({...submitted.previous, limit_reached: true});
        chatLimitReached = true; pendingText = null; stored(pendingChatKey(), null);
        saveDraft();
        $('chat-status').textContent = 'Этот диалог заполнен. Начните новый диалог — история сохранена.';
        return;
      }
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
      if (scope !== session.session_id) { chats.reset(); clearDrafts(); pendingText = null; }
      scope = session.session_id;
      restoreDraft(activeChat);
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
      await chats.load();
      if (revision !== generation) return;
      updateNavigation();
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
        if (error.status === 403) clearDrafts(); else chatDrafts.clear();
        scope = null; activeRun = null; chatAvailable = false; chatWorking = false; chatLimitReached = false;
        chats.reset(); ++chatGeneration;
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
  $('recovery-message').addEventListener('input', () => { saveDraft(); controls(); });
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
    if (document.querySelector('dialog[open], [popover]:popover-open')) return;
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
      location.hash = chatHash(activeChat); updateNavigation();
      requestAnimationFrame(() => $(entry.dataset.chatEntry)?.scrollIntoView({block: 'start'}));
    }
    const link = event.target.closest('.nav-link, .chat-link');
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
    location.hash = chatHash(activeChat); updateNavigation();
    requestAnimationFrame(() => $('recovery-message').focus({preventScroll: true}));
  });
  $('reload').addEventListener('click', () => {
    loadMoodle(); refresh().catch(error => feedback(error.message, true));
  });
  if (moodleEnabled) {
    $('moodle-refresh').addEventListener('click', loadMoodle);
    // Keep a visible workspace current; the server coalesces feed requests.
    window.setInterval(() => { if (!document.hidden && scope) loadMoodle(); }, 5 * 60 * 1000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && scope) loadMoodle();
    });
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
  async function signOut() {
    if (busy || chatBusy) return;
    try { await api('/auth/signout', {}); ++generation; clearDrafts(); scope = null; activeRun = null;
      chats.reset(); clearDrafts(); activeChat = 'default'; ++chatGeneration;
      ++moodleRevision; moodleBusy = false;
      clearMoodle('Войдите в Google AI, чтобы увидеть свои задания и баллы.');
      if (moodleEnabled) { $('moodle-refresh').disabled = true; $('moodle').setAttribute('aria-busy', 'false'); }
      $('panel').replaceChildren(); $('trace-records').replaceChildren(); $('signout').hidden = true;
      $('chat-history').replaceChildren(); $('chat-history').hidden = true; $('conversation-index').replaceChildren();
      $('workspace').classList.remove('has-chat'); $('recovery-message').value = '';
      chatAvailable = false; chatWorking = false; chatLimitReached = false; pendingText = null;
      $('chat-status').textContent = 'Войдите, чтобы открыть чат.';
      $('trace-health').textContent = 'Войдите, чтобы загрузить журнал.';
      history.replaceState(null, '', '/'); updateNavigation();
      feedback('Вы вышли. Для просмотра плана требуется вход.'); controls();
      if (document.body.dataset.signinUrl === '/login') window.location.assign('/login');
    } catch (error) {
      if (error.status === 403 && document.body.dataset.signinUrl === '/login') {
        saveDraft();
        window.location.assign('/login');
        return;
      }
      feedback(error.message, true);
    }
  }
  $('signout').addEventListener('click', signOut);

  loadMoodle();
  refresh().catch(error => feedback(error.message, true));
})();
"""
SCRIPT = WORKSPACE_SCRIPT + ANSWER_REVEAL_SCRIPT + STUDY_TRACE_SCRIPT + SCRIPT
from .sidebar import SCRIPT as SIDEBAR_SCRIPT
SCRIPT = SIDEBAR_SCRIPT + SCRIPT
