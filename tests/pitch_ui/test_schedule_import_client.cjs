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
  querySelectorAll(selector) { const all=this.children.flatMap(child=>[child,...child.querySelectorAll(selector)]);return all.filter(child=>child.dataset.field); }
}
async function fixture(options={}) {
 const nodes=new Map(), $=id=> { if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id); };
 for(const id of ['class-schedule','classes-fields','classes-rows','classes-status','classes-from','classes-until',
   'class-add','classes-file','classes-import','classes-review','classes-reviewed','classes-reload'])$(id);
 const draftId='a'.repeat(64), calls=[], location={hash:options.chatDraft?'#draft='+draftId:'',replace(value){this.redirect=value;}};
 let imported=false, holdImport=null, holdSave=null, session='session-one', saved=false;
 const draft={classes:[{subject:'<img onerror=secret>',weekday:1,start:'12:00',end:'14:00'}],valid_from:null,valid_until:null};
 const response=(status,value)=>({status,ok:status>=200&&status<300,json:async()=>structuredClone(value)});
 const fetch=async(url,init={})=> {
   calls.push({url,...init});
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
   if(url==='/api/google-calendar/plans/classes')return response(200,{status:'draft'});
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
 const document={cookie:'',getElementById:$,createElement:tag=>new Element(tag)};
 vm.runInNewContext(script,{document,fetch,location,Date,Number,Error,AbortSignal,setTimeout,clearTimeout});
 await settle();
 const fire=(id,event,target)=> {assert.equal(typeof $(id).listeners[event],'function',id+' '+event+' listener');return $(id).listeners[event]({preventDefault(){},target:target||$(id)});};
 const file=(size=200)=> {const value={type:'application/pdf',size};$('classes-file').files=[value];fire('classes-file','change');return value;};
 return {$,calls,fire,file,location,setSession:()=>session='session-two',releaseImport:()=>holdImport?.(),releaseSave:()=>holdSave?.()};
}
(async()=> {
 const selected=process.argv[3],posts=h=>h.calls.filter(c=>c.method==='POST'),imports=h=>posts(h).filter(c=>c.url.endsWith('/import'));
 if(selected==='explicit_import') {
   const h=await fixture(),file=h.file();await settle();assert.equal(posts(h).length,0,'file selection must not send data');
   h.fire('classes-import','click');await settle();assert.equal(imports(h).length,1);assert.equal(posts(h).length,1,'recognition cannot save');
   assert.equal(imports(h)[0].body,file);assert.equal(imports(h)[0].headers['Content-Type'],'application/pdf');
   assert.equal(imports(h)[0].headers['X-CSRF-Token'],'synthetic_csrf_token_0123456789');
   assert.equal(h.$('classes-review').hidden,false);assert.equal(h.$('classes-reviewed').checked,false);
   const subject=h.$('classes-rows').children[0].querySelectorAll('[data-field]').find(e=>e.dataset.field==='subject');
   assert.equal(subject.value,'<img onerror=secret>','provider text is an input value, never HTML');
 } else if(selected==='review_dates_save') {
   const h=await fixture();h.file();h.fire('classes-import','click');await settle();
   h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1,'blank extraction dates cannot save');
   h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1,'unreviewed extraction cannot save');
   h.$('classes-reviewed').checked=true;h.$('classes-week-complete').checked=true;h.fire('class-schedule','submit');await settle();
   assert.equal(posts(h).length,2);assert.equal(posts(h)[1].url,'/api/account/classes');
   assert.deepEqual(JSON.parse(posts(h)[1].body),{valid_from:'2026-10-19',valid_until:'2026-11-01',classes:[{subject:'<img onerror=secret>',weekday:1,start:'12:00',end:'14:00'}]});
 } else if(selected==='chat_draft') {
   const h=await fixture({chatDraft:true});
   assert.equal(h.$('classes-rows').children.length,1);
   assert.equal(h.$('classes-rows').children[0].querySelectorAll('[data-field]').find(e=>e.dataset.field==='subject').value,'<img onerror=secret>');
   assert.equal(h.$('classes-from').value,'');assert.equal(h.$('classes-until').value,'');
   assert.equal(h.$('classes-review').hidden,false);assert.equal(posts(h).length,0);
   assert.equal(h.$('classes-calendar').disabled,true,'unreviewed chat draft cannot be published');
 } else if(selected==='chat_draft_session_change') {
   const h=await fixture({chatDraft:true,holdDraft:true});h.setSession();h.releaseImport();await settle();
   assert.equal(h.$('classes-fields').disabled,true);
   assert.equal(h.$('classes-rows').children.length,0,'foreign session draft is not rendered');
 } else if(selected==='calendar_after_save') {
   const h=await fixture({chatDraft:true});
   h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.$('classes-reviewed').checked=true;h.$('classes-week-complete').checked=true;
   h.fire('class-schedule','submit');await settle();
   assert.equal(h.$('classes-calendar').disabled,false);assert.equal(posts(h).length,1,'saving does not publish');
   h.fire('classes-calendar','click');await settle();
   assert.equal(posts(h).length,2);assert.equal(posts(h)[1].url,'/api/google-calendar/plans/classes');
   assert.deepEqual(JSON.parse(posts(h)[1].body),{});
   assert.equal(h.$('classes-calendar-review').hidden,false);
 } else if(selected==='manual') {
   const h=await fixture();h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   const controls=h.$('classes-rows').children[0].querySelectorAll('[data-field]');controls.find(e=>e.dataset.field==='subject').value='Manual class';
   h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1);assert.equal(imports(h).length,0);
 } else if(selected==='failure') {
   const h=await fixture({failure:true}),first=h.$('classes-rows').children[0];h.file();h.fire('classes-import','click');await settle();
   assert.equal(h.$('classes-rows').children[0],first,'failed extraction preserves editable manual rows');assert.equal(h.$('classes-fields').disabled,false);
   assert.match(h.$('classes-status').textContent,/вручную|вручн/);
 } else if(selected==='duplicate_import') {
   const h=await fixture({holdImport:true});h.file();h.fire('classes-import','click');await settle();h.fire('classes-import','click');await settle();
   assert.equal(imports(h).length,1);assert.equal(h.$('classes-fields').disabled,true);h.releaseImport();await settle();
   assert.equal(h.$('classes-fields').disabled,false);
 } else if(selected==='new_file') {
   const h=await fixture();h.file();h.fire('classes-import','click');await settle();h.$('classes-reviewed').checked=true;h.file();
   assert.equal(h.$('classes-reviewed').checked,false,'a new source resets review');
 } else if(selected==='session_change') {
   const h=await fixture({holdImport:true}),first=h.$('classes-rows').children[0];h.file();h.fire('classes-import','click');await settle();
   h.setSession();h.releaseImport();await settle();assert.equal(h.$('classes-rows').children[0],first,'late result cannot enter another session');
   assert.equal(h.$('classes-fields').disabled,true);h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1);
 } else if(selected==='double_save'||selected==='uncertain_save') {
   const h=await fixture({holdSave:selected==='double_save',uncertain:selected==='uncertain_save'});
   h.$('classes-from').value='2026-10-19';h.$('classes-until').value='2026-11-01';
   h.$('classes-rows').children[0].querySelectorAll('[data-field]').find(e=>e.dataset.field==='subject').value='Manual class';
   h.fire('class-schedule','submit');await settle();h.fire('class-schedule','submit');await settle();assert.equal(posts(h).length,1);
   assert.equal(h.$('classes-fields').disabled,true);if(selected==='double_save'){h.releaseSave();await settle();assert.equal(h.$('classes-fields').disabled,false);}
 } else if(selected==='local_size') {
   const h=await fixture();h.file(8*1024*1024+1);h.fire('classes-import','click');await settle();assert.equal(posts(h).length,0);
 } else throw Error('Unknown case '+selected);
})().catch(error=>{console.error(error);process.exitCode=1;});
