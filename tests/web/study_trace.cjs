// Exercise the exact shipped module with a DOM boundary and deterministic timers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[2];
const source = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/study_trace.py'), 'utf8');
const script = source.match(/SCRIPT = r"""([\s\S]*?)"""/)[1];

class Element {
  constructor(tag) {
    this.localName = tag; this.children = []; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.open = false; this.className = ''; this._text = '';
    this.classList = {add() {}, toggle() {}};
  }
  set innerHTML(_) { throw new Error('Source text must never become HTML'); }
  set outerHTML(_) { throw new Error('Source text must never become HTML'); }
  insertAdjacentHTML() { throw new Error('Source text must never become HTML'); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    if (selector.includes(' ')) {
      const [first, ...rest] = selector.split(' ');
      return this.querySelectorAll(first).flatMap(node => node.querySelectorAll(rest.join(' ')));
    }
    const matches = item => selector[0] === '.' ? item.className.split(' ').includes(selector.slice(1)) : item.localName === selector;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
}

function harness() {
  const timers = new Map(); let sequence = 0, time = 1000;
  const sandbox = {window: {}, document: {createElement: tag => new Element(tag)}, Intl,
    Date: class extends Date { static now() { return time; } },
    setTimeout(callback, delay) { const id = ++sequence; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); }};
  vm.createContext(sandbox); vm.runInContext(script, sandbox);
  return {...sandbox.window.CampusPulseStudyTrace, sandbox, timers,
    async tick() {
      assert.equal(timers.size, 1, 'There must be exactly one scheduled read');
      const [id, timer] = [...timers][0]; timers.delete(id); time += timer.delay;
      await timer.callback();
    },
    advance(value) { time += value; }};
}
const turn = (status = 'working', tool_calls = [], phase = 'tools') => ({status, agent_progress: {phase, tool_calls}});
const call = (tool, status = 'completed', extra = {}) => ({tool, status, ...extra});
const rows = node => node.querySelector('.agent-work-calls').children;

async function showsOnlyObservedToolsAndTruthfulResults() {
  const h = harness(), queued = h.create(turn('working', [], 'queued'));
  assert.equal(queued.hidden, false);
  assert.equal(rows(queued).length, 0, 'Queued work must not fabricate a prearranged tool pipeline');
  assert.equal(queued.querySelector('.agent-work-badge').hidden, true, 'No MCP badge before a real call');
  assert.match(queued.textContent, /Ожидаю запуск агента/);
  const active = h.create(turn('working', [call('read_assignments', 'running')]));
  assert.match(active.textContent, /Получаю задания Moodle/);
  assert.match(active.textContent, /Выполняется/);
  assert.equal(rows(active)[0].dataset.state, 'running');
  assert.equal(active.querySelector('.agent-work-badge').hidden, false);
  assert(!active.textContent.includes('read_schedule'), 'A deadline-only query must not claim to check classes');
  assert(!active.textContent.includes('Составляю план'), 'Calling tools is not yet composing a plan');
  h.update(active, turn('working', [call('read_assignments', 'completed', {result_count: 0, duration_ms: 1250})], 'composing'));
  assert.match(active.textContent, /Получено записей: 0/);
  assert.match(active.textContent, /Готовлю ответ по результатам проверки/);
  assert(!active.textContent.includes('не подключён'), 'A successful empty source is different from an absent connection');
  assert.match(active.querySelector('.agent-work-meta').textContent, /1,3 с/);
  h.update(active, turn('completed', [call('read_assignments', 'not_connected'), call('read_schedule', 'not_connected')]));
  assert.match(active.textContent, /Moodle не подключён/);
  assert.match(active.textContent, /Расписание пар не заполнено/);
  assert(!active.textContent.includes('Получено записей: 0'));
  assert.equal(rows(active)[0].dataset.state, 'warning');
  const absent = h.create({status: 'completed', tool_trace: []});
  assert.equal(absent.hidden, true, 'Ordinary answers must not claim MCP activity');
}

