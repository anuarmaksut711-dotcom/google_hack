const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/manual_schedule.py'), 'utf8');
const script = source.split("CLASSES_SCRIPT = r'''")[1].split("'''", 1)[0];
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for(let i=0;i<12;i++) await tick(); }
class Element {
  constructor(tag='div') { Object.assign(this,{tagName:tag,children:[],listeners:{},dataset:{},value:'',disabled:false,hidden:false,checked:false,files:[],textContent:''}); }
  append(...items) { for(const item of items) { item.parent=this;this.children.push(item); } }
  replaceChildren(...items) { this.children=[];this.append(...items); }
  addEventListener(type, fn) { this.listeners[type]=fn; }
  setAttribute(name, value) { this[name]=value; }
  focus() { this.focused=true; }
  remove() { this.parent.children=this.parent.children.filter(item=>item!==this); }
  querySelectorAll(selector) { return this.children.flatMap(child=>[child,...child.querySelectorAll(selector)]).filter(child=>child.dataset.field); }
}
const monday = {subject:'Operating systems',weekday:0,start:'16:00',end:'16:50'};
const tuesday = {subject:'Algorithms',weekday:1,start:'17:00',end:'17:50'};
const draft = (classes, dates={}) => ({classes,valid_from:null,valid_until:null,...dates});
async function fixture(saved=null, legacyMarkup=false) {
  const nodes=new Map(), $=id=> { if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id); };
  const calls=[], pending=[];
  let session='session-one', next=draft([monday]);
  const response=(status,value)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(value)});
  const fetch=async(url,init={})=> {
    calls.push({url,...init});
    if(url==='/auth/session')return response(200,{status:'authenticated',session_id:session,csrf_token:'synthetic_csrf_token_0123456789'});
    if(url==='/api/account/classes/import') {
      const value=next;
      if(value?.hold)await new Promise(resolve=>pending.push(resolve));
      if(value?.fail)throw Error('synthetic transport failure');
      return response(200,{draft:value});
    }
    if(url==='/api/account/classes') {
      if(init.method==='POST') {saved=JSON.parse(init.body);return response(200,{connected:true,schedule:saved});}
      return response(200,{connected:Boolean(saved),schedule:saved});
    }
    throw Error('Unexpected URL '+url);
  };
  vm.runInNewContext(script,{document:{getElementById:id=>legacyMarkup&&id==='classes-week-complete'?null:$(id),createElement:tag=>new Element(tag)},fetch,
    location:{replace(){}},Date,Number,Error,AbortSignal,setTimeout,clearTimeout});
  await settle();
  const fire=(id,event,target)=>$(id).listeners[event]({preventDefault(){},target:target||$(id)});
  const rows=()=>Array.from($('classes-rows').children,field=>Object.fromEntries(field.querySelectorAll('[data-field]').map(input=>[
    input.dataset.field,input.dataset.field==='weekday'?Number(input.value):input.value])));
  const upload=async(value)=> {
    next=value;$('classes-file').files=[{type:'image/png',size:200}];fire('classes-file','change');fire('classes-import','click');await settle();
  };
  const dates=()=> { $('classes-from').value='2026-10-22';$('classes-until').value='2026-11-30'; };
  const saves=()=>calls.filter(call=>call.url==='/api/account/classes'&&call.method==='POST');
  return {$,fire,rows,upload,dates,saves,calls,changeSession:()=>session='session-two',release:()=>pending.shift()?.()};
}
(async()=> {
  const selected=process.argv[3];
  if(selected==='append_days') {
    const h=await fixture();await h.upload(draft([monday]));await h.upload(draft([tuesday]));
    assert.deepEqual(h.rows(),[monday,tuesday],'a second day must append to the editable week');
    assert.equal(h.saves().length,0,'recognition cannot save a partial week');
    assert.equal(h.$('classes-from').value,'','no dates may be fabricated');
    assert.equal(h.$('classes-until').value,'');
  } else if(selected==='preserve_dates') {
    const h=await fixture();h.dates();await h.upload(draft([monday],{valid_from:'2026-01-01',valid_until:'2026-01-31'}));
    assert.equal(h.$('classes-from').value,'2026-10-22','extracted dates cannot overwrite entered dates');
    assert.equal(h.$('classes-until').value,'2026-11-30');
    h.$('classes-until').value='';await h.upload(draft([tuesday],{valid_from:'2026-10-01',valid_until:'2026-11-15'}));
    assert.equal(h.$('classes-from').value,'2026-10-22');
    assert.equal(h.$('classes-until').value,'2026-11-15','explicit dates may fill an unset field');
  } else if(selected==='repeat_file') {
    const h=await fixture();await h.upload(draft([monday]));await h.upload(draft([monday]));
    assert.deepEqual(h.rows(),[monday],'repeated recognition must not duplicate a class');
  } else if(selected==='saved_rows') {
    const h=await fixture({valid_from:'2026-10-22',valid_until:'2026-11-30',classes:[monday]});
    await h.upload(draft([tuesday]));assert.deepEqual(h.rows(),[monday,tuesday],'saved classes stay in the draft');
    assert.equal(h.saves().length,0,'the saved weekly schedule is unchanged until Save');
  } else if(selected==='manual_rows') {
    const h=await fixture();const subject=h.$('classes-rows').children[0].querySelectorAll('[data-field]').find(c=>c.dataset.field==='subject');
    subject.value='My seminar';h.fire('class-schedule','input',subject);await h.upload(draft([monday]));
    assert.deepEqual(h.rows(),[{subject:'My seminar',weekday:0,start:'09:00',end:'10:20'},monday]);
  } else if(selected==='failed_next_day'||selected==='malformed_next_day') {
    const values=selected==='failed_next_day'?[{fail:true}]:[
      null,draft([null]),draft([{...tuesday,weekday:'1'}]),draft([{...tuesday,subject:23}]),
      draft([{...tuesday,end:'not-time'}]),draft([tuesday],{valid_from:'2026-02-30'}),
      draft([{...tuesday,end:'16:00'}])];
    const h=await fixture();await h.upload(draft([monday]));h.dates();
    for(const value of values) {
      await h.upload(value);assert.deepEqual(h.rows(),[monday],'an unusable later image preserves the current draft');
      assert.equal(h.$('classes-from').value,'2026-10-22');assert.equal(h.$('classes-until').value,'2026-11-30');
      assert.equal(h.$('classes-fields').disabled,false);assert.equal(h.saves().length,0);
    }
  } else if(selected==='session_changes') {
    const h=await fixture();await h.upload(draft([monday]));h.dates();
    await h.upload({...draft([tuesday]),hold:true});h.changeSession();h.release();await settle();
    assert.deepEqual(h.rows(),[monday]);assert.equal(h.$('classes-from').value,'2026-10-22');
    assert.equal(h.$('classes-fields').disabled,true);h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,0);
  } else if(selected==='whole_week_confirmation') {
    const h=await fixture();await h.upload(draft([monday]));h.dates();h.$('classes-reviewed').checked=true;
    h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,0,'review of one image does not confirm full-week coverage');
    h.$('classes-week-complete').checked=true;h.fire('class-schedule','change',h.$('classes-week-complete'));
    assert.equal(h.$('classes-reviewed').checked,true,'confirmations must not reset each other');
    h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,1);
    await h.upload(draft([tuesday]));assert.equal(h.$('classes-week-complete').checked,false,'a new image requires a fresh whole-week decision');
    h.$('classes-reviewed').checked=true;h.$('classes-week-complete').checked=true;
    const subject=h.$('classes-rows').children[0].querySelectorAll('[data-field]').find(c=>c.dataset.field==='subject');
    subject.value='Edited class';h.fire('class-schedule','input',subject);
    assert.equal(h.$('classes-week-complete').checked,false,'editing the draft invalidates full-week confirmation');
  } else if(selected==='legacy_markup') {
    const h=await fixture(null,true);h.dates();
    h.$('classes-rows').children[0].querySelectorAll('[data-field]').find(c=>c.dataset.field==='subject').value='Manual class';
    h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,1,'manual entry remains usable when older markup omits import confirmation');
    await h.upload(draft([monday]));h.$('classes-reviewed').checked=true;
    h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,1,'import saving fails closed without the full-week control');
  } else if(selected==='row_limit') {
    const classes=Array.from({length:30},(_,i)=>({subject:'Class '+i,weekday:Math.floor(i/5),start:String(8+i%5*2).padStart(2,'0')+':00',end:String(9+i%5*2).padStart(2,'0')+':00'}));
    const h=await fixture();await h.upload(draft(classes));await h.upload(draft([tuesday]));
    assert.deepEqual(h.rows(),classes,'a day that would exceed 30 classes cannot discard or partially modify the draft');
  } else throw Error('Unknown case '+selected);
})().catch(error=>{console.error(error);process.exitCode=1;});
