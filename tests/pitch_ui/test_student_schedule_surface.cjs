const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/calendar_plans.py'), 'utf8').split("SCRIPT = r'''")[1].split("'''", 1)[0];
function element(tag = 'div') {
 let text = '';
 return {localName:tag, children:[], hidden:false, dataset:{}, listeners:{}, style:{},
 append(...values){this.children.push(...values);}, replaceChildren(...values){this.children=values;text='';},
 addEventListener(name, callback){this.listeners[name]=callback;},
 get textContent(){return text + this.children.map(child=>typeof child==='string'?child:child.textContent).join('');},
 set textContent(value){text=String(value);this.children=[];}};
}
const client = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/client.py'), 'utf8').replace(/\r\n/g, '\n');
function clientBetween(start, end) {
 const from=client.indexOf(start),to=client.indexOf(end,from);
 assert(from>=0&&to>from,`Missing shipped client boundary: ${start}`);
 return client.slice(from,to);
}
const panel = element(), recoveryActions = [];
const panelContext = {$:()=>panel, act:action=>recoveryActions.push(action)};
vm.createContext(panelContext);
vm.runInContext(clientBetween("  $('panel').addEventListener('click'", "  $('clear-run')?"), panelContext);
panel.listeners.click({target:{closest:()=>({disabled:false,dataset:{action:'compose-schedule'}})}});
assert.deepEqual(recoveryActions,[],'Compose CTA must not dispatch a recovery write action');
const composer = {value:'',dispatchEvent(){},focus(){},setSelectionRange(){}}, clickHandlers = [];
const composerContext = {$:()=>composer, Event, requestAnimationFrame:callback=>callback(),
 document:{body:{dataset:{}},addEventListener:(name,callback)=>clickHandlers.push(callback)},
 location:{hash:'#panel'}, busy:false, chatBusy:false, voiceState:'idle', activeChat:'default',
 setNavigationOpen(){}, chatHash:()=> '#workspace', updateNavigation(){},
 sendMessage(){throw Error('Preparing a draft must not send a message');},
 api(){throw Error('Preparing a draft must not perform a request');}};
vm.createContext(composerContext);
vm.runInContext(clientBetween("  document.addEventListener('click', event => {", "\n  updateNavigation();"),composerContext);
const scheduleEvent={target:{closest:selector=>selector==='[data-action="compose-schedule"]'?{disabled:false,closest:()=>null}:null}};
clickHandlers[0](scheduleEvent);
assert.equal(composerContext.location.hash,'#workspace');assert.match(composer.value,/Я могу учиться/);
composer.value='Мой собственный запрос';clickHandlers[0](scheduleEvent);
assert.equal(composer.value,'Мой собственный запрос','Opening scheduling must preserve an existing unsent draft');
const nodes = new Map(), handlers = new Map(), writes = [];
const plan = {plan_id:'a'.repeat(64), hash:'b'.repeat(64), timezone:'Asia/Almaty', blocks:[
 {title:'Homework',start:'2026-10-22T12:00:00Z',end:'2026-10-22T13:00:00Z'}]};
let state = {status:'draft',operations:[],last_tool_call:null};
const sandbox = {console, Intl, Date, URL, setTimeout, clearTimeout,
 document:{body:{dataset:{view:'plan'}}, getElementById:id=>nodes.get(id)||null, createElement:element},
 window:{addEventListener(name,callback){handlers.set(name,callback);}},
 fetch:async()=>({ok:true,status:200,json:async()=>({items:[{plan,state}]})}),
 calendarPost:async(path,value)=>{writes.push({path,value}); state={...state,status:'approved'};return {status:'approved'};}};
vm.createContext(sandbox); vm.runInContext(source,sandbox);
assert(handlers.has('campuspulse:panel-ready'),'Plans must initialize when the async review panel arrives');
for(const id of ['calendar-plans','calendar-plan-list','calendar-plans-status'])nodes.set(id,element());
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const descend=node=>[node,...node.children.flatMap(value=>typeof value==='string'?[]:descend(value))];
(async()=>{
 await handlers.get('campuspulse:panel-ready')(); await settle();
 const card=nodes.get('calendar-plan-list').children[0];assert(card,'The injected panel loads existing proposals');
 const details=card.children.find(child=>child.localName==='details');
 assert(details&&details.textContent.includes(plan.hash),'Exact approval metadata remains reviewable');
 assert.equal(details.open,undefined,'Technical metadata is collapsed');
 const outside=card.children.filter(child=>child.localName!=='details').map(child=>child.textContent).join(' ');
 assert(!outside.includes(plan.plan_id)&&!outside.includes(plan.hash),'Opaque identifiers stay out of primary copy');
 const approve=card.children.find(child=>child.localName==='button');
 assert.equal(approve.textContent,'Подтвердить расписание');await approve.listeners.click();await settle();
 assert.equal(writes.length,1);assert.equal(writes[0].path,'/api/google-calendar/plans/approve');
 assert.equal(writes[0].value.plan_id,plan.plan_id);assert.equal(writes[0].value.payload_hash,plan.hash);
 const apply=nodes.get('calendar-plan-list').children[0].children.find(child=>child.localName==='button');
 assert.equal(apply.textContent,'Применить в Google Calendar');
 state={status:'unknown',operations:[]}; await handlers.get('campuspulse:panel-ready')();await settle();
 assert(!nodes.get('calendar-plan-list').textContent.includes('Добавлено в Google Calendar'),'Unknown outcomes cannot claim success');
 state={status:'verified',operations:[],verified_at:'2026-10-22T12:00:00Z',
   links:['https://calendar.google.com/calendar/event?eid=synthetic']};
 await handlers.get('campuspulse:panel-ready')();await settle();
 const verified=nodes.get('calendar-plan-list').children[0];
 assert(verified.textContent.includes('Добавлено в Google Calendar'));
 assert(verified.textContent.includes('Проверено:'));
 assert(descend(verified).some(node=>node.localName==='a'),'The fixture includes verified event links');
 sandbox.calendarPost=async()=>({status:'applied_unverified'});
 sandbox.fetch=async()=>{throw Error('Plans refresh unavailable');};
 const verify=verified.children.find(child=>child.localName==='button');
 await verify.listeners.click();await settle();
 const downgraded=nodes.get('calendar-plan-list').children[0];
 assert.equal(downgraded.dataset.status,'applied_unverified','Authoritative verify status survives a failed list refresh');
 assert(!downgraded.textContent.includes('Добавлено в Google Calendar'),'Old success heading is removed');
 assert(!downgraded.textContent.includes('Проверено:'),'Old verified timestamp is removed');
 assert(!descend(downgraded).some(node=>node.localName==='a'),'Old verified links are removed');
 assert(nodes.get('calendar-plans-status').textContent.includes('Не удалось загрузить расписание'));
 sandbox.fetch=async()=>({ok:true,status:200,json:async()=>({items:[{plan,state}]})});
 await handlers.get('campuspulse:panel-ready')();await settle();
 const beforeRace=nodes.get('calendar-plan-list').children[0];
 assert.equal(beforeRace.dataset.status,'verified');
 let releaseOldRead,releaseFreshRead,reads=0;
 sandbox.fetch=()=>{
  reads++;
  return new Promise(resolve=>{
   const release=value=>resolve({ok:true,status:200,json:async()=>({items:[{plan,state:value}]})});
   if(reads===1)releaseOldRead=release;
   else if(reads===2)releaseFreshRead=release;
   else throw Error('Refresh must not loop');
  });
 };
 const oldRead=handlers.get('campuspulse:panel-ready')();await settle();
 await beforeRace.children.find(child=>child.localName==='button').listeners.click();await settle();
 assert.equal(beforeRace.dataset.status,'applied_unverified','POST result downgrades while old list read is in flight');
 releaseOldRead(state);await oldRead;await settle();
 assert.equal(nodes.get('calendar-plan-list').children[0].dataset.status,'applied_unverified',
   'A pre-mutation verified snapshot must not overwrite the authoritative POST result');
 assert.equal(reads,2,'One current list read is queued after the invalidated read settles');
 releaseFreshRead({status:'applied_unverified',operations:[]});await settle();
 assert.equal(nodes.get('calendar-plan-list').children[0].dataset.status,'applied_unverified');
 assert(!nodes.get('calendar-plan-list').textContent.includes('Добавлено в Google Calendar'));
 await settle();assert.equal(reads,2,'Settling the current read must not autoload indefinitely');
 reads=0;
 const replacementRead=handlers.get('campuspulse:panel-ready')();await settle();
 for(const id of ['calendar-plans','calendar-plan-list','calendar-plans-status'])nodes.set(id,element());
 await handlers.get('campuspulse:panel-ready')();
 releaseOldRead(state);await replacementRead;await settle();
 assert.equal(nodes.get('calendar-plan-list').children.length,0,'A previous panel read cannot populate its replacement');
 assert.equal(reads,2,'Replacing an in-flight panel queues one fresh read');
 releaseFreshRead({status:'applied_unverified',operations:[]});await settle();
 assert.equal(nodes.get('calendar-plan-list').children[0].dataset.status,'applied_unverified');
 await settle();assert.equal(reads,2,'Panel replacement does not start a refresh loop');
 plan.purpose='classes';plan.class_timezone='Asia/Almaty';
 plan.blocks=[{title:'Алгоритмы',start:'2026-10-22T14:00:00Z',end:'2026-10-22T15:20:00Z',
  timezone:'Asia/Almaty',valid_until:'2026-11-19',recurrence:['RRULE:FREQ=WEEKLY;UNTIL=20261119T140000Z']}];
 sandbox.fetch=async()=>({ok:true,status:200,json:async()=>({items:[{plan,state:{status:'draft',operations:[]}}]})});
 await handlers.get('campuspulse:panel-ready')();await settle();
 const weekly=nodes.get('calendar-plan-list').textContent;
 assert(weekly.includes('Пары · Проверьте расписание'),'Class publication is distinct from study blocks');
 assert(weekly.includes('Каждую неделю до')&&weekly.includes('19 нояб. 2026'),'Recurrence final date is visible before consent');
 assert(weekly.includes('четверг')&&weekly.includes('19:00')&&weekly.includes('20:20'),'Weekly day and local class time are visible');
 assert(weekly.includes('Asia/Almaty'),'Student timezone is visible for the recurring series');
 console.log('Student Calendar review checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