async function preservesCallOrderAndRepeatedCallsFromDurableTrace() {
  const h = harness();
  const node = h.create({status: 'completed', tool_trace: [
    call('read_assignments', 'completed', {result_count: 15, duration_ms: 201}),
    call('read_schedule', 'completed', {result_count: 0, duration_ms: 80}),
    call('read_assignments', 'completed', {result_count: 13, duration_ms: 31}),
  ]});
  assert.equal(rows(node).length, 3, 'Repeated real calls must remain visible');
  assert.deepEqual(rows(node).map(row => row.querySelector('code').textContent),
    ['read_assignments', 'read_schedule', 'read_assignments']);
  assert.deepEqual(rows(node).map(row => row.querySelector('.agent-work-result').textContent),
    ['Получено записей: 15', 'Получено занятий: 0', 'Получено записей: 13']);
  assert.match(node.textContent, /Ответ подготовлен/);
}

async function githubObservationsUseActualStatusAndNeverExposeSourceContents() {
  const h = harness(), node = h.create(turn('working', [call('read_github_assignment', 'running')]));
  assert.equal(rows(node).length, 1);
  assert.match(node.textContent, /Читаю репозиторий GitHub/);
  assert.match(node.textContent, /Выполняется/);
  assert.equal(rows(node)[0].dataset.state, 'running');
  assert.equal(node.querySelector('code').textContent, 'read_github_assignment');
  assert(!node.textContent.includes('Moodle'));
  assert(!node.textContent.includes('Расписание пар'));
  h.update(node, {status: 'completed', tool_trace: [call('read_github_assignment', 'completed', {
    duration_ms: 800, result_count: 4, readme: '<script>private()</script>', issues: ['source data']})]});
  assert.match(node.textContent, /Репозиторий GitHub/);
  assert.match(node.textContent, /Получено записей: 4/);
  assert.equal(rows(node)[0].dataset.state, 'completed');
  assert(!node.textContent.includes('<script>'));
  assert(!node.textContent.includes('source data'));
  for (const [status, message] of [['partial', 'Получена часть данных'],
    ['unavailable', 'Данные сейчас недоступны'], ['failed', 'Не удалось получить данные'],
    ['not_connected', 'Репозиторий недоступен']]) {
    h.update(node, {status: 'completed', tool_trace: [call('read_github_assignment', status, {result_count: 0})]});
    assert(node.textContent.includes(message));
    assert.equal(rows(node)[0].dataset.state, 'warning');
    assert(!node.textContent.includes('Получено записей: 0'));
    assert(!node.textContent.includes('Moodle не подключён'));
  }
  h.update(node, turn('unknown', [call('read_github_assignment', 'running')]));
  assert.match(node.textContent, /Результат не получен/);
  assert.equal(rows(node)[0].dataset.state, 'warning');
  assert(!node.textContent.includes('Выполняется'));
}

async function finalTraceSupersedesAnEarlierPartialProgressSnapshot() {
  const h = harness(), node = h.create({status: 'completed',
    agent_progress: {phase: 'tools', tool_calls: [call('read_assignments', 'running')]},
    tool_trace: [call('read_assignments', 'completed', {result_count: 15}),
      call('read_schedule', 'not_connected')],
  });
  assert.equal(rows(node).length, 2, 'The final saved trace must include all completed tools');
  assert.match(node.textContent, /Получено записей: 15/);
  assert.match(node.textContent, /Расписание пар не заполнено/);
  assert(!node.textContent.includes('Выполняется'));
  assert(!node.textContent.includes('Результат не получен'), 'An earlier partial poll must not override the final result');
}

async function completedChatRerenderPreservesCollapsedDisclosure() {
  const h = harness(), elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, new Element('div'));
    return elements.get(id);
  };
  Object.assign(h.sandbox, {$, answerReveal: {finish() {}, start() {}}, controls() {},
    renderChatAnswer(node, value) {node.textContent = value;}, Event: class {}});
  h.sandbox.document.body = {dataset: {chatMode: 'calendar_advice'}};
  h.sandbox.document.dispatchEvent = () => {};
  const client = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/client.py'), 'utf8');
  const from = client.indexOf('  function showChat('), to = client.indexOf('  function watchChatProgress(', from);
  assert(from >= 0 && to > from);
  vm.runInContext(`let renderedChat,chatLimitReached,speechAvailable,chatAvailable,chatWorking;
    ${client.slice(from, to)}`, h.sandbox);
  const base = {request_key: 'one', created_at: '2026-10-22T14:00:00Z', message: 'Plan', duration_ms: null};
  h.sandbox.chat = {available: true, turns: [{...base, status: 'working',
    agent_progress: {phase: 'tools', tool_calls: [call('read_assignments', 'running')]}}]};
  vm.runInContext('showChat(chat)', h.sandbox);
  const initial = $('chat-history').querySelector('.agent-work details');
  initial.open = false;
  h.sandbox.chat = {available: true, turns: [{...base, status: 'completed', response: 'Here is your plan',
    tool_trace: [call('read_assignments', 'completed', {result_count: 2})]}]};
  vm.runInContext('showChat(chat)', h.sandbox);
  const final = $('chat-history').querySelector('.agent-work details');
  assert.notEqual(final, initial, 'The terminal rendering replaces the conversation DOM');
  assert.equal(final.open, false, 'Completion must retain a disclosure the user closed while waiting');
  assert.match($('chat-history').textContent, /Here is your plan/);
  assert.match($('chat-history').textContent, /Получено записей: 2/);
}

