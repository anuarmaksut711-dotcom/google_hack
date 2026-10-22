const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const {createDocument} = require('./schedule_dom.cjs');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/manual_schedule.py'), 'utf8');
const script = source.split("CLASSES_SCRIPT = r'''")[1].split("'''", 1)[0];
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for(let i=0;i<12;i++) await tick(); }
const requests = [];
const monday = {subject:'Operating systems',weekday:0,start:'16:00',end:'16:50'};
const tuesday = {subject:'Algorithms',weekday:1,start:'17:00',end:'17:50'};
const draft = (classes, dates={}) => ({classes,valid_from:null,valid_until:null,...dates});
async function fixture(saved=null) {
  const dom=createDocument(), {$}=dom;
  const calls=[], pending=[];
  let session='session-one', next=draft([monday]);
  const response=(status,value)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(value)});
  const fetch=async(url,init={})=> {
    calls.push({url,...init});
    requests.push({url,...init});
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
    if(url==='/api/account/google-calendar')return response(200,{connected:false});
    throw Error('Unexpected URL '+url);
  };
  vm.runInNewContext(script,{document:dom.document,fetch,
    location:{replace(){}},Date,Number,Error,AbortSignal,setTimeout,clearTimeout});
  await settle();
  const fire=(id,event,target)=>$(id).listeners[event]({preventDefault(){},target:target||$(id),type:event});
  const rows=dom.values;
  const upload=async(value)=> {
    next=value;$('classes-file').files=[{type:'image/png',size:200}];fire('classes-file','change');fire('classes-import','click');await settle();
  };
  const dates=()=> { $('classes-from').value='2026-10-22';$('classes-until').value='2026-11-30'; };
  const saves=()=>calls.filter(call=>call.url==='/api/account/classes'&&call.method==='POST');
  const add=()=>{fire('class-add','click');return dom.rows().at(-1);};
  const click=(entry,action)=>dom.fire(entry.querySelector(`[data-action="${action}"]`),'click');
  return {...dom,fire,rows,entries:dom.rows,click,add,upload,dates,saves,calls,changeSession:()=>session='session-two',release:()=>pending.shift()?.()};
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
    const h=await fixture();const subject=h.field(h.add(),'subject');
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
  } else if(selected==='direct_import_save') {
    const h=await fixture();await h.upload(draft([monday]));h.dates();
    h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,1,'valid imported timetable saves directly without checkbox gates');
    await h.upload(draft([tuesday]));
    const subject=h.field(h.entries()[0],'subject');subject.value='Edited class';h.fire('class-schedule','input',subject);
    h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,2,'additional days and edited subjects remain directly saveable');
    assert.deepEqual(JSON.parse(h.saves()[1].body).classes,[{...monday,subject:'Edited class'},tuesday]);
    assert(h.calls.every(call=>!call.url.endsWith('/apply')&&!call.url.endsWith('/approve')),'saving never approves or writes Calendar events');
  } else if(selected==='empty_save') {
    const h=await fixture({valid_from:'2026-10-22',valid_until:'2026-11-30',classes:[monday,tuesday]});
    for(const entry of [...h.entries()]) { h.click(entry,'edit');h.click(entry,'remove'); }
    assert.equal(h.entries().length,0);h.fire('class-schedule','submit');await settle();
    assert.deepEqual(JSON.parse(h.saves()[0].body),{valid_from:'2026-10-22',valid_until:'2026-11-30',classes:[]});
    assert.equal(h.$('classes-calendar').disabled,true,'empty timetable cannot prepare empty Calendar series');
  } else if(selected==='row_limit') {
    const classes=Array.from({length:30},(_,i)=>({subject:'Class '+i,weekday:Math.floor(i/5),start:String(8+i%5*2).padStart(2,'0')+':00',end:String(9+i%5*2).padStart(2,'0')+':00'}));
    const h=await fixture();await h.upload(draft(classes));await h.upload(draft([tuesday]));
    assert.deepEqual(h.rows(),classes,'a day that would exceed 30 classes cannot discard or partially modify the draft');
    assert.equal(h.$('class-add').disabled,true,'row bound counts lessons, not day groups');
  } else if(selected==='grouped_days') {
    const early={...monday,subject:'Mathematics',start:'09:00',end:'10:20'};
    const h=await fixture();await h.upload(draft([tuesday,monday,early]));
    assert.deepEqual(h.rows(),[early,monday,tuesday],'day groups and lessons are ordered by weekday and start time');
    const groups=h.$('classes-rows').querySelectorAll('.classes-day');
    assert.equal(groups.length,2,'two Monday lessons share one group');
    assert.equal(groups[0].dataset.weekday,'0');assert.match(groups[0].textContent,/Понедельник/);
    assert.equal(groups[0].querySelectorAll('[data-class-row]').length,2);
    for(const entry of h.entries()) {
      assert.equal(entry.querySelector('.classes-editor').hidden,true,'imported lessons start compact');
      assert(entry.querySelector('.classes-summary').textContent.includes(h.field(entry,'subject').value));
    }
  } else if(selected==='edit_and_regroup') {
    const h=await fixture();await h.upload(draft([monday,tuesday]));const entry=h.entries()[0];
    h.click(entry,'edit');assert.equal(entry.querySelector('.classes-editor').hidden,false);
    const subject=h.field(entry,'subject');assert.equal(h.document.activeElement,subject);
    subject.value='Updated seminar';h.fire('class-schedule','input',subject);
    const day=h.field(entry,'weekday');day.focus();day.value='1';h.fire('class-schedule','change',day);
    assert.equal(h.document.activeElement,day,'moving a lesson between days preserves the active control');
    h.click(entry,'done');
    assert.equal(h.$('classes-rows').querySelectorAll('.classes-day').length,1,'empty old weekday groups disappear');
    assert.equal(entry.querySelector('.classes-editor').hidden,true);
    assert(entry.querySelector('.classes-summary').textContent.includes('Updated seminar'));
    assert.equal(h.document.activeElement,entry.querySelector('[data-action="edit"]'),'Done restores focus to its edit action');
    h.dates();h.fire('class-schedule','submit');await settle();
    assert.deepEqual(JSON.parse(h.saves()[0].body).classes,[{...monday,subject:'Updated seminar',weekday:1},tuesday]);
  } else if(selected==='add_delete') {
    const h=await fixture();assert.equal(h.entries().length,0,'an empty timetable does not fabricate a lesson');
    const entry=h.add();assert.equal(entry.querySelector('.classes-editor').hidden,false);
    const subject=h.field(entry,'subject');assert.equal(h.document.activeElement,subject);
    subject.value='New lecture';h.fire('class-schedule','input',subject);h.click(entry,'done');
    h.click(entry,'edit');h.click(entry,'remove');assert.equal(h.entries().length,0);assert.equal(h.document.activeElement,h.$('class-add'));
  } else if(selected==='invalid_editor') {
    const h=await fixture();await h.upload(draft([monday]));const entry=h.entries()[0];
    h.field(entry,'subject').value='';h.dates();h.fire('class-schedule','submit');await settle();
    assert.equal(h.saves().length,0);assert.equal(entry.querySelector('.classes-editor').hidden,false,'invalid collapsed lesson opens for correction');
    assert.equal(h.document.activeElement,h.field(entry,'subject'));
  } else if(selected==='overlap') {
    const h=await fixture();await h.upload(draft([monday,{...monday,subject:'Conflict',start:'16:30',end:'17:20'}]));
    h.dates();h.fire('class-schedule','submit');await settle();assert.equal(h.saves().length,0);
    assert.match(h.$('classes-status').textContent,/пересекаются/);
  } else throw Error('Unknown case '+selected);
  for(const request of requests.filter(call=>call.method==='POST')) {
    assert(['/api/account/classes/import','/api/account/classes'].includes(request.url),
      `Disconnected timetable editing cannot write or approve Calendar events: ${request.url}`);
    assert.equal(request.headers['X-CSRF-Token'],'synthetic_csrf_token_0123456789');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
