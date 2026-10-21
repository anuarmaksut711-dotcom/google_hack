const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const src = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/client.py'), 'utf8');
const between = (a,b) => src.slice(src.indexOf(a),src.indexOf(b,src.indexOf(a)));
function harness() {
  const nodes = new Map(), saved = new Map(), calls = [];
  function element() {return {children:[], dataset:{}, textContent:'', hidden:false, disabled:false,
    append(...v){this.children.push(...v);},replaceChildren(...v){this.children=v;},
    setAttribute(k,v){if(k==='data-stale')this.dataset.stale=v;},querySelector(){return null;},classList:{remove(){}}};}
  const $ = id => {if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  $('panel').children=['exact saved version'];
  const sandbox = {$, document:{createElement:element}, console, URLSearchParams,
    crypto:{randomUUID:()=> 'stable'}, location:{hash:'#panel'}, history:{replaceState(){}},
    stored(n,v){if(v!==undefined)saved.set(n,v);return saved.get(n)||null;},
    keyFor:()=> 'stable-action-key', reference:()=>({plan_id:'plan',version:1,payload_hash:'hash'}),
    controls(){},feedback(message){sandbox.message=message;},focusPanel(){},
    refresh:async()=>{},api:async(p,v)=>{calls.push([p,v]);throw Error('network lost');}};
  vm.createContext(sandbox);
  vm.runInContext(`let busy=false,chatBusy=false,activeRun='run-one',traceBusy=false,cursor=0,generation=0;
    ${between('  async function act(', "  $('start').addEventListener")}`,sandbox);
  return {$,sandbox,calls,run:c=>vm.runInContext(c,sandbox)};
}
(async()=>{
  const h=harness();await h.run("act('apply')");
  assert.deepEqual(h.$('panel').children,['exact saved version'],'Lost response must retain exact plan for review');
  assert.equal(h.$('panel').dataset.stale,'true','Old controls must stay blocked until saved state is read');
  assert.match(h.sandbox.message,/неизвест|подтвержд/);
  const t=harness();
  t.sandbox.api=async()=>({json:async()=>({records:[{observation:{event:{tool:'read_assignments',phase:'result',status:'complete',sequence:1},layer:'domain_adapter',summary:{item_count:2,complete:true}}}],next:1})});
  await t.run('trace(true)');
  const text=JSON.stringify(t.$('trace-records').children);
  assert.match(text,/Читает задания/,'Tool purpose must be visible');
  assert.match(text,/Записей: 2/,'Actual returned count must be visible');
  assert.match(text,/complete/,'Original status must remain inspectable');
  console.log('pitch client: lost response and factual trace passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

// Re-run the shipped refresh function with an authenticated session and a failed
// state read. The session and exact selection must survive a server outage.
(async()=>{
  const h=harness();
  Object.assign(h.sandbox, {
    chats:{reset(){},load:async()=>{}}, clearDrafts(){}, restoreDraft(){},
    chatDrafts:new Map(), loadChat:async()=>{}, loadMoodle(){}, updateNavigation(){},
    api:async p=>{
      if(p==='/auth/session')return {json:async()=>({session_id:'session-one'})};
      throw Error('state unavailable');
    }
  });
  h.sandbox.location.search='?run_id=run-one';
  vm.runInContext(`let scope='session-one',pendingText=null,activeChat='default',chatAvailable=true,
    chatWorking=false,chatLimitReached=false,chatGeneration=0;
    ${between('  async function refresh()', '  async function act(')}`,h.sandbox);
  await assert.rejects(h.run('refresh()'),/state unavailable/);
  assert.equal(h.run('scope'),'session-one','A state-read outage must not sign the user out');
  assert.equal(h.run('activeRun'),'run-one');
  assert.equal(h.$('panel').dataset.stale,'true');
  assert.deepEqual(h.$('panel').children,['exact saved version']);
  console.log('pitch client: transient state read preserves authenticated selection');
})().catch(e=>{console.error(e);process.exitCode=1;});