async function handlesFailuresAndNeverTreatsUnknownOrRunningAsSuccessful() {
  const h = harness(), node = h.create(turn('working', [call('read_assignments', 'unavailable')]));
  assert.match(node.textContent, /Данные сейчас недоступны/);
  assert(!node.textContent.includes('Получено записей: 0'));
  h.update(node, turn('failed', [call('read_schedule', 'unexpected_status')]));
  assert.match(node.textContent, /Не удалось подготовить ответ/);
  assert.match(node.textContent, /Не удалось получить данные/);
  assert.equal(rows(node)[0].dataset.state, 'warning');
  h.update(node, turn('unknown', [call('read_schedule', 'running')]));
  assert.match(node.textContent, /Ответ не получен/);
  assert.match(node.textContent, /Результат не получен/);
  assert(!node.textContent.includes('Выполняется'), 'A terminal unknown turn must not animate an ongoing call');
  assert.equal(rows(node)[0].dataset.state, 'warning');
}

async function unsafeSourceTextIsNotHTMLAndDetailsStayCollapsed() {
  const h = harness(), evil = '<img src=x onerror=alert(1)>', calls = [
    call('read_assignments', 'completed', {result_count: 2, duration_ms: 350, title: evil, error: evil}),
    call(evil, 'completed', {result_count: 200}),
    call('write_calendar', 'completed', {result_count: 1}),
    call('read_schedule', evil, {result_count: evil, duration_ms: evil}),
  ];
  const node = h.create(turn('working', calls));
  assert.equal(rows(node).length, 2, 'Unrecognised/write tools must not appear as an approved study tool');
  assert(!node.textContent.includes(evil), 'Tool arguments/errors and source content are not UI markup');
  const details = node.querySelector('details'); details.open = false;
  h.update(node, turn('working', [call('read_assignments', 'completed', {result_count: 2}),
    call('read_schedule', 'running')], 'composing'));
  assert.equal(node.querySelector('details'), details);
  assert.equal(details.open, false, 'Polling must preserve the user’s disclosure choice');
  assert.match(node.textContent, /Готовлю ответ по результатам проверки/);
  assert(!node.textContent.includes('Составляю план'), 'A schedule read alone does not prove a plan is being generated');
  const row = rows(node)[0];
  h.update(node, turn('working', [call('read_assignments', 'completed', {result_count: 2}),
    call('read_schedule', 'running')], 'composing'));
  assert.equal(rows(node)[0], row, 'An unchanged snapshot must not recreate or announce rows');
  const phase = node.querySelector('.agent-work-phase');
  assert.equal(phase.attributes.role, 'status');
  assert.equal(phase.attributes['aria-live'], 'polite');
}

async function pollingDoesNotOverlapAndStopsAtTerminalTurn() {
  const h = harness(); let resolveRead, count = 0, active = 0, maxActive = 0;
  const observed = [], poller = new h.Poller({read() {
    ++count; ++active; maxActive = Math.max(maxActive, active);
    return new Promise(resolve => { resolveRead = chat => { --active; resolve(chat); }; });
  }, valid: () => true, update: (chat, item) => observed.push(item.status), failed: assert.fail, interval: 10});
  poller.start('one'); const pending = h.tick();
  assert.equal(h.timers.size, 0, 'No next read may be scheduled while a read is unresolved');
  assert.equal(count, 1);
  resolveRead({turns: [{request_key: 'one', status: 'working'}]}); await pending;
  assert.equal(h.timers.size, 1);
  const final = h.tick();
  resolveRead({turns: [{request_key: 'other', status: 'working'}, {request_key: 'one', status: 'completed'}]}); await final;
  assert.equal(maxActive, 1);
  assert.deepEqual(observed, ['working', 'completed']);
  assert.equal(h.timers.size, 0, 'A completed request must stop polling even if another turn is working');
}

