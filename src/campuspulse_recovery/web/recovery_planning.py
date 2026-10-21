"""Small authenticated forms over the existing revisioned planning API."""
from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from .manual_schedule import ACCOUNT_CLASSES, CLASSES_SCRIPT
from .assets.planning_styles import CSS
from .views.navigation import install_page_navigation, wrap_page

PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Пары и время для учёбы · CampusPulse AI</title><link rel="stylesheet" href="/assets/planning.css">
<main class="planning-page"><a href="/#moodle">Задания Moodle</a><h1>Пары и время для учёбы</h1>
<p>Сохраните пары и время для учёбы. Затем откройте раздел <a href="/#panel">Предложенное расписание</a> и проверьте его перед добавлением в Google Calendar.</p>
<!-- CLASSES -->
<!-- PLANNING_START -->
<section id="planning-inputs" aria-label="Время для заданий">
<p>Укажите оставшееся время на задания и часы, когда вы можете учиться.
Эти данные используются при составлении расписания. Перед добавлением событий в Google Calendar вы проверите и подтвердите расписание.</p>
<p>Время пар не используется для заданий. Другие обязательства учтите при выборе свободных часов.</p>
<p id="planning-status" role="status" aria-live="polite">Загружаем настройки…</p>
<button id="planning-reload" type="button">Перечитать настройки</button>
<form id="effort-form"><h2>Оставшееся время</h2>
<label for="planning-task">Задание</label><select id="planning-task" required></select>
<p id="planning-empty" hidden></p>
<label for="planning-minutes">Сколько минут осталось</label>
<input id="planning-minutes" type="number" min="0" max="100000" step="1" aria-describedby="effort-help">
<p id="effort-help">Пустое поле — время пока неизвестно. 0 — не включать задание в новый план.
Выполненные задания можно отметить на странице заданий.</p>
<button type="submit" disabled>Сохранить время задания</button></form>
<form id="windows-form"><h2>Свободные часы</h2><p id="planning-zone"></p>
<ul id="planning-windows"></ul>
<p id="planning-windows-empty"></p>
<label for="planning-start">Начало свободного времени</label><input id="planning-start" type="datetime-local" step="900">
<label for="planning-end">Конец свободного времени</label><input id="planning-end" type="datetime-local" step="900">
<button id="planning-add" type="button">Добавить интервал</button>
<p>Добавляйте конкретные интервалы с шагом 15 минут. Новый расчёт использует ближайшие 72 часа.
Пустой список после сохранения означает, что свободных часов нет.</p>
<button type="submit" disabled>Сохранить свободные часы</button></form>
</section>
<!-- PLANNING_END -->
<p><a href="/#panel">К предложенному расписанию</a></p>
</main><script src="/assets/recovery-planning.js" defer></script></html>'''

SCRIPT = r'''(() => {
'use strict';
const $=id=>document.getElementById(id), status=$('planning-status');
let state=null, windows=[], busy=false, sessionId=null, needsReload=true;
const format=new Intl.DateTimeFormat('ru-RU',{dateStyle:'short',timeStyle:'short'});
$('planning-zone').textContent='Время устройства: '+Intl.DateTimeFormat().resolvedOptions().timeZone+'.';
function controls(){
  document.querySelectorAll('#planning-inputs button,#planning-inputs input,#planning-inputs select').forEach(el=>el.disabled=busy||!state||needsReload);
  $('planning-reload').disabled=busy;
  const noTask=!$('planning-task').value;
  $('planning-task').disabled=busy||!state||needsReload||noTask;
  $('planning-minutes').disabled=busy||!state||needsReload||noTask;
  $('effort-form').querySelector('button[type=submit]').disabled=busy||!state||needsReload||noTask;
  for(const id of ['effort-form','windows-form'])$(id).setAttribute('aria-busy',String(busy));
}
async function csrf(){
  for(let attempt=0;attempt<2;attempt++){
    const response=await fetch('/auth/session',{credentials:'same-origin',cache:'no-store',redirect:'error',
      signal:AbortSignal.timeout(15000),headers:{'X-CSRF-Bootstrap':'1'}});
    if(response.status===409&&attempt===0)continue;
    if(response.status===403){needsReload=true;location.replace('/login');throw Error('Войдите в аккаунт ещё раз.');}
    if(!response.ok)throw Error('Не удалось проверить вход. Повторите попытку.');
    const session=await response.json();
    if(!sessionId||session.session_id!==sessionId){
      needsReload=true;throw Error('Сессия изменилась. Перечитайте настройки перед сохранением.');
    }
    if(typeof session.csrf_token!=='string'||!/^[A-Za-z0-9_-]{20,256}$/.test(session.csrf_token))
      throw Error('Не удалось проверить вход. Повторите попытку.');
    return session.csrf_token;
  }
}
async function api(path,data){
  const options={credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(110000)};
  if(data!==undefined){
    Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json',
      'X-CSRF-Token':await csrf()},body:JSON.stringify(data)});
  }
  let response;
  try{response=await fetch(path,options);}
  catch(error){
    if(data!==undefined)needsReload=true;
    throw Error(data!==undefined?'Ответ на сохранение не получен. Нажмите «Перечитать настройки» и проверьте значения перед повтором.':
      'Не удалось загрузить настройки. Нажмите «Перечитать настройки».');
  }
  if(response.status===403){needsReload=true;location.replace('/login');throw Error('Войдите в аккаунт ещё раз.');}
  if(response.status===409){needsReload=true;throw Error('Задания или настройки изменились. Нажмите «Перечитать настройки» и проверьте сохранённые значения.');}
  if(!response.ok){
    if(data!==undefined&&response.status>=500)needsReload=true;
    throw Error(needsReload?'Не удалось подтвердить сохранённые значения. Нажмите «Перечитать настройки».':
      'Не удалось сохранить настройки. Проверьте значения и повторите.');
  }
  try{return await response.json();}
  catch(error){
    if(data!==undefined)needsReload=true;
    throw Error('Ответ сервера не удалось прочитать. Перечитайте сохранённые настройки перед повтором.');
  }
}
function effort(){
  const value=state?.estimates.find(e=>e.task_id===$('planning-task').value);
  $('planning-minutes').value=value?.remaining_minutes??'';
}
function draw(){
  $('planning-windows').replaceChildren();
  $('planning-windows-empty').hidden=windows.length>0;
  $('planning-windows-empty').textContent=state?.working_windows===null?
    'Свободные часы пока неизвестны. Добавьте интервалы или сохраните пустой список, если свободного времени нет.':
    'В списке нет свободных часов. Изменения действуют после сохранения.';
  windows.forEach((w,i)=>{
    const li=document.createElement('li'), button=document.createElement('button');
    li.append(format.format(new Date(w.start))+' — '+format.format(new Date(w.end))+' ');
    button.type='button';button.textContent='Удалить интервал';
    button.addEventListener('click',()=>{
      if(busy||needsReload)return;
      windows.splice(i,1);draw();status.textContent='Интервал удалён из списка. Сохраните свободные часы.';
    });li.append(button);$('planning-windows').append(li);
  });controls();
}
async function action(task,save=false){
  if(busy||save&&(!state||needsReload))return;busy=true;controls();
  try{await task();}catch(error){status.textContent=error.message;}
  finally{busy=false;controls();}
}
async function load(){
  needsReload=true;
  status.textContent='Перечитываем сохранённые настройки…';
  const session=await api('/auth/session');
  const [values,feed]=await Promise.all([api('/api/moodle/planning'),api('/api/moodle')]);
  sessionId=session.session_id;
  state=values;windows=(state.working_windows||[]).map(w=>({...w}));
  $('planning-task').replaceChildren();
  (feed.snapshot?.tasks||[]).filter(t=>t.local_status!=='done').forEach(task=>{
    const option=document.createElement('option');option.value=task.id;
    option.textContent=(task.course_title?task.course_title+' · ':'')+task.title;$('planning-task').append(option);
  });effort();draw();
  $('planning-empty').hidden=!!$('planning-task').value;
  $('planning-empty').textContent=feed.connected&&feed.snapshot?
    'Нет незавершённых заданий. Обновите Moodle или проверьте отметки выполнения на странице заданий.':
    'Задания ещё не получены. Подключите Moodle и обновите задания перед оценкой времени.';
  needsReload=false;
  status.textContent=state.working_windows===null?'Свободные часы пока не указаны. Добавьте и сохраните интервалы.':'Настройки загружены.';
}
$('planning-reload').addEventListener('click',()=>action(load));
$('planning-task').addEventListener('change',effort);
$('effort-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
  const raw=$('planning-minutes').value;
  if(!$('planning-task').value)throw Error('Сначала загрузите задание из Moodle.');
  if(raw!==''&&(!Number.isSafeInteger(Number(raw))||Number(raw)<0||Number(raw)>100000))
    throw Error('Укажите целое число минут от 0 до 100000 или оставьте поле пустым.');
  state=await api('/api/moodle/planning',{expected_revision:state.revision,
    estimates:[{task_id:$('planning-task').value,remaining_minutes:raw===''?null:Number(raw)}]});
  status.textContent='Время задания сохранено. Вернитесь к предложенному расписанию и нажмите «Составить расписание».';
},true);});
$('planning-add').addEventListener('click',()=>{
  if(busy||!state||needsReload)return;
  const start=new Date($('planning-start').value),end=new Date($('planning-end').value);
  if(!Number.isFinite(start.getTime())||!Number.isFinite(end.getTime())||start>=end){status.textContent='Укажите начало и конец интервала.';return;}
  if([start,end].some(value=>value.getMinutes()%15||value.getSeconds()||value.getMilliseconds())){
    status.textContent='Начало и конец должны быть кратны 15 минутам: например, 18:00 или 18:15.';return;
  }
  if(windows.length>=32){status.textContent='Можно сохранить до 32 интервалов.';return;}
  if(windows.some(w=>start<new Date(w.end)&&end>new Date(w.start))){status.textContent='Свободные интервалы не должны пересекаться.';return;}
  windows.push({start:start.toISOString(),end:end.toISOString()});windows.sort((a,b)=>a.start.localeCompare(b.start));draw();
  status.textContent='Интервал добавлен. Сохраните свободные часы.';
});
$('windows-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
  state=await api('/api/moodle/planning',{expected_revision:state.revision,working_windows:windows});
  windows=state.working_windows||[];draw();status.textContent='Свободные часы сохранены. Вернитесь к предложенному расписанию и нажмите «Составить расписание».';
},true);});
controls();action(load);
})();'''


def install_recovery_planning_page(app, *, auth, inputs, google_calendar_enabled=False):
    install_page_navigation(app)

    @app.get('/planning', response_class=HTMLResponse)
    async def page(request: Request):
        await auth.load(request)
        if request.query_params:
            raise ValueError('planning_selectors_forbidden')
        page = PAGE.replace('<!-- CLASSES -->', ACCOUNT_CLASSES)
        if inputs is None:
            start = page.index('<!-- PLANNING_START -->')
            end = page.index('<!-- PLANNING_END -->') + len('<!-- PLANNING_END -->')
            page = (page[:start] + '<p>Сохраните пары, затем расскажите ассистенту, когда можете учиться '
                    'и сколько времени нужно на задания. Доступность составления расписания указана в разделе '
                    '«Предложенное расписание».</p>' + page[end:])
            page = page.replace('<script src="/assets/recovery-planning.js" defer></script>', '')
        page = page.replace('</html>', '<script src="/assets/planning-classes.js" defer></script></html>')
        page = wrap_page(page, active='planning', title='Пары и время',
                         google_calendar_enabled=google_calendar_enabled)
        return HTMLResponse(page, headers={'Content-Security-Policy':
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})

    @app.get('/assets/recovery-planning.js')
    async def script(request: Request):
        await auth.load(request)
        return Response(SCRIPT, media_type='text/javascript')

    @app.get('/assets/planning-classes.js')
    async def classes_script(request: Request):
        await auth.load(request)
        return Response(CLASSES_SCRIPT, media_type='text/javascript')

    @app.get('/assets/planning.css')
    async def style(request: Request):
        await auth.load(request)
        return Response(CSS, media_type='text/css')
