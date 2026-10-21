"""Authenticated preview, exact approval and MCP dispatch for chat proposals."""
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ..domain.models import Contract
from ..domain.ports import BindingConflict
from .app import body


class Selection(Contract):
    plan_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    payload_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


def install_plan_routes(app, *, auth, calendar):
    owner = auth.policy.config.student.trusted_user_id

    async def propose(blocks, request_key, session):
        source = await calendar.tools(owner).read(session=session)
        if source.get('error'):
            return False
        await calendar.plans.propose(owner, request_key, blocks, source)
        return True
    app.state.calendar_plan_proposer = propose

    @app.get('/api/google-calendar/plans')
    async def plans(request: Request):
        await auth.load(request)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        return {'items': await calendar.plans.list(owner)}

    @app.post('/api/google-calendar/plans/approve')
    async def approve(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        value = await body(request, Selection)
        try:
            await calendar.plans.approve(owner, value.plan_id, value.payload_hash,
                credential.session.binding.session_id)
            return {'status': 'approved'}
        except BindingConflict:
            return JSONResponse({'error': 'calendar_plan_changed'}, status_code=409)

    async def dispatch(request, action):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('calendar_selectors_forbidden')
        value = await body(request, Selection)
        plan, state = await calendar.plans.get(owner, value.plan_id)
        if plan['hash'] != value.payload_hash:
            return JSONResponse({'error': 'calendar_plan_changed'}, status_code=409)
        result = await calendar.tools(owner).call(action, session=credential.session.binding.session_id,
                                                 plan_id=value.plan_id)
        if (await auth.load(request, mutation=True)).session != credential.session:
            raise ValueError('calendar_session_changed')
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
<h2 id="calendar-plans-heading">Планы из чата</h2>
<p>Расскажите ассистенту о делах, встрече или задаче. Он предложит события с датой и временем.
Проверьте их здесь, подтвердите план и добавьте его в Google Calendar.</p>
<p><a href="#workspace">Составить план с ассистентом</a></p>
<p id="calendar-plans-status" role="status" aria-live="polite"></p>
<div id="calendar-plan-list"></div></section>'''

SCRIPT = r'''
;(()=>{
 const root=document.getElementById('calendar-plans'); if(!root)return;
 const list=document.getElementById('calendar-plan-list'),notice=document.getElementById('calendar-plans-status');
 const labels={draft:'Проверьте события',approved:'План подтверждён',applying:'Добавляем события…',
 verified:'Добавлено в Google Calendar',partial:'Часть событий добавлена — проверьте результат',
 unknown:'Результат записи уточняется',applied_unverified:'Требуется проверка Calendar',
 stale:'Календарь изменился. Попросите ассистента составить новый план.'};
 const visible=()=>['calendar','plan'].includes(document.body.dataset.view);
 let loading=false;
 async function load(){
  if(loading||!visible())return;loading=true;
  try{
   const response=await fetch('/api/google-calendar/plans',{credentials:'same-origin'});
   if(response.status===404){root.hidden=true;return;}
   if(!response.ok)throw Error();
   const data=await response.json();list.replaceChildren();
   for(const {plan,state} of data.items){
    const card=document.createElement('article');card.className='calendar-plan-card';
    const heading=document.createElement('h3');heading.textContent=labels[state.status]||'План';card.append(heading);
    const format=new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:plan.timezone});
    const ul=document.createElement('ul');
    for(const block of plan.blocks){const li=document.createElement('li');li.textContent=
     format.format(new Date(block.start))+' — '+format.format(new Date(block.end))+' · '+block.title;ul.append(li);}
    card.append(ul);
    const version=document.createElement('p');version.className='technical';version.textContent='Версия '+plan.plan_id.slice(0,12)+' · '+plan.timezone;card.append(version);
    const details=document.createElement('details'),summary=document.createElement('summary'),operations=document.createElement('pre');
    summary.textContent='Операции плана';operations.textContent=JSON.stringify({operations:state.operations,last_tool_call:state.last_tool_call},null,2);operations.style.overflowWrap='anywhere';operations.style.whiteSpace='pre-wrap';details.append(summary,operations);card.append(details);
    if(state.verified_at){const p=document.createElement('p');p.textContent='Проверено: '+format.format(new Date(state.verified_at));card.append(p);}
    for(const link of state.links||[]){if(!/^https:\/\/(www\.google\.com\/calendar\/|calendar\.google\.com\/)/.test(link))continue;
     const a=document.createElement('a');a.href=link;a.target='_blank';a.rel='noopener noreferrer';a.textContent='Открыть событие в Google Calendar';card.append(a,document.createElement('br'));}
    const actions=[];
    if(state.status==='draft')actions.push(['approve','Подтвердить этот план']);
    if(state.status==='approved')actions.push(['apply','Добавить в Google Calendar']);
    if(['partial','unknown','applied_unverified'].includes(state.status))actions.push(['verify','Проверить результат'],['apply','Продолжить добавление']);
    if(state.status==='applying')actions.push(['verify','Проверить результат']);
    if(state.status==='verified')actions.push(['verify','Проверить в Google Calendar']);
    for(const [action,label] of actions){const button=document.createElement('button');button.type='button';button.textContent=label;
     button.addEventListener('click',async()=>{button.disabled=true;notice.textContent=action==='approve'?'Сохраняем подтверждение…':'Проверяем Google Calendar…';
      try{const result=await calendarPost('/api/google-calendar/plans/'+action,{plan_id:plan.plan_id,payload_hash:plan.hash});
       notice.textContent=result.error?'Не удалось получить результат. Проверьте его ещё раз.':labels[result.status]||'Обновлено.';
       if(result.trace){document.getElementById('calendar-journal').hidden=false;document.getElementById('calendar-trace').textContent=JSON.stringify(result.trace,null,2);}
       await load();
       if(result.status==='verified')document.getElementById('calendar-refresh')?.click();
      }catch{notice.textContent='Не удалось выполнить действие. Обновите календарь и проверьте подключение.';}finally{button.disabled=false;}
     });card.append(button);}
    list.append(card);
   }
   if(!data.items.length)notice.textContent='Новые планы из чата появятся здесь перед добавлением в календарь.';
  }catch{notice.textContent='Не удалось загрузить планы. Обновите страницу.';}finally{loading=false;}
 }
 window.addEventListener('hashchange',load);
 document.getElementById('reload')?.addEventListener('click',load);
 if(visible())load();
})();
'''
