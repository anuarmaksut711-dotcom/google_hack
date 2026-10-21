"""Account-scoped local Calendar routes; all writes are OAuth state only."""
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import Field

from ..domain.models import Contract
from ..integrations.local_calendar import CalendarConnectionError, LocalCalendarTools
from .app import body

ACCOUNT_GOOGLE_CALENDAR = ('<section><h2>Google Calendar</h2>'
    '<p>Подключите основной календарь, чтобы видеть события на ближайшие три дня.</p>'
    '<a href="/#calendar">Открыть календарь на сайте</a></section>')

PAGE = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Calendar · Google AI</title><link rel="stylesheet" href="/assets/login.css">
<link rel="stylesheet" href="/assets/google-calendar.css"></head>
<body><main><a href="/settings">К настройкам</a><h1>Google Calendar</h1>
<p>События основного календаря: сегодня и ближайшие три дня.</p>
<p>Ассистент использует названия и время событий, когда вы отправляете вопрос в чат.</p>
<button id="calendar-connect" type="button" disabled>Подключить Google Calendar</button>
<button id="calendar-refresh" type="button" hidden>Обновить события</button>
<button id="calendar-disconnect" type="button" hidden>Отключить календарь</button>
<p id="calendar-status" role="status" aria-live="polite"></p>
<p id="calendar-range"></p><ul id="calendar-events"></ul>
<details id="calendar-journal" hidden><summary>Журнал чтения календаря</summary><pre id="calendar-trace"></pre></details>
<p>Отключение удаляет сохранённый доступ из приложения. Разрешение Google можно отозвать
в настройках Google-аккаунта → Безопасность → Сторонние приложения.</p>
</main><script src="/assets/google-calendar.js"></script></body></html>'''

# The site and the standalone connection page share the same controls and
# authenticated endpoints. No second Calendar state or fixture data is used.
CALENDAR_SECTION = ('<section id="google-calendar" class="google-calendar" aria-labelledby="calendar-heading">'
    + PAGE.split('<a href="/settings">К настройкам</a>', 1)[1].split('</main>', 1)[0]
        .replace('<h1>Google Calendar</h1>', '<h1 id="calendar-heading">Google Calendar</h1>')
    + '<p><a href="/settings">Настройки подключений</a></p></section>')
from .calendar_plans import SCRIPT as PLAN_SCRIPT

SCRIPT = r"""'use strict';
const status=document.getElementById('calendar-status');
async function calendarPost(path,value={}){
  const csrf=document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith('__Host-cp-csrf='));
  const token=path.startsWith('/auth/google-calendar/signin/')?
    sessionStorage.getItem('cp-calendar-login-csrf'):(csrf?decodeURIComponent(csrf.split('=')[1]):null);
  if(!token)throw Error('signin');
  const response=await fetch(path,{method:'POST',credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(value)});
  if(!response.ok)throw Error(response.status===403?'signin':'request');
  return response.json();
}
function fail(error){status.textContent=error.message==='signin'?
  'Войдите в приложение и повторите подключение.':'Не удалось прочитать или подключить календарь. Попробуйте подключить его заново.';}
if(location.pathname==='/google-calendar/connect'){
  const signinButton=document.getElementById('calendar-signin');
  signinButton.disabled=true;
  (async()=>{try{
    // External navigation may first land on this public page. Probe through
    // the normal same-origin authenticated API before restoring the view.
    const response=await fetch('/api/account/google-calendar',{credentials:'same-origin'});
    if(response.ok&&(await response.json()).connected){location.replace('/#calendar');return;}
  }catch{}signinButton.disabled=false;})();
  document.getElementById('calendar-signin').addEventListener('click',async event=>{
    event.target.disabled=true;status.textContent='Открываем Google…';
    try{
      const challenge=await fetch('/auth/challenge',{credentials:'same-origin'});
      if(!challenge.ok)throw Error();
      sessionStorage.setItem('cp-calendar-login-csrf',(await challenge.json()).csrf_token);
      sessionStorage.setItem('cp-calendar-flow','signin');
      const result=await calendarPost('/auth/google-calendar/signin/start');
      location.assign(result.authorization_url);
    }catch(error){fail(error);event.target.disabled=false;}
  });
}else if(location.pathname==='/auth/google-calendar/callback'){
  const query=new URLSearchParams(location.search);
  const payload={code:query.get('code')||'',state:query.get('state')||''};
  history.replaceState(null,'','/auth/google-calendar/callback');
  async function recoverCallback(message){
    const back=document.getElementById('calendar-return')||document.querySelector('main a');
    try{
      const response=await fetch('/api/account/google-calendar',{credentials:'same-origin'});
      if(response.ok&&(await response.json()).connected){
        status.textContent='Повторное подключение не завершилось. Сохранённое подключение не удалено — откройте календарь, чтобы проверить события.';
        back.textContent='Открыть мой календарь';back.href='/#calendar';return;
      }
    }catch{}
    status.textContent=message;back.textContent='Подключить календарь заново';back.href='/google-calendar/connect';
  }
  if(query.has('error')||!payload.code||!payload.state){
    recoverCallback(query.has('error')?'Google не предоставил доступ. Вы можете начать подключение заново.':
      'Эта ссылка подключения уже использована или устарела. Начните новое подключение.');
  }else{
    status.textContent='Завершаем подключение…';
    const flow=sessionStorage.getItem('cp-calendar-flow');
    const signin=flow?flow==='signin':document.cookie.split(';').some(s=>s.trim()==='__Host-cp-calendar-flow=signin');
    calendarPost(signin?'/auth/google-calendar/signin/complete':'/api/account/google-calendar/complete',payload)
      .then(()=>{sessionStorage.removeItem('cp-calendar-login-csrf');sessionStorage.removeItem('cp-calendar-flow');location.replace('/#calendar');})
      .catch(()=>recoverCallback('Не удалось завершить подключение Google. Начните подключение заново.'));
  }
}else{
  const connect=document.getElementById('calendar-connect'),refresh=document.getElementById('calendar-refresh'),
    disconnect=document.getElementById('calendar-disconnect'),list=document.getElementById('calendar-events');
  async function load(){
    connect.disabled=refresh.disabled=true;list.replaceChildren();document.getElementById('calendar-range').textContent='';
    status.textContent='Читаем календарь…';
    try{
      const stateResponse=await fetch('/api/account/google-calendar',{credentials:'same-origin'});
      if(!stateResponse.ok)throw Error(stateResponse.status===403?'signin':'request');
      const state=await stateResponse.json();
      refresh.hidden=disconnect.hidden=!state.connected;
      connect.textContent=state.connected?(state.can_write===false?'Разрешить добавление событий':'Подключить заново'):'Подключить Google Calendar';
      if(!state.connected){status.textContent='Календарь ещё не подключён.';return;}
      const response=await fetch('/api/google-calendar/events',{credentials:'same-origin'}),data=await response.json();
      if(data.trace){document.getElementById('calendar-journal').hidden=false;
        document.getElementById('calendar-trace').textContent=JSON.stringify(data.trace,null,2);}
      if(!response.ok||data.error)throw Error();
      const format=new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:data.timezone});
      document.getElementById('calendar-range').textContent=format.format(new Date(data.horizon.start))+' — '+
        format.format(new Date(data.horizon.end))+' ('+data.timezone+')';
      for(const event of data.events){
        const item=document.createElement('li'),label=document.createElement(event.htmlLink?'a':'span');
        label.textContent=event.summary||'Без названия';
        if(event.htmlLink){label.href=event.htmlLink;label.target='_blank';label.rel='noopener noreferrer';}
        item.append(label);
        const start=event.start,end=event.end;
        item.append(document.createTextNode(start.date?' · '+start.date+' — '+end.date+' (весь день; дата окончания не включается)':
          ' · '+format.format(new Date(start.dateTime))+' — '+format.format(new Date(end.dateTime))));
        list.append(item);
      }
      status.textContent=data.events.length?'События загружены.':'На ближайшие 72 часа событий нет.';
    }catch(error){fail(error);}finally{connect.disabled=refresh.disabled=false;}
  }
  connect.addEventListener('click',async()=>{connect.disabled=true;try{
    sessionStorage.setItem('cp-calendar-flow','account');
    const result=await calendarPost('/api/account/google-calendar/start');location.assign(result.authorization_url);
  }catch(error){fail(error);connect.disabled=false;}});
  disconnect.addEventListener('click',async()=>{disconnect.disabled=true;try{
    await calendarPost('/api/account/google-calendar/disconnect');await load();
  }catch(error){fail(error);}finally{disconnect.disabled=false;}});
  refresh.addEventListener('click',load);
  const embedded=!!document.getElementById('google-calendar');
  const visible=()=>!embedded||document.body.dataset.view==='calendar';
  window.addEventListener('hashchange',()=>{if(visible())load();});
  document.getElementById('reload')?.addEventListener('click',()=>{if(visible())load();});
  if(visible())load();
}
"""

CALLBACK_PAGE = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Подключение Calendar</title>
<link rel="stylesheet" href="/assets/login.css"></head>
<body><main><h1>Подключение Google Calendar</h1><p id="calendar-status" role="status"></p>
<a id="calendar-return" href="/#calendar">Вернуться к календарю</a></main>
<script src="/assets/google-calendar.js"></script></body></html>'''

