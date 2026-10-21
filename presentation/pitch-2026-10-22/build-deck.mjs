import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const DIR = import.meta.dirname;
const BASE_NAME = process.argv[2] || 'CampusPulse-AI-Pitch-2026-10-22-final';
const FULL_TEST_RECEIPT = process.env.PITCH_TEST_RECEIPT || 'Локальный полный pytest: 2472 passed, 0 skipped, 171.44 s. После трёх небольших copy-правок прошли 27 целевых тестов. Отдельно recovery 347 и voice 53. Точный Cloud Build gate новой версии ожидается.';
const stagingDir = path.join(DIR,'.build',`${BASE_NAME}-${Date.now()}`);
const finalizedDir = path.join(stagingDir,'finalized');
const runtime = path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies');
const SKILL = process.env.PRESENTATIONS_SKILL_DIR || path.join(os.homedir(),'.codex/plugins/cache/openai-primary-runtime/presentations/26.1020.11348/skills/presentations');
const PYTHON = process.env.RUNTIME_PYTHON || path.join(runtime,'python/bin/python3');
process.env.RUNTIME_NODE_MODULES ||= path.join(runtime,'node/node_modules');
const requireRuntime = createRequire(path.join(process.env.RUNTIME_NODE_MODULES,'package.json'));
const { Presentation, PresentationFile } = await import(pathToFileURL(requireRuntime.resolve('@oai/artifact-tool')));
const { GlobalFonts } = requireRuntime('@napi-rs/canvas');
await fs.mkdir(path.join(DIR,'.build'),{recursive:true});
await fs.mkdir(path.join(DIR,'output'),{recursive:true});
await fs.mkdir(finalizedDir,{recursive:true});
const { finalizePresentation, resolvePresentationFont } = await import(pathToFileURL(path.join(SKILL, 'container_tools/artifact_tool_utils.mjs')));
const availableFonts = GlobalFonts.families.map(x => x.family);
const font = resolvePresentationFont({ availableFonts });
console.log(`Font selected from installed inventory: ${font}`);

const C = { ink:'#132238', muted:'#586778', paper:'#FAFAF7', blue:'#4056BD', teal:'#087F8C', light:'#EAF1F3', rule:'#CED7DC', amber:'#A66A12', green:'#247455', white:'#FFFFFF' };
const p = Presentation.create({slideSize:{width:1280,height:720}});
const source = 'Интеграция 22.10.2026: origin/main d8c6054 и UI 15bb5fd, рабочая ветка codex/site-integration-2026-10-22. Источник облачного read-only релиза: setup/PITCH-CLOUD-HANDOFF.md, source 1382f49aaba7c37c6571bbd8355349064e039012. Он фиксирует реальный вход, 15 Moodle-записей и вызовы read_assignments/read_schedule. Новые Calendar/OCR изменения локально проверены, их облачная приёмка ещё не завершена. Материалы не содержат личных данных или credentials.';
const boundary = 'Managed study Agent Engine имеет только инструменты чтения. Backend проверяет типизированный AdviceReply и текущие сохранённые пары, затем ConnectedPlans создаёт предложение. Явное согласие относится к plan_id и payload_hash. Backend вызывает отдельные приватные MCP apply_plan и verify_plan. Классы, Calendar и сессия проверяются на актуальность. Распознавание PDF/PNG/JPEG создаёт только черновик, видимые дни добавляются с удалением точных дублей, даты не выводятся по догадке. Студент подтверждает всю неделю и даты перед сохранением. Пары автоматически в Calendar не переносятся.';
function notes(s, specific) { s.speakerNotes.text = `${specific} ${boundary} ${source} ${FULL_TEST_RECEIPT}`; }


