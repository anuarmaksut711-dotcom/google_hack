"""Authenticated weekly schedule editor; all records use the current owner."""
from fastapi import Request
from fastapi.responses import JSONResponse

from ..lms.manual_schedule import WeeklySchedule
from .app import body


ACCOUNT_CLASSES = '''<section aria-labelledby="classes-heading"><h2 id="classes-heading">Моё расписание занятий</h2>
<p>Добавьте пары на неделю. Они повторяются в выбранный период, максимум 91 день.
Время — по часовому поясу аккаунта. Названия и время занятий передаются Gemini при составлении плана.</p>
<form id="class-schedule"><fieldset id="classes-fields" disabled><legend>Недельное расписание</legend>
<label for="classes-from">Действует с</label><input id="classes-from" type="date" required>
<label for="classes-until">Действует по</label><input id="classes-until" type="date" required>
<div id="classes-rows"></div><button id="class-add" type="button">Добавить пару</button>
<p>Если занятий в этом периоде нет, удалите все пары и сохраните пустое расписание.</p>
<button type="submit">Сохранить расписание</button></fieldset></form>
<p id="classes-status" role="status" aria-live="polite">Загружаем расписание…</p></section>'''


CLASSES_SCRIPT = r'''
(() => {
  const form = document.getElementById('class-schedule'), fields = document.getElementById('classes-fields');
  const rows = document.getElementById('classes-rows'), status = document.getElementById('classes-status');
  const from = document.getElementById('classes-from'), until = document.getElementById('classes-until');
  const add = document.getElementById('class-add');
  const days = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];
  let serial = 0;
  function row(value = {}) {
    const fieldset = document.createElement('fieldset'), legend = document.createElement('legend');
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
    remove.addEventListener('click', () => { fieldset.remove(); add.disabled = rows.children.length >= 30; add.focus(); });
    fieldset.append(remove); rows.append(fieldset); add.disabled = rows.children.length >= 30; return subject;
  }
  add.addEventListener('click', () => { if(rows.children.length < 30) row().focus(); });
  async function api(data) {
    const options = {credentials:'same-origin'};
    if(data !== undefined) {
      const cookie = document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith('__Host-cp-csrf='));
      if(!cookie) throw Error();
      Object.assign(options, {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':decodeURIComponent(cookie.split('=')[1])}, body:JSON.stringify(data)});
    }
    const response = await fetch('/api/account/classes', options);
    if(response.status === 403) { location.replace('/login'); throw Error(); }
    if(!response.ok) throw Error(); return response.json();
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const classes = Array.from(rows.children, fieldset => {
      const value = {};
      fieldset.querySelectorAll('[data-field]').forEach(input => { value[input.dataset.field] = input.dataset.field === 'weekday' ? Number(input.value) : input.value.trim(); });
      return value;
    });
    if(until.value < from.value || (Date.parse(until.value)-Date.parse(from.value))/86400000 > 90) {
      status.textContent = 'Выберите период от 1 до 91 дня.'; until.focus(); return;
    }
    if(classes.some(c => !c.subject || c.end <= c.start)) { status.textContent = 'Заполните предметы и укажите окончание каждой пары позже начала.'; return; }
    const sorted = [...classes].sort((a,b)=>a.weekday-b.weekday || a.start.localeCompare(b.start));
    if(sorted.some((c,i)=>i && c.weekday===sorted[i-1].weekday && c.start < sorted[i-1].end)) {
      status.textContent = 'Пары пересекаются. Исправьте время перед сохранением.'; return;
    }
    fields.disabled = true; status.textContent = 'Сохраняем расписание…';
    try { await api({valid_from:from.value,valid_until:until.value,classes}); status.textContent = 'Расписание сохранено. Новые планы в чате будут учитывать эти пары.'; }
    catch { status.textContent = 'Не удалось сохранить расписание. Данные остались в форме — попробуйте ещё раз.'; }
    finally { fields.disabled = false; }
  });
  async function load() {
    try {
      const value = await api();
      if(value.schedule) { from.value = value.schedule.valid_from; until.value = value.schedule.valid_until; value.schedule.classes.forEach(row); }
      else row();
      fields.disabled = false;
      status.textContent = value.connected ? 'Ваше сохранённое расписание. Изменения нужно сохранить.' : 'Заполните период и добавьте свои пары.';
    } catch { status.textContent = 'Не удалось загрузить расписание. Перезагрузите страницу перед редактированием.'; }
  }
  load();
})();'''


def install_class_routes(app, *, auth, account):
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
