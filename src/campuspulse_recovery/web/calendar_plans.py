"""Authenticated preview, exact approval and MCP dispatch for chat proposals."""
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ..domain.models import Contract
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied
from .app import body


class Selection(Contract):
    plan_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    payload_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class ClassPublication(Contract):
    """The saved owner-bound timetable is the only source of class events."""


def install_plan_routes(app, *, auth, calendar):
    owner = auth.policy.config.student.trusted_user_id

    async def propose(blocks, request_key, session):
        source = await calendar.tools(owner).read(session=session)
        if source.get('error'):
            return False
        await calendar.grants.session(calendar.raw_documents, owner, session)
        await calendar.plans.propose(owner, request_key, blocks, source,
                                     class_timezone=auth.policy.config.student.timezone)
        await calendar.grants.session(calendar.raw_documents, owner, session)
        return True
    app.state.calendar_plan_proposer = propose

    @app.post('/api/google-calendar/plans/classes')
    async def propose_classes(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        await body(request, ClassPublication)
        try:
            horizon = await calendar.plans.class_horizon(owner,
                class_timezone=auth.policy.config.student.timezone)
            source = await calendar.tools(owner).read(session=credential.session.binding.session_id,
                horizon=horizon)
            if source.get('error'):
                return JSONResponse({'error': 'calendar_source_unavailable'}, status_code=503)
            if (await auth.load(request, mutation=True)).session != credential.session:
                raise AccessDenied('calendar_session_changed')
            plan = await calendar.plans.propose_classes(owner, source,
                class_timezone=auth.policy.config.student.timezone)
            if (await auth.load(request, mutation=True)).session != credential.session:
                raise AccessDenied('calendar_session_changed')
            _, state = await calendar.plans.get(owner, plan['plan_id'])
            return {'status': state['status'], 'plan_id': plan['plan_id']}
        except BindingConflict as error:
            return JSONResponse({'error': str(error)}, status_code=409)

    @app.get('/api/google-calendar/plans')
    async def plans(request: Request):
        credential = await auth.load(request)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        items = await calendar.plans.list(owner)
        if (await auth.load(request)).session != credential.session:
            raise AccessDenied('calendar_session_changed')
        return {'items': items}

    @app.post('/api/google-calendar/plans/approve')
    async def approve(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        value = await body(request, Selection)
        try:
            plan, _ = await calendar.plans.preflight(owner, value.plan_id)
            source = await calendar.tools(owner).read(session=credential.session.binding.session_id,
                **({'horizon': plan['horizon']} if plan.get('purpose') == 'classes' else {}))
            if source.get('error'):
                return JSONResponse({'error': 'calendar_source_unavailable'}, status_code=503)
            await calendar.plans.approve(owner, value.plan_id, value.payload_hash,
                credential.session.binding.session_id, source=source)
            if (await auth.load(request, mutation=True)).session != credential.session:
                raise AccessDenied('calendar_session_changed')
            return {'status': 'approved'}
        except BindingConflict:
            return JSONResponse({'error': 'calendar_plan_changed'}, status_code=409)

    async def dispatch(request, action):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        value = await body(request, Selection)
        try:
            plan, state = await calendar.plans.preflight(owner, value.plan_id)
        except BindingConflict:
            return JSONResponse({'error': 'calendar_plan_changed'}, status_code=409)
        if plan['hash'] != value.payload_hash:
            return JSONResponse({'error': 'calendar_plan_changed'}, status_code=409)
        result = await calendar.tools(owner).call(action, session=credential.session.binding.session_id,
                                                 plan_id=value.plan_id)
        if (await auth.load(request, mutation=True)).session != credential.session:
            raise AccessDenied('calendar_session_changed')
        if result.get('trace'):
            await calendar.plans.state(owner, value.plan_id, last_tool_call=result['trace'])
        return JSONResponse(result, status_code=503 if result.get('error') else 200)

    @app.post('/api/google-calendar/plans/apply')
    async def apply(request: Request):
        return await dispatch(request, 'apply_plan')

    @app.post('/api/google-calendar/plans/verify')
    async def verify(request: Request):
        return await dispatch(request, 'verify_plan')


MARKUP = '''<section id="calendar-plans" aria-labelledby="calendar-plans-heading">
<h2 id="calendar-plans-heading">Предложенное расписание</h2>
<p>Укажите, когда можете учиться и сколько времени нужно на задания. Ассистент предложит расписание по вашим заданиям Moodle, парам и календарю.</p>
<button type="button" data-action="compose-schedule">Составить расписание</button>
<p>Проверьте предложенное время, подтвердите расписание и нажмите «Применить в Google Calendar».</p>
<p id="calendar-plans-status" role="status" aria-live="polite"></p>
<div id="calendar-plan-list"></div></section>'''

SCRIPT = r'''
;(()=>{
 const labels={draft:'Проверьте расписание',approved:'Расписание подтверждено',applying:'Добавляем события…',
 verified:'Добавлено в Google Calendar',partial:'Расписание добавлено частично. Проверьте результат.',
 unknown:'Результат записи пока неизвестен. Проверьте календарь.',applied_unverified:'Запись пока не подтверждена. Проверьте результат.',
 stale:'Расписание устарело. Составьте и подтвердите новое.',verification_failed:'Не удалось подтвердить запись. Проверьте календарь.'};
 const visible=()=>document.body.dataset.view==='plan';
 let loading=false,readRevision=0,reloadPending=false,mutations=0;
 async function load(){
  const root=document.getElementById('calendar-plans');
  if(!root||!visible())return;
  if(loading||mutations){reloadPending=true;return;}
  loading=true;reloadPending=false;const revision=readRevision;
  const list=document.getElementById('calendar-plan-list'),notice=document.getElementById('calendar-plans-status');
  try{
   const response=await fetch('/api/google-calendar/plans',{credentials:'same-origin'});
   if(!response.ok)throw Error();
   const data=await response.json();
   if(document.getElementById('calendar-plans')!==root||revision!==readRevision)return;
   list.replaceChildren();
   for(const {plan,state} of data.items){
    const card=document.createElement('article');card.className='calendar-plan-card';card.dataset.status=state.status;
    const heading=document.createElement('h3');heading.textContent=(plan.purpose==='classes'?'Пары · ':'Учебные блоки · ')+(labels[state.status]||'Не удалось определить состояние расписания');card.append(heading);
    const format=new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:plan.purpose==='classes'?plan.class_timezone:plan.timezone});
    const ul=document.createElement('ul');
    for(const block of plan.blocks){const li=document.createElement('li');
     if(plan.purpose==='classes'&&block.recurrence){
      const day=new Intl.DateTimeFormat('ru-RU',{weekday:'long',timeZone:block.timezone});
      const time=new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',timeZone:block.timezone});
      const date=new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeZone:'UTC'});
      li.textContent=day.format(new Date(block.start))+', '+time.format(new Date(block.start))+' — '+time.format(new Date(block.end))+' · '+block.title+
       '. Каждую неделю до '+date.format(new Date(block.valid_until+'T12:00:00Z'))+'. Ближайшая пара: '+format.format(new Date(block.start))+'.';
     }else li.textContent=format.format(new Date(block.start))+' — '+format.format(new Date(block.end))+' · '+block.title;
     ul.append(li);}
    card.append(ul);
    const zone=document.createElement('p');zone.textContent='Часовой пояс: '+(plan.purpose==='classes'?plan.class_timezone:plan.timezone);card.append(zone);
    const details=document.createElement('details'),summary=document.createElement('summary'),operations=document.createElement('pre');
    summary.textContent='Сведения о расписании и подтверждении';
    const trace=state.last_tool_call;
    operations.textContent=JSON.stringify({plan_id:plan.plan_id,payload_hash:plan.hash,operations:state.operations,
      last_tool_call:trace?{tool:trace.tool,status:trace.status,operation_count:trace.operation_count,completed_count:trace.completed_count}:undefined},null,2);
    details.append(summary,operations);card.append(details);
    const evidence=document.createElement('div');card.append(evidence);
    if(state.status==='verified'&&state.verified_at){const p=document.createElement('p');p.textContent='Проверено: '+format.format(new Date(state.verified_at));evidence.append(p);}
    if(state.status==='verified')for(const link of state.links||[]){
     let url;try{url=new URL(link);}catch{continue;}
     if(url.protocol!=='https:'||!['www.google.com','calendar.google.com'].includes(url.hostname)||url.username||url.password||url.port||
        !(url.pathname==='/calendar'||url.pathname.startsWith('/calendar/')))continue;
     const a=document.createElement('a');a.href=link;a.target='_blank';a.rel='noopener noreferrer';a.textContent='Открыть событие в Google Calendar';evidence.append(a,document.createElement('br'));}
    const actions=[];
    if(state.status==='draft')actions.push(['approve','Подтвердить расписание']);
    if(state.status==='approved')actions.push(['apply','Применить в Google Calendar']);
    if(['partial','unknown','applied_unverified'].includes(state.status))actions.push(['verify','Проверить результат'],['apply','Продолжить добавление']);
    if(state.status==='applying')actions.push(['verify','Проверить результат']);
    if(state.status==='verified')actions.push(['verify','Проверить в Google Calendar']);
    for(const [action,label] of actions){const button=document.createElement('button');button.type='button';button.textContent=label;
     button.addEventListener('click',async()=>{
      if(mutations)return;
      ++mutations;++readRevision;
      const buttons=list.querySelectorAll?.('button')||[button];buttons.forEach(item=>item.disabled=true);
      if(card.dataset.status==='verified'){
       card.dataset.status='unknown';heading.textContent='Проверяем запись в Google Calendar…';evidence.replaceChildren();
      }
      notice.textContent=action==='approve'?'Сохраняем подтверждение…':action==='apply'?'Добавляем события в Google Calendar…':'Проверяем Google Calendar…';
      try{const result=await calendarPost('/api/google-calendar/plans/'+action,{plan_id:plan.plan_id,payload_hash:plan.hash});
       card.dataset.status=result.status||'unknown';
       heading.textContent=labels[result.status]||labels.unknown;
       if(result.status!=='verified')evidence.replaceChildren();
       notice.textContent=result.error?'Не удалось подтвердить результат. Проверьте календарь и обновите расписание.':labels[result.status]||'Состояние обновлено.';
      }catch{notice.textContent='Не удалось выполнить действие. Обновите расписание и проверьте подключение Google Calendar.';}
      finally{--mutations;++readRevision;buttons.forEach(item=>item.disabled=false);await load();}
     });card.append(button);}
    list.append(card);
   }
   notice.textContent=data.items.length?'':'Предложенное расписание появится здесь после вашего запроса ассистенту.';
  }catch{if(revision===readRevision&&document.getElementById('calendar-plans')===root)
    notice.textContent='Не удалось загрузить расписание. Нажмите «Обновить состояние».';}
  finally{loading=false;if(reloadPending||document.getElementById('calendar-plans')!==root){reloadPending=false;load();}}
 }
 window.addEventListener('hashchange',load);
 window.addEventListener('campuspulse:panel-ready',load);
 document.getElementById('reload')?.addEventListener('click',load);
 if(visible())load();
})();
'''
