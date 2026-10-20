"""Small authenticated forms over the existing revisioned planning API."""
from fastapi import Request
from fastapi.responses import HTMLResponse, Response

PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Время для учебного плана · Google AI</title><link rel="stylesheet" href="/assets/login.css">
<main><a href="/">К заданиям и плану</a><h1>Время для учебного плана</h1>
<p>Укажите оставшееся время на задания и часы, когда вы можете учиться.
Эти данные используются при создании предложения. Запись в Google Calendar потребует отдельного согласования плана.</p>
<p>Указывайте свободные часы с учётом пар и других обязательств. Календарная ссылка Moodle сообщает сроки, но не объём работы и не факт сдачи.</p>
<p>Для расчёта используйте экспорт Moodle «Все события» и «Недавние и следующие 60 дней».</p>
<p id="planning-status" role="status" aria-live="polite">Загружаем настройки…</p>
<button id="planning-reload" type="button">Перечитать настройки</button>
<form id="effort-form"><h2>Оставшееся время</h2>
<label for="planning-task">Задание</label><select id="planning-task" required></select>
<label for="planning-minutes">Сколько минут осталось</label>
<input id="planning-minutes" type="number" min="0" max="100000" step="1" aria-describedby="effort-help">
<p id="effort-help">Пустое поле — время пока неизвестно. 0 — не включать задание в новый план.
Выполненные задания можно отметить на странице заданий.</p>
<button type="submit" disabled>Сохранить время задания</button></form>
<form id="windows-form"><h2>Свободные часы</h2><p id="planning-zone"></p>
<ul id="planning-windows"></ul>
<label for="planning-start">Начало свободного времени</label><input id="planning-start" type="datetime-local" step="900">
<label for="planning-end">Конец свободного времени</label><input id="planning-end" type="datetime-local" step="900">
<button id="planning-add" type="button">Добавить интервал</button>
<p>Добавляйте конкретные интервалы с шагом 15 минут. Новый расчёт использует ближайшие 72 часа.
Пустой список после сохранения означает, что свободных часов нет.</p>
<button type="submit" disabled>Сохранить свободные часы</button></form>
<p><a href="/">Вернуться и создать предложение плана</a></p>
</main><script src="/assets/recovery-planning.js" defer></script></html>'''

SCRIPT = r'''(() => {
'use strict';
const $=id=>document.getElementById(id), status=$('planning-status');
let state=null, windows=[], busy=false;
const format=new Intl.DateTimeFormat('ru-RU',{dateStyle:'short',timeStyle:'short'});
$('planning-zone').textContent='Время устройства: '+Intl.DateTimeFormat().resolvedOptions().timeZone+'.';
function controls(){
  document.querySelectorAll('button,input,select').forEach(el=>el.disabled=busy||!state);
  $('planning-reload').disabled=busy;
  $('effort-form').querySelector('button[type=submit]').disabled=busy||!state||!$('planning-task').value;
}
async function api(path,data){
  const options={credentials:'same-origin'};
  if(data!==undefined){
    const cookies=document.cookie.split(';').map(v=>v.trim()).filter(v=>v.startsWith('__Host-cp-csrf='));
    if(cookies.length!==1)throw Error('Войдите в аккаунт ещё раз.');
    Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json',
      'X-CSRF-Token':decodeURIComponent(cookies[0].split('=')[1])},body:JSON.stringify(data)});
  }
  const response=await fetch(path,options);
  if(response.status===403){location.replace('/login');throw Error('Войдите в аккаунт ещё раз.');}
  if(response.status===409)throw Error('Задания или настройки изменились. Нажмите «Перечитать настройки» и повторите изменение.');
  if(!response.ok)throw Error('Не удалось сохранить или загрузить настройки. Проверьте интервалы и повторите.');
  return response.json();
}
function effort(){
  const value=state?.estimates.find(e=>e.task_id===$('planning-task').value);
  $('planning-minutes').value=value?.remaining_minutes??'';
}
function draw(){
  $('planning-windows').replaceChildren();
  windows.forEach((w,i)=>{
    const li=document.createElement('li'), button=document.createElement('button');
    li.append(format.format(new Date(w.start))+' — '+format.format(new Date(w.end))+' ');
    button.type='button';button.textContent='Удалить интервал';
    button.addEventListener('click',()=>{windows.splice(i,1);draw();});li.append(button);$('planning-windows').append(li);
  });controls();
}
async function action(task){
  if(busy)return;busy=true;controls();
  try{await task();}catch(error){status.textContent=error.message;}
  finally{busy=false;controls();}
}
async function load(){
  const [values,feed]=await Promise.all([api('/api/moodle/planning'),api('/api/moodle')]);
  state=values;windows=(state.working_windows||[]).map(w=>({...w}));
  $('planning-task').replaceChildren();
  (feed.snapshot?.tasks||[]).filter(t=>t.local_status!=='done').forEach(task=>{
    const option=document.createElement('option');option.value=task.id;
    option.textContent=(task.course_title?task.course_title+' · ':'')+task.title;$('planning-task').append(option);
  });effort();draw();
  status.textContent=state.working_windows===null?'Свободные часы пока не указаны. Добавьте и сохраните интервалы.':'Настройки загружены.';
}
$('planning-reload').addEventListener('click',()=>action(load));
$('planning-task').addEventListener('change',effort);
$('effort-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
  const raw=$('planning-minutes').value;
  state=await api('/api/moodle/planning',{expected_revision:state.revision,
    estimates:[{task_id:$('planning-task').value,remaining_minutes:raw===''?null:Number(raw)}]});
  status.textContent='Время задания сохранено. Создайте новое предложение плана.';
});});
$('planning-add').addEventListener('click',()=>{
  const start=new Date($('planning-start').value),end=new Date($('planning-end').value);
  if(!Number.isFinite(start.getTime())||!Number.isFinite(end.getTime())||start>=end){status.textContent='Укажите начало и конец интервала.';return;}
  if(windows.length>=32){status.textContent='Можно сохранить до 32 интервалов.';return;}
  if(windows.some(w=>start<new Date(w.end)&&end>new Date(w.start))){status.textContent='Свободные интервалы не должны пересекаться.';return;}
  windows.push({start:start.toISOString(),end:end.toISOString()});windows.sort((a,b)=>a.start.localeCompare(b.start));draw();
  status.textContent='Интервал добавлен. Сохраните свободные часы.';
});
$('windows-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{
  state=await api('/api/moodle/planning',{expected_revision:state.revision,working_windows:windows});
  windows=state.working_windows||[];draw();status.textContent='Свободные часы сохранены. Создайте новое предложение плана.';
});});
controls();action(load);
})();'''


def install_recovery_planning_page(app, *, auth, inputs):
    @app.get('/planning', response_class=HTMLResponse)
    async def page(request: Request):
        await auth.load(request)
        if request.query_params:
            raise ValueError('planning_selectors_forbidden')
        return HTMLResponse(PAGE, headers={'Content-Security-Policy':
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})

    @app.get('/assets/recovery-planning.js')
    async def script(request: Request):
        await auth.load(request)
        return Response(SCRIPT, media_type='text/javascript')