CONNECT_PAGE = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Подключение Calendar · Google AI</title>
<link rel="stylesheet" href="/assets/login.css"></head><body><main>
<h1>Подключить Google Calendar</h1><p>Войдите через Google, чтобы видеть события основного календаря на ближайшие три дня.</p>
<p>Названия и время событий помогают ассистенту учитывать занятое время. На странице Google вы увидите,
какой доступ запрашивает приложение. Для добавления расписания нужно разрешение на добавление событий.</p>
<p>Новые события добавляются только после просмотра, подтверждения расписания и отдельного нажатия «Применить в Google Calendar».</p>
<button id="calendar-signin" type="button">Войти через Google и подключить календарь</button>
<p id="calendar-status" role="status" aria-live="polite"></p><a href="/login">Обычный вход в приложение</a>
</main><script src="/assets/google-calendar.js"></script></body></html>'''


class Completion(Contract):
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=16, max_length=256)


def install_calendar_routes(app, *, auth, calendar):
    owner = auth.policy.config.student.trusted_user_id
    tools = calendar.tools(owner) if hasattr(calendar, 'tools') else LocalCalendarTools(calendar, owner)
    if getattr(calendar, 'cloud_ready', False):
        from .calendar_plans import install_plan_routes
        install_plan_routes(app, auth=auth, calendar=calendar)

    @app.get('/google-calendar', response_class=HTMLResponse)
    async def page(request: Request):
        await auth.load(request)
        return PAGE

    @app.get('/api/account/google-calendar')
    async def state(request: Request):
        await auth.load(request)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        return await calendar.status(owner)

    @app.post('/api/account/google-calendar/start')
    async def start(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        await body(request, Contract)
        url = await calendar.start(owner=owner, session=credential.session.binding.session_id)
        response = JSONResponse({'authorization_url': url})
        response.delete_cookie('__Host-cp-calendar-flow', secure=True, samesite='strict', path='/')
        return response

    @app.post('/api/account/google-calendar/complete')
    async def complete(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        value = await body(request, Completion)
        try:
            connection = await calendar.exchange(owner=owner,
                session=credential.session.binding.session_id, principal=credential.session.principal_id,
                state=value.state, code=value.code)
            if (await auth.load(request, mutation=True)).session != credential.session:
                raise ValueError('calendar_session_changed')
            await calendar.save(owner, connection)
            return {'connected': True}
        except CalendarConnectionError:
            return JSONResponse({'error': 'calendar_authorization_failed'}, status_code=400)

    @app.post('/api/account/google-calendar/disconnect')
    async def disconnect(request: Request):
        await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        await body(request, Contract)
        await calendar.disconnect(owner)
        return {'connected': False}

    @app.get('/api/google-calendar/events')
    async def events(request: Request):
        credential = await auth.load(request)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        result = await tools.read(session=credential.session.binding.session_id) if hasattr(calendar, 'tools') else await tools.read()
        if (await auth.load(request)).session != credential.session:
            raise ValueError('calendar_session_changed')
        return JSONResponse(result, status_code=503 if 'error' in result else 200)


def install_calendar_public(app, *, origin):
    """External top-level callback is a bridge, never an authenticated action.

    SameSite=Strict application cookies are sent only by its same-origin POST.
    That POST also checks the one-use state, original session, PKCE and nonce.
    """
    @app.get('/auth/google-calendar/callback', response_class=HTMLResponse)
    async def callback(request: Request):
        if (request.url.scheme != 'https' or request.headers.get('host') != origin.split('://')[1]
                or request.headers.get('sec-fetch-mode') not in (None, 'navigate')):
            return Response(status_code=403)
        return HTMLResponse(CALLBACK_PAGE, headers={
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            'Referrer-Policy': 'no-referrer'})

    @app.get('/assets/google-calendar.js')
    async def script():
        return Response(SCRIPT + PLAN_SCRIPT, media_type='text/javascript')

    @app.get('/assets/google-calendar.css')
    async def css():
        return Response('''#calendar-trace{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