async function staleAndStoppedReadsCannotUpdateOrReschedule() {
  for (const invalidate of ['stop', 'session']) {
    const h = harness(); let resolveRead, valid = true, updates = 0;
    const poller = new h.Poller({read: () => new Promise(resolve => {resolveRead = resolve;}),
      valid: () => valid, update() {++updates;}, failed: assert.fail, interval: 10});
    poller.start('one'); const pending = h.tick();
    if (invalidate === 'stop') poller.stop(); else valid = false;
    resolveRead({turns: [{request_key: 'one', status: 'working'}]}); await pending;
    assert.equal(updates, 0, `${invalidate} must discard an in-flight response`);
    assert.equal(h.timers.size, 0);
  }
  const h = harness(); let count = 0;
  const poller = new h.Poller({read() {++count;}, valid: () => false, update: assert.fail, failed: assert.fail});
  poller.start('one'); await h.tick();
  assert.equal(count, 0, 'A stale selection/session must not start an API read');
}

async function transientReadFailurePreservesDisplayAndNeverResubmitsMessage() {
  const h = harness(); let count = 0, display = 'Last observed successful data'; const errors = [];
  const poller = new h.Poller({async read() {
    ++count;
    if (count === 1) throw Object.assign(new Error('temporary network error'), {status: 503});
    return {turns: [{request_key: 'one', status: 'unknown'}]};
  }, valid: () => true, update(chat, item) {display = item.status;}, failed(error) {errors.push(error.status);}, interval: 10});
  poller.start('one'); await h.tick();
  assert.equal(display, 'Last observed successful data', 'A read error must preserve already observed information');
  assert.deepEqual(errors, [503]);
  assert.equal(h.timers.size, 1, 'A transient read error can retry only the read');
  await h.tick(); assert.equal(display, 'unknown'); assert.equal(h.timers.size, 0);
  assert.equal(count, 2);
  const denied = new h.Poller({async read() {throw Object.assign(new Error('expired session'), {status: 403});},
    valid: () => true, update: assert.fail, failed(error) {errors.push(error.status);}});
  denied.start('one'); await h.tick();
  assert.equal(h.timers.size, 0, 'An expired session stops further reads');
  assert.deepEqual(errors, [503, 403]);
  assert.equal(typeof poller.send, 'undefined', 'The progress controller has no mutation/replay capability');
}

async function restartAndDeadlineDiscardOldResults() {
  const h = harness(); const resolvers = [], updates = [];
  const poller = new h.Poller({read: () => new Promise(resolve => resolvers.push(resolve)), valid: () => true,
    update: (chat, item) => updates.push(item.request_key), failed: assert.fail, interval: 10});
  poller.start('old'); const oldRead = h.tick(); poller.start('new');
  resolvers.shift()({turns: [{request_key: 'old', status: 'completed'}]}); await oldRead;
  assert.deepEqual(updates, []);
  const newRead = h.tick(); resolvers.shift()({turns: [{request_key: 'new', status: 'working'}]});
  h.advance(165001); await newRead;
  assert.deepEqual(updates, ['new']);
  assert.equal(h.timers.size, 0, 'The observation window is bounded and cannot loop indefinitely');
}

(async () => {
  for (const test of [showsOnlyObservedToolsAndTruthfulResults, preservesCallOrderAndRepeatedCallsFromDurableTrace,
    githubObservationsUseActualStatusAndNeverExposeSourceContents,
    finalTraceSupersedesAnEarlierPartialProgressSnapshot, completedChatRerenderPreservesCollapsedDisclosure,
    handlesFailuresAndNeverTreatsUnknownOrRunningAsSuccessful, unsafeSourceTextIsNotHTMLAndDetailsStayCollapsed,
    pollingDoesNotOverlapAndStopsAtTerminalTurn, staleAndStoppedReadsCannotUpdateOrReschedule,
    transientReadFailurePreservesDisplayAndNeverResubmitsMessage, restartAndDeadlineDiscardOldResults]) {
    await test(); console.log('PASS', test.name);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
