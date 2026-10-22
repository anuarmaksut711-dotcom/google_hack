const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const {createDocument} = require('./schedule_dom.cjs');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/manual_schedule.py'), 'utf8');
const script = source.split("CLASSES_SCRIPT = r'''")[1].split("'''", 1)[0];
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for(let i=0;i<12;i++) await tick(); }
const requests = [];
async function fixture(options={}) {
 const dom=createDocument(), {$,document}=dom;
 for(const id of ['class-schedule','classes-fields','classes-rows','classes-status','classes-from','classes-until',
   'class-add','classes-file','classes-import','classes-reload'])$(id);
 const draftId='a'.repeat(64), calls=[], location={hash:options.chatDraft?'#draft='+draftId:'',replace(value){this.redirect=value;}};
 let imported=false, holdImport=null, holdSave=null, session='session-one', saved=false;
 const draft={classes:[{subject:'<img onerror=secret>',weekday:1,start:'12:00',end:'14:00'}],valid_from:null,valid_until:null};
 const response=(status,value)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(value)});
 const fetch=async(url,init={})=> {
   calls.push({url,...init});
   requests.push({url,...init});
   if(url==='/auth/session')return response(200,{status:'authenticated',session_id:session,csrf_token:'synthetic_csrf_token_0123456789'});
   if(url==='/api/account/classes/import') {
     if(options.holdImport)await new Promise(resolve=>holdImport=resolve);
     imported=true;
     return response(options.failure?422:200,options.failure?{error:'schedule_import_unreadable'}:{draft,warning:'Review required'});
   }
   if(url==='/api/account/classes/drafts/'+draftId) {
     if(options.holdDraft)await new Promise(resolve=>holdImport=resolve);
     return response(200,{draft,warning:'Review required'});
   }
   if(url==='/api/account/google-calendar')return response(200,{connected:true,can_write:true});
   if(url==='/api/google-calendar/plans/classes')return response(options.calendarFailure?503:200,{status:'draft'});
   if(url==='/api/account/classes') {
     if(init.method==='POST') {
       if(options.holdSave)await new Promise(resolve=>holdSave=resolve);
       if(options.uncertain)throw Error('lost response');
       saved=true;return response(200,{connected:true,schedule:JSON.parse(init.body)});
     }
     return response(200,{connected:saved,schedule:null});
   }
   throw Error('Unexpected URL '+url);
 };
 vm.runInNewContext(script,{document,fetch,location,Date,Number,Error,AbortSignal,setTimeout,clearTimeout});
 await settle();
 const fire=(id,event,target)=> {assert.equal(typeof $(id).listeners[event],'function',id+' '+event+' listener');return $(id).listeners[event]({preventDefault(){},target:target||$(id),type:event});};
 const file=(size=200)=> {const value={type:'application/pdf',size};$('classes-file').files=[value];fire('classes-file','change');return value;};
 const add=()=>{fire('class-add','click');return dom.rows().at(-1);};
 return {...dom,calls,fire,file,add,location,setSession:()=>session='session-two',releaseImport:()=>holdImport?.(),releaseSave:()=>holdSave?.()};
}
(async()=> {
 const selected=process.argv[3],posts=h=>h.calls.filter(c=>c.method==='POST'),imports=h=>posts(h).filter(c=>c.url.endsWith('/import'));
 if(selected==='explicit_import') {
   const h=await fixture(),file=h.file();await settle();assert.equal(posts(h).length,0,'file selection must not send data');
   h.fire('classes-import','click');await settle();assert.equal(imports(h).length,1);assert.equal(posts(h).length,1,'recognition cannot save');
   assert.equal(imports(h)[0].body,file);assert.equal(imports(h)[0].headers['Content-Type'],'application/pdf');
   assert.equal(imports(h)[0].headers['X-CSRF-Token'],'synthetic_csrf_token_0123456789');
   const subject=h.field(h.rows()[0],'subject');
   assert.equal(subject.value,'<img onerror=secret>','provider text is an input value, never HTML');
   assert(h.rows()[0].querySelector('.classes-summary').textContent.includes('<img onerror=secret>'),
     'provider text is rendered as literal summary text');
   assert.equal(h.rows()[0].querySelectorAll('img').length,0);
 } else if(selected==='review_dates_save') {
   const h=await fixture();h.file();h.fire('classes-import','click');await settle();
   h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1,'blank extraction dates cannot save');
   h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.fire('class-schedule','submit');await settle();
   assert.equal(posts(h).length,3);assert.equal(posts(h)[1].url,'/api/account/classes');
   assert.deepEqual(JSON.parse(posts(h)[1].body),{valid_from:'2026-10-19',valid_until:'2026-11-01',classes:[{subject:'<img onerror=secret>',weekday:1,start:'12:00',end:'14:00'}]});
 } else if(selected==='chat_draft') {
   const h=await fixture({chatDraft:true});
   assert.equal(h.rows().length,1);
   assert.equal(h.field(h.rows()[0],'subject').value,'<img onerror=secret>');
   assert.equal(h.$('classes-from').value,'');assert.equal(h.$('classes-until').value,'');
   assert.equal(posts(h).length,0);
   assert.equal(h.$('classes-calendar').disabled,true,'unsaved chat draft cannot prepare Calendar');
 } else if(selected==='chat_draft_session_change') {
   const h=await fixture({chatDraft:true,holdDraft:true});h.setSession();h.releaseImport();await settle();
   assert.equal(h.$('classes-fields').disabled,true);
   assert.equal(h.rows().length,0,'foreign session draft is not rendered');
 } else if(selected==='calendar_after_save') {
   const h=await fixture({chatDraft:true});
   h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.fire('class-schedule','submit');await settle();
   assert.equal(h.$('classes-calendar').disabled,false);
   assert.equal(posts(h).length,2,'saving prepares review without publishing');
   assert.equal(posts(h)[1].url,'/api/google-calendar/plans/classes');
   assert.deepEqual(JSON.parse(posts(h)[1].body),{});
   assert.equal(h.$('classes-calendar-review').hidden,false);
 } else if(selected==='manual') {
   const h=await fixture();h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.field(h.add(),'subject').value='Manual class';
   h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,2);assert.equal(imports(h).length,0);
 } else if(selected==='calendar_failure_keeps_saved_week') {
   const h=await fixture({calendarFailure:true});h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.field(h.add(),'subject').value='Manual class';
   h.fire('class-schedule','submit');await settle();
   assert.equal(posts(h).length,2);assert.equal(h.$('classes-calendar').disabled,false);
   assert.match(h.$('classes-status').textContent,/сохранено на сайте/);
   assert.equal(h.$('classes-calendar-review').hidden,false);
   h.fire('classes-calendar','click');await settle();
   assert.equal(posts(h).length,3,'retry only prepares Calendar, it never saves the week again');
 } else if(selected==='failure') {
   const h=await fixture({failure:true}),first=h.add();h.field(first,'subject').value='My existing class';h.file();h.fire('classes-import','click');await settle();
   assert.equal(h.rows()[0],first,'failed extraction preserves editable manual rows');assert.equal(h.$('classes-fields').disabled,false);
   assert.match(h.$('classes-status').textContent,/вручную|вручн/);
 } else if(selected==='duplicate_import') {
   const h=await fixture({holdImport:true});h.file();h.fire('classes-import','click');await settle();h.fire('classes-import','click');await settle();
   assert.equal(imports(h).length,1);assert.equal(h.$('classes-fields').disabled,true);h.releaseImport();await settle();
   assert.equal(h.$('classes-fields').disabled,false);
 } else if(selected==='new_file') {
   const h=await fixture();h.file();h.fire('classes-import','click');await settle();const before=h.values();h.file();
   assert.deepEqual(h.values(),before,'selecting another source preserves the current unsaved draft');
   assert.equal(posts(h).length,1,'a newly selected file is not automatically sent');
 } else if(selected==='session_change') {
   const h=await fixture({holdImport:true}),first=h.add();h.file();h.fire('classes-import','click');await settle();
   h.setSession();h.releaseImport();await settle();assert.equal(h.rows()[0],first,'late result cannot enter another session');
   assert.equal(h.$('classes-fields').disabled,true);h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1);
 } else if(selected==='double_save'||selected==='uncertain_save') {
   const h=await fixture({holdSave:selected==='double_save',uncertain:selected==='uncertain_save'});
   h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.field(h.add(),'subject').value='Manual class';
   h.fire('class-schedule','submit');await settle();h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1);
   assert.equal(h.$('classes-fields').disabled,true);if(selected==='double_save'){h.releaseSave();await settle();assert.equal(h.$('classes-fields').disabled,false);}
 } else if(selected==='local_size') {
   const h=await fixture();h.file(8*1024*1024+1);h.fire('classes-import','click');await settle();assert.equal(posts(h).length,0);
 } else if(selected==='save_focus') {
   const h=await fixture({holdSave:true});h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.field(h.add(),'subject').value='Manual class';
   const save=h.$('classes-save');save.focus();assert.equal(h.document.activeElement,save);
   h.fire('class-schedule','submit');await settle();
   assert.equal(h.document.activeElement,h.document.body,'disabling the fieldset drops native Save focus');
   assert.equal(posts(h).length,1);h.releaseSave();await settle();
   assert.equal(h.document.activeElement,save,'completed Save restores its initiating control after reenabling the form');
 } else if(selected==='import_focus_move') {
   for(const leaveSidebar of [false,true]) {
     const h=await fixture({holdImport:true});h.file();h.$('classes-import').focus();h.fire('classes-import','click');await settle();
     assert.equal(h.document.activeElement,h.document.body,'recognition disables its initiating control');
     const sidebar=h.$('sidebar-link');sidebar.focus();assert.equal(h.document.activeElement,sidebar);
     if(leaveSidebar)h.document.body.focus();
     h.releaseImport();await settle();
     assert.equal(h.rows().length,1,'recognition still populates the draft');
     assert.equal(h.document.activeElement,leaveSidebar?h.document.body:sidebar,
       'a completed import must not steal focus after the user leaves the form, even if focus later moves to body');
   }
 } else throw Error('Unknown case '+selected);
 for(const request of requests.filter(call=>call.method==='POST')) {
   assert(['/api/account/classes/import','/api/account/classes','/api/google-calendar/plans/classes'].includes(request.url),
     `The timetable editor may only recognize, save locally or prepare a proposal: ${request.url}`);
   assert.equal(request.headers['X-CSRF-Token'],'synthetic_csrf_token_0123456789','every mutation uses the fresh session CSRF token');
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