#calendar-events li{margin:14px 0;overflow-wrap:anywhere}
#calendar-connect,#calendar-refresh,#calendar-disconnect{margin:4px 8px 4px 0;max-width:100%}
.google-calendar{min-height:calc(100svh - 195px);padding:34px 0;max-width:1000px}
.google-calendar>p{max-width:74ch;font-size:14px;color:var(--muted,#525d56)}
#calendar-status{padding:24px;border:1px solid var(--line,#d5dcd6);border-radius:14px;margin-top:24px}
#calendar-range{font-size:13px}
#calendar-journal{margin:28px 0;font-size:13px}
body[data-view]:not([data-view=calendar]) #google-calendar{display:none}
#calendar-plans{max-width:100%;min-width:0;overflow-wrap:anywhere}
.calendar-plan-card{min-width:0;padding:20px;margin:16px 0;border:1px solid var(--line,#d8dedb);border-radius:16px;background:var(--surface,#fff)}
.calendar-plan-card h3{margin:0 0 12px}.calendar-plan-card button{max-width:100%;margin:8px 10px 0 0}
.calendar-plan-card details{margin:16px 0;color:var(--muted,#525d56);font-size:13px}
.calendar-plan-card summary{min-height:36px;padding-block:6px;cursor:pointer}
.calendar-plan-card pre{max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere}
body[data-view=calendar] .recovery-home,body[data-view=calendar] #moodle,
body[data-view=calendar] #panel,body[data-view=calendar] .journal,
body[data-view=calendar] #feedback{display:none}
''', media_type='text/css')