function text(s, value, x,y,w,h,size=26,color=C.ink,bold=false,align='left') {
  const a=s.shapes.add({geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
  a.text=value;
  a.text.style={typeface:font,fontSize:size,color,bold,alignment:align,verticalAlignment:'top',wrap:'square',autoFit:'none',insets:0};
  return a;
}
function rule(s,x,y,w,color=C.rule,width=1) {return s.shapes.add({geometry:'line',position:{left:x,top:y,width:w,height:0},line:{fill:color,width},fill:'none'});}
function slide(title,n,sub='') {
  const s=p.slides.add(); s.background.fill=C.paper;
  text(s,'CampusPulse AI',64,36,900,30,22,C.blue,true);
  text(s,String(n).padStart(2,'0'),1136,36,80,30,20,C.muted,false,'right');
  text(s,title,64,100,1152,112,46,C.ink,true);
  if(sub) text(s,sub,64,214,1152,65,25,C.muted);
  rule(s,64,662,1152);
  text(s,'Защита проекта, 22 октября 2026',64,675,1000,25,17,C.muted);
  return s;
}
function node(s,label,x,y,w,h,color=C.ink,fill=C.white,size=25) {
  const a=s.shapes.add({geometry:'rect',position:{left:x,top:y,width:w,height:h},fill,line:{fill:color,width:1.4}});
  a.text=label;a.text.style={typeface:font,fontSize:size,color,bold:true,alignment:'center',verticalAlignment:'middle',autoFit:'none',wrap:'square',insets:10}; return a;
}
function arrow(s,a,b,from='right',to='left',dash=false,color=C.blue) {
 return s.shapes.connect(a,b,{kind:from==='right' && to==='left'?'straight':'elbow',fromSide:from,toSide:to,line:{fill:color,width:2.3,style:dash?'dashed':'solid'},tail:{type:'triangle',width:'med',length:'med'}});
}

// 1. Project identity and student problem.
{
 const s=slide('Дедлайны Moodle и время студента',1,'Сценарий: несколько работ на одной неделе, пары и другие обязательства');
 text(s,'«С чего начать\nи когда заниматься?»',64,305,670,200,55,C.blue,true);
 text(s,'Сроки уже есть в Moodle',812,300,400,50,27,C.ink,true);
 text(s,'Для плана ещё нужны\nвремя на задания\nи свободные интервалы.',812,370,400,150,28,C.muted);
 text(s,'Задача проекта: помочь составить выполнимый учебный план.',64,578,1120,54,29,C.ink,true);
 notes(s,'Moodle iCalendar содержит события и сроки. Он не подтверждает факт сдачи, оценку или оставшуюся трудоёмкость. Пользователь сообщает время на работу и доступные интервалы.');
}
// 2. Solution and class import.
{
 const s=slide('Что делает CampusPulse AI',2,'Дедлайны, сохранённые пары и планирование в одном приложении');
 text(s,'Данные студента',64,300,530,50,30,C.teal,true);
 text(s,'Дедлайны Moodle\nПары из PDF или скриншотов\nВремя на учёбу в сообщении чату',64,371,540,170,28,C.ink);
 text(s,'Результат',700,300,516,50,30,C.blue,true);
 text(s,'Учебные блоки на 72 часа\nПросмотр и точное согласие\nЗапись в Calendar с проверкой',700,371,516,170,28,C.ink);
 text(s,'Импорт добавляет видимые дни в черновик. Всю неделю проверяет студент.',64,584,1152,52,26,C.amber,true);
 notes(s,'Один скриншот выбранного дня не описывает всю неделю. Повторный импорт не добавляет точные дубли. Даты действия вводит студент, если источник не содержит явного полного периода. OCR не сохраняет результат автоматически.');
}
// 3. Editable user flow.
{
 const s=slide('Основной сценарий: план на 72 часа',3,'Студент сохраняет проверенные пары и задаёт время на учёбу в чате');
 const labels=['1\nMoodle +\nпары','2\nПредложение\nиз чата','3\nСогласие\nна версию','4\nЗапись\nв Calendar','5\nНезависимая\nпроверка'];
 const ns=labels.map((v,i)=>node(s,v,64+i*236,325,208,155,i===2?C.teal:C.blue,C.white,24));
 for(let i=0;i<4;i++)arrow(s,ns[i],ns[i+1]);
 text(s,'Backend проверяет пары и занятость Calendar перед записью.',64,530,1152,52,28,C.ink,true);
 text(s,'Изменение пар или Calendar требует нового предложения и согласия.',64,592,1152,42,25,C.amber);
 notes(s,'Основной интегрированный сценарий использует connected Calendar планы из чата. Это отдельный backend writer. Модель не вызывает запись. Старый recovery остаётся отдельным защищённым режимом и не выдаётся за текущий путь.');
}
// 4. Editable architecture, with separate model-read and approved-write lanes.
{
 const s=slide('Архитектура CampusPulse',4);
 text(s,'Чтение и рекомендация',64,208,1152,38,25,C.teal,true);
 const ui=node(s,'Веб-\nинтерфейс',64,260,205,100,C.ink,C.white,24);
 const backend=node(s,'Cloud Run\nFastAPI',377,260,205,100,C.ink,C.white,24);
 const engine=node(s,'Agent Engine\nADK + Gemini',690,260,205,100,C.blue,C.white,24);
 const read=node(s,'Приватный MCP\nMoodle и пары',1003,260,213,100,C.blue,C.white,23);
 arrow(s,ui,backend);arrow(s,backend,engine);arrow(s,engine,read);
 text(s,'Backend проверяет типизированный AdviceReply и актуальные данные',64,378,1152,40,25,C.muted);
 text(s,'Запись после согласия пользователя',64,437,1152,38,25,C.teal,true);
 const approval=node(s,'Точная версия\nplan_id + hash',64,490,205,100,C.teal,C.white,23);
 const plans=node(s,'Backend\nConnectedPlans',377,490,205,100,C.ink,C.white,21);
 const write=node(s,'Приватный MCP\napply / verify',690,490,205,100,C.blue,C.white,23);
 const calendar=node(s,'Google\nCalendar',1003,490,213,100,C.blue,C.white,24);
 arrow(s,approval,plans);arrow(s,plans,write);arrow(s,write,calendar);
 text(s,'Firestore хранит пары, план, согласие и результат проверки',64,614,1152,35,23,C.muted);
 notes(s,'Верхняя линия показывает managed read-only study agent. Нижняя линия показывает действие backend после согласия владельца, а не инструменты модели. OCR использует отдельный ограниченный multimodal-вызов через делегат CalendarConversation, без инструментов и записи. Сохранение состояния выполняет backend в Firestore.');
}
// 5. Native editable responsibility table.
{
 const s=slide('MCP и Function Calling',5,'Модель получает инструменты чтения. Запись запускает backend после согласия.');
 const values=[
  ['Кто вызывает','Операция и проверка'],
  ['Модель через ADK Function Calling','read_assignments и read_schedule через MCP'],
  ['Backend','Проверка AdviceReply и сохранённых пар'],
  ['Backend через приватный MCP','read_calendar: актуальная занятость'],
  ['Пользователь в интерфейсе','Согласие на plan_id и payload_hash'],
  ['Backend через приватный MCP','apply_plan и отдельный verify_plan']
 ];
 const table=s.tables.add({rows:6,columns:2,left:64,top:300,width:1152,height:282,columnWidths:[470,682],values});
 table.borders.assign({fill:C.rule,width:1,style:'solid'});
 for(let r=0;r<6;r++){
  table.rows[r].height=47;
  for(let c=0;c<2;c++){
   const cell=table.getCell(r,c);cell.fill=r===0?C.ink:C.paper;
   cell.text.style={typeface:font,fontSize:r===0?24:23,color:r===0?C.white:C.ink,bold:r===0,verticalAlignment:'middle',autoFit:'none',wrap:'none',insets:{top:7,bottom:7,left:13,right:13}};
  }
 }
 text(s,'Текст модели не разрешает запись. Статус verified требует чтения результата.',64,602,1152,44,24,C.muted);
 notes(s,'Function Calling задаёт выбор структурированной функции для ADK-модели. MCP предоставляет приватный протокол инструментов. Writer имеет отдельную проверку владельца, живой сессии, Calendar-привязки, текущей базы пар и точного согласия. Текущий connected-сценарий не использует solve_repair как инструмент study-модели.');
}
// 6. Verified cloud release and new-version boundary.
{
 const s=slide('Agent Engine и облачный статус',6,'22 октября 2026, подтверждённый read-only релиз 1382f49');
 text(s,'Подтверждено в облаке',64,302,550,45,30,C.teal,true);
 text(s,'Обычный вход на сайт\n15 записей реального Moodle\nВызовы read_assignments / read_schedule\nTrace сохранён в истории чата',64,366,560,190,26,C.ink);
 text(s,'Новая интегрированная версия',700,302,516,45,30,C.blue,true);
 text(s,'UI и backend объединены локально\nИмпорт видимых дней с проверкой\nConnectedPlans с apply и verify\nПовтор записи без дубликатов',700,366,516,190,26,C.ink);
 text(s,'Облачная приёмка новой записи Calendar и распознавания ещё не завершена.',64,594,1152,52,25,C.amber,true);
 notes(s,'setup/PITCH-CLOUD-HANDOFF.md фиксирует успешный Cloud Build и реальный вход 22 октября для source 1382f49. В том релизе read_assignments вернул 15 записей. read_schedule сообщил not_connected, поэтому план корректно остался черновиком. Реальная запись Calendar и новый импорт файлов не входят в ту приёмку. Новые live результаты нужно сверять с итоговым rollout receipt.');
}
// 7. Local evidence and practical limits, no invented full-suite count.
{
 const s=slide('Локальная проверка и следующий шаг',7,'22 октября 2026: локальные тесты, браузер и один реальный OCR-пример');
 text(s,'2472',64,302,330,85,64,C.blue,true);
 text(s,'pytest-теста без пропусков',64,395,350,44,25,C.ink);
 text(s,'1 / 0',462,302,330,85,64,C.blue,true);
 text(s,'событие / дублей при повторе',462,395,350,64,25,C.ink);
 text(s,'4',860,302,350,85,64,C.blue,true);
 text(s,'пары на одном скриншоте',860,395,350,44,25,C.ink);
 text(s,'Vertex Gemini распознал видимые пары понедельника. Даты остались пустыми.\nЭто один проверенный пример, а не измеренная точность OCR.\nConnected fixture использует тестовые внешние сервисы.',64,470,1152,94,25,C.muted);
 text(s,'Следующий шаг: принять новую версию на реальном сайте с записью, независимой проверкой и повтором без дубликатов.',64,583,1152,64,27,C.ink,true);
 notes(s,'Реальный настроенный Vertex Gemini через адаптер extract_classes распознал личный исходный скриншот: четыре пары понедельника 16:00–16:50, 17:00–17:50, 18:00–18:50, 19:00–19:50, даты null, другие дни не добавлены. Это вызов адаптера с пользовательскими gcloud credentials, не website HTTP и не статистическая оценка качества OCR. Исходник и приватный ответ в Git не включаются. Свежий connected_browser_fixture.py --check прошёл: один Engine invocation, один schedule import, одно Calendar-создание, apply и verify, повторный apply без второго создания. Часы 17.10.2026 21:00 Asia/Almaty. Отдельные evaluation receipts подтверждают 347 offline recovery и 53 voice loopback checks. Полный локальный pytest 2472 passed, 0 skipped за 171.44 s, после трёх copy-правок 27 целевых тестов прошли. Полный gate точной облачной сборки ещё ожидается.');
}

await fs.mkdir(path.join(DIR,'previews'),{recursive:true});
for(let i=0;i<p.slides.items.length;i++) {
 const s=p.slides.items[i];const stem=`slide-${String(i+1).padStart(2,'0')}`;
 const png=await p.export({slide:s,format:'png',scale:1});
 await fs.writeFile(path.join(DIR,'previews',`${stem}.png`),new Uint8Array(await png.arrayBuffer()));
 const lay=await s.export({format:'layout'}); await fs.writeFile(path.join(DIR,'.build',`${stem}.layout.json`),await lay.text());
}
const montage=await p.export({format:'png',montage:{columns:2,slideWidth:640,padding:20,gap:20,background:C.light}});
await fs.writeFile(path.join(DIR,'previews','overview.png'),new Uint8Array(await montage.arrayBuffer()));
try {
 const pdf=await p.export({format:'pdf'});
 await fs.writeFile(path.join(finalizedDir,`${BASE_NAME}.pdf`),new Uint8Array(await pdf.arrayBuffer()));
} catch(error) {console.log(`Native PDF export unavailable. Use bundled LibreOffice on the finalized PPTX. ${error.message}`);}
const candidate=path.join(stagingDir,`${BASE_NAME}.candidate.pptx`);
await (await PresentationFile.exportPptx(p)).save(candidate);
const result=await finalizePresentation({workspaceDir:DIR,candidatePath:candidate,finalPath:path.join(finalizedDir,`${BASE_NAME}.pptx`),pythonExecutable:PYTHON,integrityValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_layout_geometry.py'),explicitTotalSlideCount:7,requiredNativeTableOwnerSlides:[5],requiredNativeChartOwnerSlides:[],layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit','--require-native-table-slide','5'],fontPolicy:{basis:'design',families:[font]},verifyArtifactToolImport:true,receiptPath:path.join(stagingDir,`${BASE_NAME}.validation.json`)});
console.log(JSON.stringify(result));
const pdfPath=path.join(finalizedDir,`${BASE_NAME}.pdf`);
try {await fs.access(pdfPath);} catch {
 const soffice=process.env.SOFFICE || path.join(runtime,'bin/override/soffice');
 const fontConfig=path.join(DIR,'.build','fonts.conf');
 await fs.writeFile(fontConfig,`<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><dir>/System/Library/Fonts</dir><dir>/System/Library/Fonts/Supplemental</dir><dir>/Library/Fonts</dir><dir>${path.join(runtime,'native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype')}</dir><cachedir>${path.join(DIR,'.build','fontcache')}</cachedir></fontconfig>`);
 const profile=pathToFileURL(path.join(DIR,'.build','soffice-profile')).href;
 const converted=spawnSync(soffice,[`-env:UserInstallation=${profile}`,'--headless','--convert-to','pdf','--outdir',finalizedDir,result.finalPath],{encoding:'utf8',env:{...process.env,FONTCONFIG_FILE:fontConfig}});
 if(converted.status!==0) throw new Error(`PDF conversion failed: ${converted.stderr}`);
 await fs.access(pdfPath);
 console.log(converted.stdout.trim());
}
await fs.copyFile(result.finalPath,path.join(DIR,'output',`${BASE_NAME}.pptx`));
await fs.copyFile(pdfPath,path.join(DIR,'output',`${BASE_NAME}.pdf`));
console.log(`Validated artifacts copied unchanged to output/${BASE_NAME}.{pptx,pdf}`);
