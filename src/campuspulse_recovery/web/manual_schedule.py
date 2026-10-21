"""Authenticated weekly schedule editor; all records use the current owner."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lms.manual_schedule import WeeklySchedule
from ..lms.schedule_import import (DOCUMENT_SIGNATURES, IMPORT_REVIEW_WARNING,
    MAX_DOCUMENT_BYTES, ScheduleImportDraft, validate_document)
from ..security.context import AccessDenied
from .app import body


ACCOUNT_CLASSES = '''<section aria-labelledby="classes-heading"><h2 id="classes-heading">Моё расписание занятий</h2>
<p>Соберите все пары на неделю: загрузите расписание целиком или добавляйте скриншоты по дням.
Пары повторяются в выбранный период и исключаются из времени на задания.</p>
<button id="classes-reload" type="button">Перечитать расписание</button>
<form id="class-schedule"><fieldset id="classes-fields" disabled><legend>Недельное расписание</legend>
<label for="classes-file">PDF или скриншот расписания</label>
<input id="classes-file" type="file" accept="application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg" aria-describedby="classes-file-help">
<p id="classes-file-help">PDF, PNG или JPEG до 8 МиБ, одна группа. Каждый следующий файл добавляет пары в черновик.
Файл отправляется AI-провайдеру по кнопке «Распознать пары» и не хранится приложением.</p>
<button id="classes-import" type="button" disabled>Распознать пары</button>
<p>Проверьте распознанные пары и добавьте остальные дни. Даты действия укажите сами, если их нет в файле.
Изменения учитываются в плане после сохранения.</p>
<details><summary>Как учитываются пары</summary><p>До 30 пар, период от 1 до 91 дня с покрытием ближайших 72 часов.
Время — по часовому поясу аккаунта. После сохранения можно подготовить пары для Google Calendar,
просмотреть повторы и отдельно подтвердить добавление.
Названия и время занятий передаются Gemini при составлении плана.
Неясные данные могут не распознаться; чередующиеся недели введите вручную.</p></details>
<label for="classes-from">Действует с</label><input id="classes-from" type="date" required>
<label for="classes-until">Действует по</label><input id="classes-until" type="date" required>
<div id="classes-rows"></div><button id="class-add" type="button">Добавить пару</button>
<p>Если занятий в этом периоде нет, удалите все пары и сохраните пустое расписание.</p>
<div id="classes-review" hidden><input id="classes-reviewed" type="checkbox">
<label for="classes-reviewed">Я проверил(а) предметы, дни, время и даты действия</label>
<input id="classes-week-complete" type="checkbox">
<label for="classes-week-complete">Все пары недели добавлены. В остальные дни пар нет</label>
<p>Скриншот одного дня не показывает всю неделю. Добавьте остальные дни перед сохранением.</p></div>
<button type="submit">Сохранить расписание</button></fieldset></form>
<button id="classes-calendar" type="button" disabled>Подготовить пары для Google Calendar</button>
<p><a id="classes-calendar-review" href="/#panel" hidden>Просмотреть и подтвердить пары в Google Calendar</a></p>
<p id="classes-status" role="status" aria-live="polite">Загружаем расписание…</p></section>'''


CLASSES_SCRIPT = r'''
(() => {
  'use strict';
  const form = document.getElementById('class-schedule'), fields = document.getElementById('classes-fields');
  const rows = document.getElementById('classes-rows'), status = document.getElementById('classes-status');
  const from = document.getElementById('classes-from'), until = document.getElementById('classes-until');
  const add = document.getElementById('class-add'), file = document.getElementById('classes-file');
  const recognize = document.getElementById('classes-import'), reload = document.getElementById('classes-reload');
  const review = document.getElementById('classes-review'), reviewed = document.getElementById('classes-reviewed');
  const weekComplete = document.getElementById('classes-week-complete');
  const calendar = document.getElementById('classes-calendar'), calendarReview = document.getElementById('classes-calendar-review');
  const chatDraft = String(location.hash || '').match(/^#draft=([0-9a-f]{64})$/)?.[1];
  const days = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];
  let serial = 0, busy = false, loaded = false, needsReload = true, sessionId = null, reviewRequired = false, sourceVersion = 0, starter = null, savedReady = false, draftPending = !!chatDraft;
  function controls() {
    fields.disabled = busy || needsReload || !loaded;
    reload.disabled = busy;
    add.disabled = rows.children.length >= 30;
    recognize.disabled = busy || needsReload || !loaded || !file.files.length;
    calendar.disabled = busy || needsReload || !loaded || !savedReady;
    form.setAttribute('aria-busy', String(busy));
  }
  function resetReview() { reviewed.checked = false; if(weekComplete) weekComplete.checked = false; }
  function changedSchedule() { savedReady = false; calendarReview.hidden = true; resetReview(); controls(); }
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
  function row(value = {}, isStarter = false) {
    const fieldset = document.createElement('fieldset'), legend = document.createElement('legend');
    if(isStarter) starter = fieldset;
    legend.textContent = 'Пара'; fieldset.append(legend);
    function input(label, key, type, initial) {
      const id = 'class-' + (++serial), text = document.createElement('label');
      text.htmlFor = id; text.textContent = label;
      const control = document.createElement(type === 'select' ? 'select' : 'input');
      control.id = id; control.dataset.field = key; control.required = true;
      if(type === 'select') days.forEach((day, index) => {
        const option = document.createElement('option'); option.value = index; option.textContent = day; control.append(option);
      }); else control.type = type;
      if(key === 'subject') control.maxLength = 120;
      control.value = value[key] ?? initial; fieldset.append(text, control); return control;
    }
    const subject = input('Предмет', 'subject', 'text', '');
    input('День недели', 'weekday', 'select', 0); input('Начало', 'start', 'time', '09:00'); input('Окончание', 'end', 'time', '10:20');
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Удалить пару';
    remove.addEventListener('click', () => {
      if(busy || needsReload) return;
      fieldset.remove(); changedSchedule(); add.focus();
    });
    fieldset.append(remove); rows.append(fieldset); return subject;
  }
  add.addEventListener('click', () => {
    if(!busy && !needsReload && rows.children.length < 30) { row().focus(); changedSchedule(); }
  });
  function changed(event) {
    if(event.target === reviewed || event.target === weekComplete) return;
    if(starter && Array.from(starter.querySelectorAll('[data-field]')).includes(event.target)) starter = null;
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
    busy = true; controls();
    try { await task(); }
    catch(error) { status.textContent = error.message; }
    finally { busy = false; controls(); }
  }
  recognize.addEventListener('click', () => {
    if(needsReload || !loaded) return;
    action(async () => {
      const selected = file.files[0], version = sourceVersion;
      if(!selected) throw Error('Выберите PDF, PNG или JPEG.');
      if(!['application/pdf','image/png','image/jpeg'].includes(selected.type)) throw Error('Поддерживаются PDF, PNG и JPEG. Пары также можно заполнить вручную.');
      if(!selected.size || selected.size > 8 * 1024 * 1024) throw Error('Выберите непустой файл размером до 8 МиБ или заполните пары вручную.');
      resetReview(); status.textContent = 'Распознаём пары. Сохранённое расписание пока не изменилось…';
      const value = await api('/api/account/classes/import', selected, selected.type), draft = value && value.draft;
      if(version !== sourceVersion) throw Error('Выбран другой файл. Распознайте его перед проверкой.');
      if(!validDraft(draft))
        throw Error('Не удалось разобрать расписание. Пары можно заполнить вручную.');
      const existing = Array.from(rows.children).filter(fieldset => fieldset !== starter);
      const keys = new Set(existing.map(fieldset => classKey(readRow(fieldset)))), additions = [];
      for(const lesson of draft.classes) {
        const key = classKey(lesson);
        if(!keys.has(key)) { additions.push(lesson); keys.add(key); }
      }
      if(existing.length + additions.length > 30)
        throw Error('В черновике может быть до 30 пар. Удалите лишние пары и распознайте файл ещё раз.');
      if(starter) { starter.remove(); starter = null; }
      additions.forEach(lesson => row(lesson));
      if(!from.value && draft.valid_from) from.value = draft.valid_from;
      if(!until.value && draft.valid_until) until.value = draft.valid_until;
      reviewRequired = true; review.hidden = false; resetReview();
      savedReady = false; calendarReview.hidden = true;
      status.textContent = additions.length ? 'Пары добавлены в черновик. Загрузите остальные дни или добавьте их вручную. Перед сохранением проверьте всю неделю и даты действия.' :
        'Эти пары уже есть в черновике. Загрузите остальные дни и проверьте всю неделю перед сохранением.';
      from.focus();
    });
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    if(busy || needsReload || !loaded) return;
    action(async () => {
      const classes = Array.from(rows.children, readRow);
      const period = (Date.parse(until.value) - Date.parse(from.value)) / 86400000;
      if(!/^\d{4}-\d{2}-\d{2}$/.test(from.value) || !/^\d{4}-\d{2}-\d{2}$/.test(until.value) ||
          !Number.isFinite(period) || period < 0 || period > 90) {
        until.focus(); throw Error('Укажите обе даты действия: период от 1 до 91 дня.');
      }
      if(classes.length > 30 || classes.some(c => !c.subject || c.subject.length > 120 ||
          !Number.isInteger(c.weekday) || c.weekday < 0 || c.weekday > 6 ||
          !/^([01]\d|2[0-3]):[0-5]\d$/.test(c.start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(c.end) || c.end <= c.start))
        throw Error('Заполните предметы и укажите окончание каждой пары позже начала.');
      const sorted = [...classes].sort((a,b) => a.weekday-b.weekday || a.start.localeCompare(b.start));
      if(sorted.some((c,i) => i && c.weekday === sorted[i-1].weekday && c.start < sorted[i-1].end))
        throw Error('Пары пересекаются. Исправьте время перед сохранением.');
      if(reviewRequired && !reviewed.checked) { reviewed.focus(); throw Error('Подтвердите, что проверили предметы, дни, время и даты распознанного расписания.'); }
      if(reviewRequired && !weekComplete?.checked) { weekComplete?.focus(); throw Error('Добавьте все пары недели и подтвердите, что в остальные дни пар нет.'); }
      status.textContent = 'Сохраняем расписание…';
      await api('/api/account/classes', {valid_from:from.value, valid_until:until.value, classes});
      reviewRequired = false; review.hidden = true; resetReview();
      savedReady = classes.length > 0;
      status.textContent = 'Расписание сохранено. Новые планы будут учитывать эти пары. Для добавления пар в календарь нажмите «Подготовить пары для Google Calendar».';
    });
  });
  calendar.addEventListener('click', () => {
    if(busy || needsReload || !loaded || !savedReady) return;
    action(async () => {
      calendarReview.hidden = true;
      status.textContent = 'Проверяем Calendar и готовим пары для просмотра…';
      await api('/api/google-calendar/plans/classes', {});
      calendarReview.hidden = false;
      status.textContent = 'Пары подготовлены. Откройте предложенное расписание, проверьте даты повторов и подтвердите добавление.';
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
    rows.replaceChildren(); starter = null; from.value = ''; until.value = ''; file.value = '';
    if(draftValue) {
      from.value = draftValue.draft.valid_from || ''; until.value = draftValue.draft.valid_until || '';
      draftValue.draft.classes.forEach(lesson => row(lesson));
    } else if(value.schedule) {
      from.value = value.schedule.valid_from; until.value = value.schedule.valid_until; value.schedule.classes.forEach(lesson => row(lesson));
    } else row({}, true);
    reviewRequired = !!draftValue; review.hidden = !reviewRequired; resetReview(); sourceVersion++;
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
