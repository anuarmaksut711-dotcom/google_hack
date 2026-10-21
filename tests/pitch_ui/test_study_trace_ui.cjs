const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/client.py'), 'utf8');
const traceSource = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/study_trace.py'), 'utf8');
const traceScript = traceSource.match(/SCRIPT = r"""([\s\S]*?)"""/)[1];
const start = source.indexOf('  function appendChatInline(');
const end = source.indexOf('  function watchChatProgress(', start);
assert(start >= 0 && end > start, 'The real chat rendering functions must be present');

function element(tag = 'div') {
  let text = '';
  return {localName:tag, children:[], className:'', dataset:{}, hidden:false,
    classList:{add(){}, toggle(){}}, attributes:{}, open:false,
    setAttribute(name, value) {this.attributes[name] = value;},
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;},
    querySelectorAll(selector) {
      if (selector.includes(' ')) {
        const [first, ...rest] = selector.split(' ');
        return this.querySelectorAll(first).flatMap(node => node.querySelectorAll(rest.join(' ')));
      }
      const matches = child => selector[0] === '.' ? child.className.split(' ').includes(selector.slice(1)) : child.localName === selector;
      return this.children.filter(child => typeof child !== 'string').flatMap(child =>
        [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
    },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { text = ''; this.children = children; },
    get textContent() { return text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); },
    set textContent(value) { text = String(value); this.children = []; },
    set innerHTML(value) { throw new Error('Chat/tool content must never be inserted as HTML: ' + value); }};
}
function render(turn, options = {}) {
  const nodes = new Map(), reveals = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const sandbox = {$, Event, console, Intl, window:{}, controls(){},
    document:{createElement:element, createTextNode(value){const node = element('#text'); node.textContent = value; return node;}, dispatchEvent(){}},
    answerReveal:{finish(){}, start(...args){reveals.push(args);}}};
  vm.createContext(sandbox);
  vm.runInContext(traceScript, sandbox);
  vm.runInContext('let renderedChat=null,chatLimitReached=false,speechAvailable=false,chatAvailable=false,chatWorking=false;\n' + source.slice(start, end), sandbox);
  sandbox.chat = {available:true, turns:[{request_key:'request_1', message:'Мои сроки?',
    status:'completed', created_at:'2026-10-22T08:00:00Z', response:'Два задания на неделю.', duration_ms:1500, ...turn}]};
  sandbox.options = options;
  vm.runInContext('showChat(chat, options)', sandbox);
  return {log:$('chat-history'), reveals};
}
const descendants = node => [node, ...node.children.flatMap(child => typeof child === 'string' ? [] : descendants(child))];
const trace = log => descendants(log).find(node => node.className === 'agent-work' && !node.hidden)?.querySelector('details');
const caseName = process.argv[3];

if (caseName === 'legacy') {
  for (const turn of [{}, {tool_trace:[]}, {agent_run_id:'study_run_without_trace'}]) {
    const {log} = render(turn);
    assert.equal(trace(log), undefined, 'History without observations must not invent tool calls');
    assert.match(log.textContent, /Два задания на неделю/);
    assert.equal(log.children.length, 2, 'Legacy user/reply grouping must remain intact');
  }
} else if (caseName === 'actual') {
  const {log, reveals} = render({tool_trace:[
    {tool:'read_assignments', status:'completed', duration_ms:1250, result_count:7},
    {tool:'read_schedule', status:'completed', duration_ms:500, result_count:0},
  ]}, {animateRequestKey:'request_1'});
  const details = trace(log);
  assert(details, 'Actual observations must be visible below the assistant reply');
  const rows = descendants(details).filter(node => node.localName === 'li');
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /Задания Moodle.*Получено записей: 7.*1,3 с/);
  assert.match(rows[1].textContent, /Расписание пар.*Получено занятий: 0.*0,5 с/);
  const assistant = log.children[1];
  const answer = assistant.children.find(node => node.className === 'chat-answer');
  assert(assistant.children.indexOf(assistant.querySelector('.agent-work')) < assistant.children.indexOf(answer));
  assert.equal(details.open, true, 'Actual tool activity is available in the native disclosure');
  assert.equal(reveals.length, 1);
  assert.equal(reveals[0][0], answer, 'Existing answer reveal receives only the answer, not tool results');
} else if (caseName === 'failed') {
  for (const [status, label] of [['failed', 'Не удалось получить данные'], ['unavailable', 'Данные сейчас недоступны'], ['not_connected', 'Расписание пар не заполнено']]) {
    const {log} = render({status:'failed', response:null,
      tool_trace:[{tool:'read_schedule', status, duration_ms:25, result_count:0}]});
    const details = trace(log);
    assert(details, 'Saved observations must survive a failed assistant response');
    assert.match(details.textContent, new RegExp(label));
    assert.doesNotMatch(details.textContent, /Выполнено|Успешно/);
    assert.match(log.textContent, /Агент не смог ответить/);
  }
} else if (caseName === 'escaped') {
  const markup = '<img src=x onerror=alert(1)>', unknownStatus = '<script>bad()</script>';
  const {log} = render({tool_trace:[{tool:markup, status:unknownStatus, duration_ms:50, result_count:3}]});
  const details = trace(log);
  assert.equal(details, undefined, 'Unknown tool names must not become claimed MCP activity');
  assert(!log.textContent.includes(markup));
  assert(!log.textContent.includes(unknownStatus));
  assert.equal(descendants(log).some(node => ['img', 'script'].includes(node.localName)), false);
} else if (caseName === 'run_id') {
  const runId = 'study_run_<unsafe>';
  const {log} = render({agent_run_id:runId});
  const metadata = descendants(log).find(node => node.localName === 'details' &&
    node.children.some(child => child.localName === 'summary' && child.textContent === 'Сведения о запросе'));
  assert(metadata.textContent.includes('Запуск агента: ' + runId));
  assert.equal(trace(log), undefined);
} else if (caseName === 'malformed') {
  for (const tool_trace of [null, {}, 'read_assignments', [null, {}, false]]) {
    assert.equal(trace(render({tool_trace}).log), undefined);
  }
  for (const result_count of ['7', -1]) {
    const {log} = render({tool_trace:[{tool:'read_assignments', status:'completed', result_count, duration_ms:-1}]});
    assert(trace(log));
    assert.doesNotMatch(trace(log).textContent, /Получено записей:|NaN|-1/,
      'Missing/invalid metrics must not be replaced with invented numbers');
  }
} else {
  throw new Error('Unknown test case: ' + caseName);
}
console.log('Study trace UI passed: ' + caseName);
