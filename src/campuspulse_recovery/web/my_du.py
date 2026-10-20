"""Authenticated My DU setup; provider credentials never reach the browser."""
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from ..domain.models import Contract
from ..lms.my_du import ConnectMyDu, MyDuLoginError, MyDuUnavailable
from .app import body


ACCOUNT_MY_DU = '''<section aria-labelledby="my-du-heading"><h2 id="my-du-heading">Пары из My DU</h2>
<p>Подключите расписание AITU, чтобы Google AI учитывал пары при составлении плана.
Названия предметов и время занятий будут переданы Gemini вместе с заданиями.
Google AI сохранит сессию My DU для обновления расписания; её можно удалить кнопкой «Отключить My DU».</p>
<p>Подготовьте учебный год, триместр и дату понедельника первой учебной недели из своего расписания.
Если указать другую дату, пары попадут на неверные дни.</p>
<button id="my-du-start" type="button">Войти через Microsoft AITU</button>
<div id="my-du-login" hidden><p><a id="my-du-link" target="_blank" rel="noopener noreferrer">Открыть вход Microsoft</a>.
Сначала заполните параметры триместра ниже, затем откройте вход Microsoft.
После перехода на my-du.astanait.edu.kz/login скопируйте полный адрес страницы, включая часть после #.
Не ждите окончания загрузки My DU: сразу вернитесь сюда с адресом.
Вернитесь сюда и вставьте его ниже. Ссылка действует 10 минут; не отправляйте её другим людям.</p>
<form id="connect-my-du">
<label for="my-du-callback">Адрес страницы после входа</label>
<input id="my-du-callback" type="url" required autocomplete="off" maxlength="12000">
<label for="my-du-year">Год начала учебного года</label>
<input id="my-du-year" type="number" min="2020" max="2100" required>
<label for="my-du-term">Триместр</label>
<select id="my-du-term"><option value="1">Первый</option><option value="2">Второй</option>
<option value="3">Третий</option><option value="-1">Дополнительный</option></select>
<label for="my-du-monday">Понедельник первой учебной недели триместра</label>
<input id="my-du-monday" type="date" required>
<button type="submit">Подключить расписание пар</button></form></div>
<p id="my-du-status" role="status" aria-live="polite"></p>
<button id="my-du-refresh" type="button" hidden>Обновить пары</button>
<button id="my-du-disconnect" type="button" hidden>Отключить My DU</button>
<ul id="my-du-classes"></ul></section>'''


