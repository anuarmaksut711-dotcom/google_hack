import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const DIR = import.meta.dirname;
const BASE_NAME = process.argv[2] || 'CampusPulse-AI-Pitch-2026-10-22-final';
const LOCAL_PYTEST_PASSED = Number(process.env.PITCH_PYTEST_PASSED);
if(!Number.isInteger(LOCAL_PYTEST_PASSED) || LOCAL_PYTEST_PASSED < 1) throw Error('Set PITCH_PYTEST_PASSED from the verified final pytest receipt.');
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
const source = 'Исходники UI: codex/pitch-ui-demo, база acf869b1eff2e50f2669c3f9f824b7b83cfb534d, 22.10.2026. Локальные проверки описаны в SCHEDULE-VALIDATION-2026-10-22.md. Облачный источник: setup/PITCH-CLOUD-HANDOFF.md из origin/codex/pitch-agent-cloud, SHA 1382f49aaba7c37c6571bbd8355349064e039012. Handoff описывает read-only study agent; live website acceptance pending, recovery/Calendar writes не включены. Исторический checkpoint 20.10 не считается новой приёмкой. Личные данные, credentials и resource identifiers исключены.';

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

// 1. Problem, including the project identity rather than adding an eighth cover.
{
 const s=slide('Дедлайны Moodle и время студента',1,'Сценарий: несколько работ на одной неделе, пары и другие обязательства');
 text(s,'«С чего начать\nи когда заниматься?»',64,305,670,200,55,C.blue,true);
 text(s,'Сроки уже есть в Moodle',812,300,400,50,27,C.ink,true);
 text(s,'Для плана ещё нужны\nтрудоёмкость заданий\nи свободные интервалы.',812,370,400,150,28,C.muted);
 text(s,'Задача проекта: помочь собрать выполнимый учебный план.',64,578,1120,54,29,C.ink,true);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
}
// 2. Solution, flat typography and clear implementation boundary.
{
 const s=slide('Что делает CampusPulse AI',2,'Персональные сроки, сохранённое расписание и диалог в одном приложении');
 text(s,'Данные студента',64,300,530,50,30,C.teal,true);
 text(s,'Дедлайны Moodle\nПары из PDF или скриншота\nМинуты на задания и свободные окна',64,371,540,170,28,C.ink);
 text(s,'Понятный результат',700,300,516,50,30,C.blue,true);
 text(s,'Предложенное расписание на 72 часа\nПодтверждение точной версии\nCalendar и независимая проверка',700,371,516,170,28,C.ink);
 text(s,'Новый UI проверен локально. Полный облачный цикл не подтверждён.',64,584,1152,52,26,C.amber,true);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
}
// 3. Editable process diagram.
{
 const s=slide('Основной сценарий: план на 72 часа',3,'Сначала студент проверяет распознанные пары, затем сохраняет время для заданий');
 const labels=['1\nMoodle +\nпары','2\nПредложенное\nрасписание','3\nПодтвердить\nверсию','4\nЗапись\nв Calendar','5\nНезависимая\nпроверка'];
 const ns=labels.map((v,i)=>node(s,v,64+i*236,325,208,155,i===2?C.teal:C.blue,C.white,24));
 for(let i=0;i<4;i++)arrow(s,ns[i],ns[i+1]);
 text(s,'Учебные блоки не пересекаются с парами. Согласие — на точную версию.',64,530,1152,52,28,C.ink,true);
 text(s,'Полный персональный сценарий в облаке пока не подтверждён.',64,592,1152,42,25,C.amber);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
}
// 4. Editable architecture. Dashed path denotes inactive recovery composition.
{
 const s=slide('Архитектура CampusPulse',4);
 const browser=node(s,'Веб-\nинтерфейс',64,242,180,86,C.ink,C.white,23);
 const backend=node(s,'Cloud Run\nFastAPI',330,242,220,86,C.ink,C.white,25);
 const gemini=node(s,'Google ADK\nGemini API',642,242,220,86,C.teal,C.white,24);
 const store=node(s,'Firestore',960,242,256,86,C.ink,C.white,26);
 const moodle=node(s,'Moodle\niCalendar',64,440,180,86,C.ink,C.white,23);
 const engine=node(s,'Agent Engine\nGemini через Vertex AI',330,440,270,100,C.blue,C.white,23);
 const mcp=node(s,'Приватный MCP',698,440,220,100,C.blue,C.white,24);
 const calendar=node(s,'Google\nCalendar',1016,440,200,100,C.blue,C.white,24);
 arrow(s,browser,backend); arrow(s,backend,gemini);
 s.shapes.add({geometry:'line',position:{left:440,top:217,width:0,height:25},line:{fill:C.ink,width:2.3}});
 s.shapes.add({geometry:'line',position:{left:440,top:217,width:648,height:0},line:{fill:C.ink,width:2.3}});
 s.shapes.add({geometry:'connector',kind:'straight',position:{left:1088,top:217,width:0,height:25},line:{fill:C.ink,width:2.3},tail:{type:'triangle',width:'med',length:'med'}});
 arrow(s,moodle,backend,'top','bottom',false,C.ink);
 arrow(s,backend,engine,'bottom','top',true,C.blue); arrow(s,engine,mcp); arrow(s,mcp,calendar,'right','left',true,C.blue);
 arrow(s,mcp,store,'top','bottom',false,C.ink);
 text(s,'Чат и распознавание файлов',64,187,500,35,23,C.teal,true);
 text(s,'Путь через Engine: новый study-agent читает; recovery пишет после согласия',330,579,880,44,23,C.amber);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
}
// 5. Native table of actual tool responsibilities.
{
 const s=slide('MCP и Function Calling',5,'Модель выбирает инструмент. Сервер проверяет доступ, входные данные и согласие.');
 const values=[
  ['Механизм и инструмент','Роль в сценарии'],
  ['MCP  read_assignments / read_schedule','Study-agent: задания и пары (только чтение)'],
  ['MCP  read_calendar','Прочитать занятые интервалы'],
  ['Function Calling  solve_repair','Рассчитать предложение плана'],
  ['MCP  apply_plan','Применить согласованную версию'],
  ['MCP  verify_plan','Перечитать результат и проверить запись']
 ];
 const table=s.tables.add({rows:6,columns:2,left:64,top:300,width:1152,height:276,columnWidths:[500,652],values});
 table.borders.assign({fill:C.rule,width:1,style:'solid'});
 for(let r=0;r<6;r++){
  table.rows[r].height=46;
  for(let c=0;c<2;c++){
   const cell=table.getCell(r,c);cell.fill=r===0?C.ink:C.paper;
   cell.text.style={typeface:font,fontSize:r===0?24:23,color:r===0?C.white:C.ink,bold:r===0,verticalAlignment:'middle',autoFit:'none',wrap:'none',insets:{top:7,bottom:7,left:13,right:13}};
  }
 }
 text(s,'Recovery-инструменты apply/verify отделены от study-agent. Неясный результат записи не считается успехом.',64,595,1152,52,24,C.muted);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
}
// 6. Historical cloud receipts and explicit remaining gates.
{
 const s=slide('Agent Engine и облачный статус',6,'Handoff Мерея от 22 октября: ветка codex/pitch-agent-cloud');
 text(s,'Реализовано в новой ветке',64,302,550,45,30,C.teal,true);
 text(s,'Чат → Agent Engine → приватный MCP\nread_assignments и read_schedule\nФактический trace инструментов',64,366,560,170,26,C.ink);
 text(s,'Что ещё не подтверждено',700,302,516,45,30,C.blue,true);
 text(s,'Приёмка опубликованного сайта\nРаспознавание реальных PDF / фото\nИнтеграция нашей UI-ветки\nЗапись Calendar и live verify',700,366,516,195,26,C.ink);
 text(s,'Этот read-only релиз не включает recovery и запись в Calendar.',64,594,1152,44,25,C.amber,true);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
}
// 7. Results and scope, no implied accuracy metric.
{
 const s=slide('Локальные результаты и границы',7,'22 октября 2026: тестовые внешние сервисы, Firestore Emulator');
 text(s,String(LOCAL_PYTEST_PASSED),64,302,350,85,64,C.blue,true);
 text(s,'pytest-тестов прошли',64,395,350,44,25,C.ink);
 text(s,'347',462,302,350,85,64,C.blue,true);
 text(s,'offline recovery-проверок',462,395,350,44,25,C.ink);
 text(s,'53',860,302,350,85,64,C.blue,true);
 text(s,'voice loopback-проверки',860,395,350,44,25,C.ink);
 text(s,'Без пропусков. В браузере: PDF → проверенные пары → расписание → verify.\nПара 22:00–22:20 исключена: учебный блок начинается в 22:30.\nКачество Gemini OCR и запись в реальный Calendar ещё не подтверждены.',64,470,1152,94,24,C.muted);
 text(s,'Следующий критерий: два реальных пользователя проходят весь облачный цикл с согласованием, записью и независимой проверкой.',64,583,1152,64,27,C.ink,true);
 s.speakerNotes.text=`Слайд отражает локальный реализованный сценарий и ограничения облачной приёмки. PDF/PNG/JPEG отправляются модели только по кнопке, распознанные пары требуют проверки и сохранения. Пары не экспортируются автоматически в Calendar. Recovery использует отдельное подтверждение версии и независимый verify. Новый read-only cloud study-agent не имеет инструментов записи. Локальные внешние сервисы синтетические, реальная модель OCR не тестировалась. ${source}`;
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
