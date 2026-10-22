"""Authenticated weekly schedule editor; all records use the current owner."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lms.manual_schedule import WeeklySchedule
from ..lms.schedule_import import (DOCUMENT_SIGNATURES, IMPORT_REVIEW_WARNING,
    MAX_DOCUMENT_BYTES, ScheduleImportDraft, validate_document)
from ..security.context import AccessDenied
from .app import body


ACCOUNT_CLASSES = '''<section class="classes-section" aria-labelledby="classes-heading">
<form id="class-schedule" novalidate><fieldset id="classes-fields" disabled>
<legend class="classes-sr-only">Недельное расписание</legend>
<section class="classes-upload" aria-labelledby="classes-heading">
<div class="classes-upload-heading"><div><h2 id="classes-heading">Загрузить расписание</h2>
<p id="classes-file-help">PDF, PNG, JPEG · до 8 МиБ</p></div>
<details class="classes-upload-help"><summary>О загрузке</summary>
<p>Выберите расписание одной группы: всю неделю или скриншоты по дням.
Файл отправляется AI-провайдеру только по кнопке «Распознать пары» и не хранится приложением.
Распознавание добавляет пары в черновик. Перед сохранением проверьте предметы, время, даты и остальные дни недели.
Неясные данные могут не распознаться; чередующиеся недели введите вручную.</p></details></div>
<label class="classes-sr-only" for="classes-file">PDF или скриншот расписания</label>
<div class="classes-file-line"><input id="classes-file" type="file" accept="application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg" aria-describedby="classes-file-help">
<button id="classes-import" type="button" disabled>Распознать пары</button></div>
<p class="classes-upload-note">Можно добавлять файлы по очереди.</p></section>
<section class="classes-period" aria-label="Период действия расписания">
<div class="classes-date-fields"><div><label for="classes-from">Действует с</label>
<input id="classes-from" type="date" required></div><div><label for="classes-until">Действует по</label>
<input id="classes-until" type="date" required></div></div>
<p>Пары повторяются каждую неделю. Время — по часовому поясу аккаунта.</p>
<details class="classes-period-help"><summary>Как учитываются пары</summary><p>До 30 пар, период от 1 до 91 дня с покрытием ближайших 72 часов.
Сохранённые пары исключаются из времени на задания. Названия и время занятий передаются Gemini при составлении плана.
Если занятий в этом периоде нет, удалите все пары и сохраните пустое расписание.</p></details></section>
<section class="classes-list-panel" aria-labelledby="classes-list-heading">
<div class="classes-list-heading"><h2 id="classes-list-heading">Проверьте расписание</h2></div>
<p id="classes-empty" class="classes-empty" hidden>Пар пока нет. Загрузите расписание или добавьте первую пару.</p>
<div id="classes-rows"></div><button id="class-add" type="button">Добавить пару</button></section>
<div class="classes-actions"><button id="classes-save" type="submit">Сохранить расписание</button>
<button id="classes-calendar" type="button" disabled>Подготовить пары для Google Calendar</button></div>
</fieldset></form>
<div class="classes-calendar-actions"><p><a id="classes-calendar-review" href="/#calendar" hidden>Посмотреть расписание на сайте</a></p>
<p id="classes-status" role="status" aria-live="polite">Загружаем расписание…</p>
<button id="classes-reload" type="button">Перечитать расписание</button></div></section>'''


CLASSES_SCRIPT = r'''
(() => {
  'use strict';
  const form = document.getElementById('class-schedule'), fields = document.getElementById('classes-fields');
  const rows = document.getElementById('classes-rows'), empty = document.getElementById('classes-empty'), status = document.getElementById('classes-status');
  const from = document.getElementById('classes-from'), until = document.getElementById('classes-until');
  const add = document.getElementById('class-add'), file = document.getElementById('classes-file');
  const recognize = document.getElementById('classes-import'), reload = document.getElementById('classes-reload');
  const calendar = document.getElementById('classes-calendar'), calendarReview = document.getElementById('classes-calendar-review');
  const chatDraft = String(location.hash || '').match(/^#draft=([0-9a-f]{64})$/)?.[1];
  const days = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];
  let serial = 0, busy = false, loaded = false, needsReload = true, sessionId = null, sourceVersion = 0, savedReady = false, draftPending = !!chatDraft, pendingFocus = null;
  const rowViews = new WeakMap();
  function classRows() { return Array.from(rows.querySelectorAll('[data-class-row]')); }
  function focusControl(control) { if(busy) pendingFocus = control; else control.focus(); }
  function controls() {
    fields.disabled = busy || needsReload || !loaded;
    reload.disabled = busy;
    add.disabled = busy || needsReload || !loaded || classRows().length >= 30;
    recognize.disabled = busy || needsReload || !loaded || !file.files.length;
    calendar.disabled = busy || needsReload || !loaded || !savedReady;
    form.setAttribute('aria-busy', String(busy));
  }
  function changedSchedule() {
    savedReady = false; calendarReview.hidden = true;
    if(loaded && !busy) status.textContent = 'Изменения не сохранены. Нажмите «Сохранить расписание», когда всё готово.';
    controls();
  }
  function readRow(fieldset) {
    const value = {};
    fieldset.querySelectorAll('[data-field]').forEach(input => {
      value[input.dataset.field] = input.dataset.field === 'weekday' ? Number(input.value) : input.value.trim();
    });
    return value;
  }
  function validDate(value) {
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      Number.isFinite(Date.parse(value + 'T00:00:00Z')) && new Date(value + 'T00:00:00Z').toISOString().slice(0,10) === value;
  }
  function validClass(value) {
    return value && typeof value.subject === 'string' && value.subject.trim() && value.subject.length <= 120 &&
      Number.isInteger(value.weekday) && value.weekday >= 0 && value.weekday <= 6 &&
      typeof value.start === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(value.start) &&
      typeof value.end === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(value.end) && value.end > value.start;
  }
  function validDraft(draft) {
    return draft && Array.isArray(draft.classes) && draft.classes.length && draft.classes.length <= 30 &&
      draft.classes.every(validClass) && (draft.valid_from == null || validDate(draft.valid_from)) &&
      (draft.valid_until == null || validDate(draft.valid_until)) && (!draft.valid_from || !draft.valid_until ||
        (draft.valid_until >= draft.valid_from && (Date.parse(draft.valid_until)-Date.parse(draft.valid_from))/86400000 <= 90));
  }
  function classKey(value) { return JSON.stringify([value.subject.trim(),value.weekday,value.start,value.end]); }
  function updateSummary(fieldset) {
    const value = readRow(fieldset), view = rowViews.get(fieldset);
    view.time.textContent = value.start && value.end ? value.start + ' – ' + value.end : 'Укажите время';
    view.subject.textContent = value.subject || 'Новая пара';
    view.edit.setAttribute('aria-label', 'Изменить пару: ' + (value.subject || 'Новая пара'));
  }
  function openEditor(fieldset, opened, focus = false) {
    const view = rowViews.get(fieldset);
    view.editor.hidden = !opened;
    view.edit.setAttribute('aria-expanded', String(opened));
    if(focus) focusControl(opened ? view.subjectInput : view.edit);
  }
  function organizeRows() {
    const focused = document.activeElement, entries = classRows(), groups = [];
    for(let weekday = 0; weekday < days.length; weekday++) {
      const lessons = entries.filter(entry => {
        const value = readRow(entry).weekday;
        return (Number.isInteger(value) && value >= 0 && value <= 6 ? value : 0) === weekday;
      })
        .sort((a,b) => readRow(a).start.localeCompare(readRow(b).start));
      if(!lessons.length) continue;
      const group = document.createElement('section'), heading = document.createElement('h3');
      const count = document.createElement('span'), groupRows = document.createElement('div');
      group.className = 'classes-day'; group.dataset.weekday = String(weekday);
      heading.className = 'classes-day-heading'; heading.textContent = days[weekday];
      count.className = 'classes-day-count';
      const last = lessons.length % 10, teen = lessons.length % 100;
      count.textContent = ' · ' + lessons.length + ' ' + (teen >= 11 && teen <= 14 ? 'пар' : last === 1 ? 'пара' : last >= 2 && last <= 4 ? 'пары' : 'пар');
      heading.append(count); groupRows.className = 'classes-day-rows';
      groupRows.append(...lessons); group.append(heading, groupRows); groups.push(group);
    }
    rows.replaceChildren(...groups);
    empty.hidden = entries.length > 0;
    if(focused && rows.contains(focused)) focused.focus({preventScroll:true});
  }
  function focusInvalid(fieldset, value = readRow(fieldset)) {
    const key = !value.subject || value.subject.length > 120 ? 'subject' :
      !Number.isInteger(value.weekday) || value.weekday < 0 || value.weekday > 6 ? 'weekday' :
      !/^([01]\d|2[0-3]):[0-5]\d$/.test(value.start) ? 'start' : 'end';
    openEditor(fieldset, true);
    focusControl(fieldset.querySelector('[data-field="' + key + '"]'));
  }
  function row(value = {}, editing = false) {
    const fieldset = document.createElement('fieldset'), legend = document.createElement('legend');
    const summary = document.createElement('div'), time = document.createElement('span'), subjectText = document.createElement('span');
    const edit = document.createElement('button'), editor = document.createElement('div'), editorFields = document.createElement('div');
    const editorActions = document.createElement('div');
    fieldset.className = 'classes-entry'; fieldset.dataset.classRow = '';
    legend.className = 'classes-sr-only'; legend.textContent = 'Пара'; fieldset.append(legend);
    summary.className = 'classes-summary'; time.className = 'classes-time'; subjectText.className = 'classes-subject';
    edit.type = 'button'; edit.className = 'classes-edit'; edit.dataset.action = 'edit';
    const icon = document.createElement('span'); icon.setAttribute('aria-hidden', 'true'); icon.textContent = '✎'; edit.append(icon);
    editor.className = 'classes-editor'; editor.id = 'class-editor-' + (++serial);
    edit.setAttribute('aria-controls', editor.id); editor.hidden = !editing; edit.setAttribute('aria-expanded', String(editing));
    editorFields.className = 'classes-editor-fields'; editorActions.className = 'classes-editor-actions';
    summary.append(time, subjectText, edit); fieldset.append(summary, editor); editor.append(editorFields, editorActions);
    function input(label, key, type, initial) {
      const id = 'class-' + (++serial), text = document.createElement('label'), field = document.createElement('div');
      text.htmlFor = id; text.textContent = label;
      const control = document.createElement(type === 'select' ? 'select' : 'input');
      control.id = id; control.dataset.field = key; control.required = true;
      if(type === 'select') days.forEach((day, index) => {
        const option = document.createElement('option'); option.value = index; option.textContent = day; control.append(option);
      }); else control.type = type;
      if(key === 'subject') control.maxLength = 120;
      control.value = value[key] ?? initial; field.className = 'classes-editor-field classes-editor-field-' + key;
      field.append(text, control); editorFields.append(field); return control;
    }
    const subject = input('Предмет', 'subject', 'text', '');
    input('День недели', 'weekday', 'select', 0); input('Начало', 'start', 'time', '09:00'); input('Окончание', 'end', 'time', '10:20');
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'classes-remove'; remove.dataset.action = 'remove'; remove.textContent = 'Удалить пару';
    remove.addEventListener('click', () => {
      if(busy || needsReload) return;
      fieldset.remove(); organizeRows(); changedSchedule(); add.focus();
    });
    const done = document.createElement('button'); done.type = 'button'; done.className = 'classes-done'; done.dataset.action = 'done'; done.textContent = 'Готово';
    done.addEventListener('click', () => {
      if(busy || needsReload) return;
      if(!validClass(readRow(fieldset))) {
        focusInvalid(fieldset); status.textContent = 'Заполните предмет и укажите окончание пары позже начала.'; return;
      }
      updateSummary(fieldset); openEditor(fieldset, false); organizeRows(); edit.focus();
    });
    edit.addEventListener('click', () => {
      if(busy || needsReload) return;
      if(!editor.hidden && !validClass(readRow(fieldset))) {
        focusInvalid(fieldset); status.textContent = 'Заполните предмет и укажите окончание пары позже начала.'; return;
      }
      openEditor(fieldset, editor.hidden, true);
    });
    editorActions.append(remove, done);
    rowViews.set(fieldset, {time, subject:subjectText, edit, editor, subjectInput:subject});
    rows.append(fieldset); updateSummary(fieldset); return fieldset;
  }
  add.addEventListener('click', () => {
    if(!busy && !needsReload && loaded && classRows().length < 30) {
      const entry = row({}, true); organizeRows(); changedSchedule(); openEditor(entry, true, true);
    }
  });
  function changed(event) {
    if(event.target === file) return;
    const entry = event.target.closest?.('[data-class-row]');
    if(entry) updateSummary(entry);
    if(entry && event.type === 'change' && event.target.dataset.field === 'weekday') organizeRows();
    changedSchedule();
  }
  form.addEventListener('input', changed);
  form.addEventListener('change', changed);
  file.addEventListener('change', () => {
    sourceVersion++; changedSchedule();
    if(!busy && file.files.length) status.textContent = 'Файл выбран. Он будет отправлен на распознавание только по кнопке «Распознать пары».';
  });
  async function session(bootstrap = false) {
    for(let attempt = 0; attempt < 2; attempt++) {
      const response = await fetch('/auth/session', {credentials:'same-origin', cache:'no-store', redirect:'error',
        signal:AbortSignal.timeout(15000), ...(bootstrap ? {headers:{'X-CSRF-Bootstrap':'1'}} : {})});
      if(response.status === 409 && bootstrap && attempt === 0) continue;
      if(response.status === 403) { needsReload = true; location.replace('/login'); throw Error('Войдите в аккаунт ещё раз.'); }
      if(!response.ok) throw Error('Не удалось проверить вход. Повторите попытку.');
      const current = await response.json();
      if(typeof current.session_id !== 'string' || !current.session_id) throw Error('Не удалось проверить вход.');
      if(sessionId && current.session_id !== sessionId) {
        needsReload = true; throw Error('Сессия изменилась. Нажмите «Перечитать расписание» перед продолжением.');
      }
      if(bootstrap && (typeof current.csrf_token !== 'string' || !/^[A-Za-z0-9_-]{20,256}$/.test(current.csrf_token)))
        throw Error('Не удалось проверить вход. Повторите попытку.');
      return current;
    }
  }
  async function api(path, data, mime = 'application/json') {
    const mutation = data !== undefined;
    const options = {credentials:'same-origin', cache:'no-store', redirect:'error', signal:AbortSignal.timeout(55000)};
    if(mutation) {
      const current = await session(true);
      if(!sessionId) throw Error('Сначала перечитайте расписание.');
      Object.assign(options, {method:'POST', headers:{'Content-Type':mime, 'X-CSRF-Token':current.csrf_token},
        body:mime === 'application/json' ? JSON.stringify(data) : data});
    }
    const saving = mutation && path === '/api/account/classes';
    let response;
    try { response = await fetch(path, options); }
    catch {
      if(saving) needsReload = true;
      throw Error(saving ? 'Ответ на сохранение не получен. Перечитайте расписание и проверьте значения перед повтором.' :
        'Не удалось распознать или загрузить расписание. Попробуйте ещё раз или заполните пары вручную.');
    }
    if(response.status === 403) { needsReload = true; location.replace('/login'); throw Error('Войдите в аккаунт ещё раз.'); }
    if(!response.ok) {
      if(saving && response.status >= 500) needsReload = true;
      if(path.endsWith('/import')) {
        const messages = {
          413:'Размер файла превышает 8 МиБ. Выберите меньший файл или заполните пары вручную.',
          415:'Поддерживаются PDF, PNG и JPEG. Выберите другой файл или заполните пары вручную.',
          429:'Распознавание уже выполняется. Дождитесь результата или заполните пары вручную.',
          503:'Распознавание сейчас недоступно. Попробуйте позже или заполните пары вручную.'
        };
        throw Error(messages[response.status] || 'Не удалось распознать пары. Выберите чёткий файл одного дня или недели либо введите пары вручную.');
      }
      if(path === '/api/google-calendar/plans/classes')
        throw Error('Не удалось подготовить пары. Проверьте подключение Google Calendar, даты расписания и занятое время. Затем повторите подготовку.');
      throw Error(needsReload ? 'Не удалось подтвердить сохранение. Перечитайте расписание перед повтором.' :
        'Не удалось сохранить расписание. Проверьте даты, предметы и время.');
    }
    let value;
    try { value = await response.json(); }
    catch { if(saving) needsReload = true; throw Error('Ответ не удалось прочитать. Перечитайте расписание перед повтором.'); }
    if(mutation) await session();
    return value;
  }
  async function action(task) {
    if(busy) return;
    const origin = document.activeElement;
    let focusMoved = false;
    const observeFocus = event => {
      if(event.target !== origin && event.target !== document.body) focusMoved = true;
    };
    document.addEventListener('focusin', observeFocus);
    busy = true; controls();
    try { await task(); }
    catch(error) { status.textContent = error.message; }
    finally {
      busy = false; controls();
      document.removeEventListener('focusin', observeFocus);
      const current = document.activeElement;
      if(!focusMoved && (current === origin || current === document.body || !current)) {
        const target = needsReload && origin && form.contains(origin) ? reload : pendingFocus ||
          (origin && (origin === reload || form.contains(origin)) ? origin : null);
        if(target && target !== document.body && !target.disabled && target.isConnected !== false &&
            !target.closest('[hidden]') && (target === reload || !fields.disabled)) target.focus({preventScroll:true});
      }
      pendingFocus = null;
    }
  }
  recognize.addEventListener('click', () => {
    if(needsReload || !loaded) return;
    action(async () => {
      const selected = file.files[0], version = sourceVersion;
      if(!selected) throw Error('Выберите PDF, PNG или JPEG.');
      if(!['application/pdf','image/png','image/jpeg'].includes(selected.type)) throw Error('Поддерживаются PDF, PNG и JPEG. Пары также можно заполнить вручную.');
      if(!selected.size || selected.size > 8 * 1024 * 1024) throw Error('Выберите непустой файл размером до 8 МиБ или заполните пары вручную.');
      status.textContent = 'Распознаём пары. Сохранённое расписание пока не изменилось…';
      const value = await api('/api/account/classes/import', selected, selected.type), draft = value && value.draft;
      if(version !== sourceVersion) throw Error('Выбран другой файл. Распознайте его перед проверкой.');
      if(!validDraft(draft))
        throw Error('Не удалось разобрать расписание. Пары можно заполнить вручную.');
      const existing = classRows();
      const keys = new Set(existing.map(fieldset => classKey(readRow(fieldset)))), additions = [];
      for(const lesson of draft.classes) {
        const key = classKey(lesson);
        if(!keys.has(key)) { additions.push(lesson); keys.add(key); }
      }
      if(existing.length + additions.length > 30)
        throw Error('В черновике может быть до 30 пар. Удалите лишние пары и распознайте файл ещё раз.');
      additions.forEach(lesson => row(lesson));
      organizeRows();
      if(!from.value && draft.valid_from) from.value = draft.valid_from;
      if(!until.value && draft.valid_until) until.value = draft.valid_until;
      savedReady = false; calendarReview.hidden = true;
      status.textContent = additions.length ? 'Пары добавлены в черновик. Загрузите остальные дни или добавьте их вручную. Перед сохранением проверьте всю неделю и даты действия.' :
        'Эти пары уже есть в черновике. Загрузите остальные дни и проверьте всю неделю перед сохранением.';
      focusControl(from);
    });
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    if(busy || needsReload || !loaded) return;
    action(async () => {
      const entries = classRows(), classes = entries.map(readRow);
      const period = (Date.parse(until.value) - Date.parse(from.value)) / 86400000;
      if(!validDate(from.value) || !validDate(until.value) ||
          !Number.isFinite(period) || period < 0 || period > 90) {
        focusControl(!validDate(from.value) ? from : until); throw Error('Укажите обе даты действия: период от 1 до 91 дня.');
      }
      if(classes.length > 30) throw Error('В расписании может быть до 30 пар. Удалите лишние пары перед сохранением.');
      const invalid = classes.findIndex(value => !validClass(value));
      if(invalid !== -1) {
        focusInvalid(entries[invalid], classes[invalid]);
        throw Error('Заполните предметы и укажите окончание каждой пары позже начала.');
      }
      const sorted = entries.map((entry,i) => ({entry, value:classes[i]}))
        .sort((a,b) => a.value.weekday-b.value.weekday || a.value.start.localeCompare(b.value.start));
      const overlap = sorted.find((item,i) => i && item.value.weekday === sorted[i-1].value.weekday && item.value.start < sorted[i-1].value.end);
      if(overlap) {
        openEditor(overlap.entry, true);
        focusControl(overlap.entry.querySelector('[data-field="start"]'));
        throw Error('Пары пересекаются. Исправьте время перед сохранением.');
      }
      status.textContent = 'Сохраняем расписание…';
      await api('/api/account/classes', {valid_from:from.value, valid_until:until.value, classes});
      entries.forEach(entry => { updateSummary(entry); openEditor(entry, false); }); organizeRows();
      savedReady = classes.length > 0;
      calendarReview.hidden = false; calendarReview.href = '/#calendar';
      calendarReview.textContent = 'Посмотреть расписание на сайте';
      status.textContent = 'Расписание сохранено на сайте. Новые планы будут учитывать эти пары.';
      if(savedReady) {
        try {
          const connection = await api('/api/account/google-calendar');
          if(connection.connected && connection.can_write !== false) await prepareCalendar();
          else status.textContent += ' Подключите Google Calendar с разрешением на добавление событий на странице календаря.';
        } catch(error) {
          status.textContent = 'Расписание сохранено на сайте. ' + error.message;
        }
      }
    });
  });
  async function prepareCalendar() {
    status.textContent = 'Расписание сохранено. Проверяем Calendar и готовим пары для просмотра…';
    await api('/api/google-calendar/plans/classes', {});
    calendarReview.href = '/#panel'; calendarReview.hidden = false;
    calendarReview.textContent = 'Просмотреть и подтвердить добавление в Google Calendar';
    status.textContent = 'Расписание сохранено на сайте. Пары готовы к добавлению в Google Calendar: откройте просмотр, подтвердите и нажмите «Применить в Google Calendar».';
  }
  calendar.addEventListener('click', () => {
    if(busy || needsReload || !loaded || !savedReady) return;
    action(async () => {
      calendarReview.hidden = true;
      await prepareCalendar();
    });
  });
  async function load() {
    needsReload = true; sessionId = null; controls();
    status.textContent = 'Загружаем сохранённое расписание…';
    const current = await session(); sessionId = current.session_id;
    const value = await api('/api/account/classes');
    const draftValue = draftPending ? await api('/api/account/classes/drafts/' + chatDraft) : null;
    await session();
    if(draftValue && !validDraft(draftValue.draft)) throw Error('Не удалось прочитать черновик из чата. Вернитесь в чат и запросите его снова.');
    rows.replaceChildren(); from.value = ''; until.value = ''; file.value = '';
    if(draftValue) {
      from.value = draftValue.draft.valid_from || ''; until.value = draftValue.draft.valid_until || '';
      draftValue.draft.classes.forEach(lesson => row(lesson));
    } else if(value.schedule) {
      from.value = value.schedule.valid_from; until.value = value.schedule.valid_until; value.schedule.classes.forEach(lesson => row(lesson));
    }
    organizeRows(); sourceVersion++;
    savedReady = !draftValue && !!value.schedule?.classes.length;
    calendarReview.hidden = true; draftPending = false;
    loaded = true; needsReload = false;
    status.textContent = draftValue ? 'Черновик из чата. Проверьте все пары недели и даты. Сохранение заменит ранее сохранённое расписание. Google Calendar изменится только после отдельного подтверждения.' :
      value.connected ? 'Ваше сохранённое расписание. Изменения нужно сохранить.' :
      'Укажите период и добавьте пары вручную или распознайте файл расписания.';
  }
  reload.addEventListener('click', () => action(load));
  controls(); action(load);
})();'''


def install_class_routes(app, *, auth, account, importer=None):
    importer = importer if importer is not None else getattr(app.state, 'class_schedule_importer', None)
    importing = set()
    app.state.class_schedule_drafter = account.save_draft

    @app.get('/api/account/classes/drafts/{draft_id}')
    async def chat_draft(request: Request, draft_id: str):
        credential = await auth.load(request)
        if request.query_params:
            raise ValueError('class_schedule_selectors_forbidden')
        with auth.request_scope(credential) as context:
            draft = await account.read_draft(context, draft_id)
        if (await auth.load(request)).session != credential.session:
            raise AccessDenied('class_draft_session_changed')
        return JSONResponse({'draft': draft, 'warning': IMPORT_REVIEW_WARNING})

    @app.post('/api/account/classes/import')
    async def extract(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('class_schedule_selectors_forbidden')
        if not callable(getattr(importer, 'extract_classes', None)):
            return JSONResponse({'error': 'schedule_import_not_configured'}, status_code=503)
        mime = request.headers.get('content-type', '')
        if mime not in DOCUMENT_SIGNATURES:
            return JSONResponse({'error': 'unsupported_schedule_document'}, status_code=415)
        owner = credential.session.binding.owner_id
        if owner in importing:
            return JSONResponse({'error': 'schedule_import_busy'}, status_code=429)
        importing.add(owner)
        try:
            async with asyncio.timeout(45):
                document = bytearray()
                async for chunk in request.stream():
                    if len(document) + len(chunk) > MAX_DOCUMENT_BYTES:
                        return JSONResponse({'error': 'schedule_document_too_large'}, status_code=413)
                    document.extend(chunk)
                try:
                    validate_document(document, mime)
                except ValueError:
                    return JSONResponse({'error': 'invalid_schedule_document'}, status_code=400)
                try:
                    value = await importer.extract_classes(document=bytes(document), mime_type=mime)
                    draft = ScheduleImportDraft.model_validate(value)
                except ValueError:
                    return JSONResponse({'error': 'schedule_import_unreadable'}, status_code=422)
                latest = await auth.load(request)
                if latest.session != credential.session:
                    raise AccessDenied('schedule_import_session_changed')
                return JSONResponse({'draft': draft.wire_value(), 'warning': IMPORT_REVIEW_WARNING})
        except AccessDenied:
            raise
        except Exception:
            # Original files, model text and SDK errors are never logged or saved.
            return JSONResponse({'error': 'schedule_import_unavailable'}, status_code=503)
        finally:
            importing.discard(owner)

    @app.get('/api/account/classes')
    async def read(request: Request):
        credential = await auth.load(request)
        if request.query_params:
            raise ValueError('class_schedule_selectors_forbidden')
        with auth.request_scope(credential) as context:
            return JSONResponse(await account.read(context))

    @app.post('/api/account/classes')
    async def save(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('class_schedule_selectors_forbidden')
        value = await body(request, WeeklySchedule)
        with auth.request_scope(credential) as context:
            return JSONResponse(await account.save(context, value))