MY_DU_SCRIPT = r'''
(() => {
  const status=document.getElementById('my-du-status'), list=document.getElementById('my-du-classes');
  const refresh=document.getElementById('my-du-refresh'), disconnect=document.getElementById('my-du-disconnect');
  async function api(action, data) {
    const options={credentials:'same-origin'};
    if(data!==undefined){
      const cookies=document.cookie.split(';').map(s=>s.trim()).filter(s=>s.startsWith('__Host-cp-csrf='));
      if(cookies.length!==1)throw Error();
      Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json',
        'X-CSRF-Token':decodeURIComponent(cookies[0].split('=')[1])},body:JSON.stringify(data)});
    }
    const response=await fetch('/api/account/my-du'+action,options);
    if(response.status===403){location.replace('/login');throw Error();}
    const value=await response.json();
    if(!response.ok){const error=new Error();error.code=value.error;throw error;}
    return value;
  }
  function show(value){
    list.replaceChildren();refresh.hidden=disconnect.hidden=!value.connected;
    if(!value.connected){status.textContent='My DU пока не подключён.';return;}
    if(value.error==='reconnect'){
      status.textContent='Войдите через Microsoft AITU ещё раз, чтобы загрузить пары.';return;
    }
    if(value.error||value.stale||!value.snapshot){
      status.textContent='Не удалось получить расписание пар. Проверьте учебный год, триместр и дату первой недели. Попробуйте обновить пары или подключиться заново.';return;
    }
    const fmt=new Intl.DateTimeFormat('ru-RU',{timeZone:'Asia/Almaty',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
    const end=new Date(value.snapshot.coverage.end);
    end.setTime(end.getTime()-1);
    status.textContent='Расписание для планирования до '+fmt.format(end)+'.';
    const meetings=value.snapshot.classes.filter(item=>new Date(item.interval.end)>new Date());
    if(!meetings.length){const li=document.createElement('li');li.textContent='В этом периоде пар нет. Укажите агенту свои доступные часы.';list.append(li);}
    meetings.forEach(item=>{
      const li=document.createElement('li');li.textContent=item.subject+' · '+fmt.format(new Date(item.interval.start))+' — '+fmt.format(new Date(item.interval.end));list.append(li);
    });
  }
  async function action(button, task, failure){
    button.disabled=true;status.textContent='Загружаем расписание…';
    const messages={
      my_du_login_expired:'Запрос входа истёк. Нажмите «Войти через Microsoft AITU» и откройте новую ссылку.',
      my_du_login_mismatch:'Эта ссылка относится к другому запросу входа. Начните вход здесь заново и скопируйте адрес из новой вкладки Microsoft.',
      my_du_login_used:'Эта ссылка уже отправлена. Если пары не появились, начните вход через Microsoft заново.',
      my_du_callback_invalid:'Вставьте полный адрес страницы My DU после входа, включая часть после #.',
      my_du_login_rejected:'My DU не принял вход. Начните вход через Microsoft заново и сразу подключите расписание.',
      my_du_unavailable:'Не удалось связаться с My DU. Попробуйте подключиться позже.'
    };
    try{await task();}catch(error){
      status.textContent=messages[error.code]||failure;
      if(['my_du_login_expired','my_du_login_mismatch','my_du_login_used','my_du_login_rejected','my_du_unavailable'].includes(error.code)){
        document.getElementById('my-du-callback').value='';
      }
    }finally{button.disabled=false;}
  }
  document.getElementById('my-du-start').addEventListener('click',event=>action(event.currentTarget,async()=>{
    const value=await api('/start',{});
    document.getElementById('my-du-callback').value='';
    document.getElementById('my-du-link').href=value.url;
    document.getElementById('my-du-login').hidden=false;
    status.textContent='Откройте вход Microsoft по ссылке выше и вернитесь с адресом страницы после входа.';
  },'Не удалось начать вход. Попробуйте ещё раз.'));
  document.getElementById('connect-my-du').addEventListener('submit',event=>{
    event.preventDefault();const form=event.currentTarget, callback=document.getElementById('my-du-callback');
    action(form.querySelector('button'),async()=>{
      const value=await api('/connect',{callback_url:callback.value.trim(),
        study_year:Number(document.getElementById('my-du-year').value),term:Number(document.getElementById('my-du-term').value),
        first_week_start:document.getElementById('my-du-monday').value});
      callback.value='';document.getElementById('my-du-login').hidden=true;show(value);
    },'Не удалось подключить пары. Проверьте дату первой недели и параметры триместра, затем начните вход через Microsoft заново.');
  });
  refresh.addEventListener('click',()=>action(refresh,async()=>show(await api('/refresh',{})),
    'Не удалось обновить пары. Попробуйте ещё раз.'));
  disconnect.addEventListener('click',()=>action(disconnect,async()=>{
    show(await api('/disconnect',{}));document.getElementById('my-du-login').hidden=true;
    document.getElementById('my-du-callback').value='';
  },'Не удалось отключить My DU. Попробуйте ещё раз.'));
  api('').then(show).catch(()=>{status.textContent='Не удалось открыть подключение My DU. Перезагрузите страницу.';});
})();'''


def install_my_du_routes(app, *, auth, account):
    @app.exception_handler(MyDuLoginError)
    async def login_error(request, error):
        print(json.dumps({'event': 'my_du_login_validation_failed', 'reason': error.reason}), flush=True)
        return JSONResponse({'error': error.reason}, status_code=400)

    @app.exception_handler(MyDuUnavailable)
    async def provider_error(request, error):
        # Only fixed diagnostic categories; never log provider bodies or credentials.
        print(json.dumps({'event': 'my_du_login_failed', 'reason': error.reason,
                          'provider_status': error.provider_status}), flush=True)
        code = 'my_du_login_rejected' if error.reason == 'provider_rejected' else 'my_du_unavailable'
        return JSONResponse({'error': code}, status_code=502)

    async def invoke(request, operation=None, contract=Contract):
        credential = await auth.load(request, mutation=operation is not None)
        if request.query_params:
            raise ValueError('my_du_selectors_forbidden')
        value = None if operation is None else await body(request, contract)
        with auth.request_scope(credential) as context:
            if operation == 'connect':
                result = await account.connect(context, value)
            elif operation == 'start':
                result = await account.start(context)
            elif operation == 'refresh':
                result = await account.refresh(context)
            elif operation == 'disconnect':
                await account.disconnect(context)
                result = await account.read(context)
            else:
                result = await account.read(context)
            # Recheck the session after potentially slow provider I/O.
            if (await auth.load(request)).session != credential.session:
                from ..security.context import AccessDenied
                raise AccessDenied('session_changed')
            return JSONResponse(result)

    @app.get('/api/account/my-du')
    async def read(request: Request):
        return await invoke(request)

    @app.post('/api/account/my-du/start')
    async def start(request: Request):
        return await invoke(request, 'start')

    @app.post('/api/account/my-du/connect')
    async def connect(request: Request):
        return await invoke(request, 'connect', ConnectMyDu)

    @app.post('/api/account/my-du/refresh')
    async def refresh(request: Request):
        return await invoke(request, 'refresh')

    @app.post('/api/account/my-du/disconnect')
    async def disconnect(request: Request):
        return await invoke(request, 'disconnect')
