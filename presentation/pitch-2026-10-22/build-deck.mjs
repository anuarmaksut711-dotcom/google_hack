import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const DIR = import.meta.dirname;
const BASE_NAME = process.argv[2] || 'CampusPulse-AI-Pitch-2026-10-22-final';
const LOCAL_PYTEST_PASSED = 2256;
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
const source = 'Архитектура и исторический облачный статус: README.md, setup/RECOVERY-WEB-HANDOFF.md, setup/RECOVERY-WEB-VALIDATION.md, setup/RECOVERY-ROLLOUT-2026-10-20.md из main 4a0858a00b3f1c1c2bf382981b0400f212f06fb8. Облачный checkpoint относится к 20.10.2026. Свежие локальные результаты ветки codex/pitch-ui-demo относятся к 21.10.2026 и описаны в LOCAL-VALIDATION.md. setup/PITCH-CLOUD-HANDOFF.md отсутствовал при подготовке. Персональные данные и private resource identifiers исключены.';

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
 s.speakerNotes.text=`Проблема сформулирована как пользовательский сценарий, без заявления о проведённом исследовании или измеренном эффекте. Moodle iCalendar даёт сроки, но не трудоёмкость. ${source}`;
}
// 2. Solution, flat typography and clear implementation boundary.
{
 const s=slide('Что делает CampusPulse AI',2,'Персональные сроки, сохранённое расписание и диалог в одном приложении');
 text(s,'Опубликованное приложение',64,300,530,50,30,C.teal,true);
 text(s,'Google-вход и Moodle iCalendar\nДиалог с Gemini по дедлайнам\nРучное расписание и Firestore',64,371,540,170,28,C.ink);
 text(s,'Recovery-процесс в коде',700,300,516,50,30,C.blue,true);
 text(s,'Предложение на 72 часа\nСогласие на точный план\nПрименение и независимый verify',700,371,516,170,28,C.ink);
 text(s,'По checkpoint 20.10 персональный recovery на сайте выключен.',64,584,1152,52,26,C.amber,true);
 s.speakerNotes.text=`Фактический статус опубликованного приложения взят из README и checkpoint 20.10.2026. Текущий UI в main использует название Google AI, а презентация сохраняет запрошенное название CampusPulse AI. Ответ обычного чата сам по себе не записывает события Calendar. ${source}`;
}
// 3. Editable process diagram.
{
 const s=slide('Основной сценарий: план на 72 часа',3,'Студент задаёт свободное время и оставшуюся трудоёмкость заданий');
 const labels=['1\nЧтение\nисточников','2\nРасчёт\nпредложения','3\nСогласие\nна план','4\nЗапись\nв Calendar','5\nНезависимый\nverify'];
 const ns=labels.map((v,i)=>node(s,v,64+i*236,325,208,155,i===2?C.teal:C.blue,C.white,24));
 for(let i=0;i<4;i++)arrow(s,ns[i],ns[i+1]);
 text(s,'Версия и хеш связывают согласие с конкретным предложением.',64,530,1152,52,28,C.ink,true);
 text(s,'Полный персональный сценарий в облаке пока не подтверждён.',64,592,1152,42,25,C.amber);
 s.speakerNotes.text=`В implemented recovery чтение источников предшествует solve_repair. apply_plan требует отдельного согласия на сохранённую версию и хеш. verify_plan перечитывает результат. Этот процесс показан как реализованный контракт, а не пройденная персональная облачная приёмка. Источник: README, Recovery website integration handoff. ${source}`;
}
// 4. Editable architecture. Dashed path denotes inactive recovery composition.
{
 const s=slide('Архитектура приложения и recovery',4);
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
 arrow(s,backend,engine,'bottom','top',true,C.blue); arrow(s,engine,mcp); arrow(s,mcp,calendar);
 arrow(s,mcp,store,'top','bottom',false,C.ink);
 text(s,'Обычный чат',64,187,500,35,23,C.teal,true);
 text(s,'Пунктир: персональный cloud recovery не подтверждён',330,579,880,44,23,C.amber);
 s.speakerNotes.text=`Native editable shapes and attached connectors. Схема упрощена: Cloud Run также обращается к Firestore напрямую, MCP читает/пишет состояние в том же Firestore. Gemini API находится в обычном chat path. Recovery runtime использует Gemini через Vertex AI и приватный MCP. Пунктир обозначает отключённую опциональную композицию сайта. README, architecture section, and rollout checkpoint 20.10.2026. ${source}`;
}
// 5. Native table of actual tool responsibilities.
{
 const s=slide('MCP и Function Calling',5,'Модель выбирает инструмент. Сервер проверяет доступ, входные данные и согласие.');
 const values=[
  ['Механизм и инструмент','Роль в recovery'],
  ['MCP  read_assignments','Прочитать задания и сохранить снимок'],
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
 text(s,'Изменение источников отменяет старое согласие. Неясный результат записи не вызывает автоматический повтор.',64,595,1152,52,24,C.muted);
 s.speakerNotes.text=`Инструменты и их mapping следуют README: solve_repair предоставляется как Function Calling, остальные перечисленные инструменты через MCP. MCP и Function Calling дополняют друг друга. Без отдельного backend-approved consent вызов apply_plan не разрешён. ${source}`;
}
// 6. Historical cloud receipts and explicit remaining gates.
{
 const s=slide('Agent Engine и облачный статус',6,'Облачный статус по checkpoint от 20 октября 2026');
 text(s,'Зафиксировано в отчёте',64,302,550,45,30,C.teal,true);
 text(s,'Cloud Run, Agent Engine и MCP\nПриватная операция async_query\nПроверки инфраструктуры и авторизации',64,366,560,170,26,C.ink);
 text(s,'До персонального recovery',700,302,516,45,30,C.blue,true);
 text(s,'Общие версии кода и настроек\nРеальные привязки двух пользователей\nОтдельные разрешённые Calendars\nПолный live-цикл apply и verify',700,366,516,195,26,C.ink);
 text(s,'Доступность ресурсов и пользовательский сценарий проверяются отдельно.',64,594,1152,44,25,C.amber,true);
 s.speakerNotes.text=`Cloud receipts исторические, 20.10.2026. В тот checkpoint реальный Engine async_query достиг service identity rejection boundary при синтетическом/неразрешённом вводе. Это подтверждает инфраструктурный путь и границы доступа, но не персональную successful tool execution. Per-user code ещё требовал обновления Engine/MCP, website recovery отключён. Credentials, account IDs, resource IDs and URLs исключены. ${source}`;
}
// 7. Results and scope, no implied accuracy metric.
{
 const s=slide('Локальные результаты и границы',7,'Свежий локальный прогон 21 октября 2026, изолированное окружение');
 text(s,String(LOCAL_PYTEST_PASSED),64,302,350,85,64,C.blue,true);
 text(s,'pytest-тестов прошли',64,395,350,44,25,C.ink);
 text(s,'347',462,302,350,85,64,C.blue,true);
 text(s,'offline recovery-проверок',462,395,350,44,25,C.ink);
 text(s,'53',860,302,350,85,64,C.blue,true);
 text(s,'voice loopback-проверки',860,395,350,44,25,C.ink);
 text(s,'26 пропусков: 12 Firestore Emulator, 14 PowerShell. Внешние сервисы синтетические.\nВ браузере пройден локальный цикл до verify и повторной загрузки.\nКачество Gemini и персональный cloud apply/verify эти проверки не подтверждают.',64,470,1152,94,24,C.muted);
 text(s,'Следующий критерий: два реальных пользователя проходят весь облачный цикл с согласованием, записью и независимой проверкой.',64,583,1152,64,27,C.ink,true);
 s.speakerNotes.text=`Source: LOCAL-VALIDATION.md, fresh local verification 21.10.2026 in .venv-pitch. Full pytest: ${LOCAL_PYTEST_PASSED} passed, 26 skipped, 35 warnings, 248.26 seconds. Missing environments: 12 Firestore Emulator tests and 14 PowerShell tests skipped. Separate recovery offline evaluation: 347 passed, 95 warnings. Voice literal-loopback: 53 passed. Wheel/sdist, pip check and MCP composition passed. Full local browser chain: effort90 and working window save/reload, proposal, exact consent, separate apply, verified, reload. Synthetic counters: 2 Calendar writes, apply_plan1, verify_plan1, 8 Calendar reads. Desktop/narrow layouts and console checked. Do not merge these counts or use them as a model quality measure. User acceptance with two real users and personal cloud test Calendars is still pending. ${source}`;
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
