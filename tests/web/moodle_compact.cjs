'use strict';
// Exercise the shipped renderer and task-status event with browser primitives doubled.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const fixedNow = Date.parse('2026-10-22T15:30:00Z');
const dataKey = name => name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
class Element {
  constructor(tag = 'div') {
    this.tag = this.tagName = tag; this.children = []; this.parentElement = null;
    this.attributes = {}; this.dataset = {}; this.events = {}; this.className = '';
    this.text = ''; this.hidden = false; this.disabled = false; this.open = false;
    this.classList = {
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled = force === undefined ? !names.has(name) : force;
        if (enabled) names.add(name); else names.delete(name);
        this.className = [...names].join(' '); return enabled;
      },
      remove: name => this.classList.toggle(name, false),
      add: name => this.classList.toggle(name, true),
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  set textContent(value) {this.text = String(value); this.children = [];}
  get textContent() {return this.text + this.children.map(child => child.textContent ?? String(child)).join(' ');}
  set innerHTML(_) {throw new Error('Untrusted task text must never be parsed as HTML');}
  set disabled(value) {
    this._disabled = !!value;
    const document = this.document();
    if (this._disabled && document?.activeElement === this) document.activeElement = document.body;
  }
  get disabled() {return this._disabled;}
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'id') this.id = String(value);
    if (name.startsWith('data-')) this.dataset[dataKey(name)] = String(value);
    if (name === 'open') this.open = true;
    if (name === 'hidden') this.hidden = true;
  }
  getAttribute(name) {
    if (name === 'id') return this.id ?? null;
    if (name.startsWith('data-')) return this.dataset[dataKey(name)] ?? null;
    return this.attributes[name] ?? null;
  }
  removeAttribute(name) {delete this.attributes[name]; if (name === 'open') this.open = false;}
  replaceChildren(...children) {
    const document = this.document();
    if (document && this.children.some(child => typeof child === 'object' && child.contains(document.activeElement)))
      document.activeElement = document.body;
    for (const child of this.children) if (typeof child === 'object') child.parentElement = null;
    this.text = ''; this.children = []; this.append(...children);
  }
  append(...children) {for (const child of children) {if (typeof child === 'object') child.parentElement = this; this.children.push(child);}}
  contains(element) {return this === element || this.children.some(child => typeof child === 'object' && child.contains(element));}
  document() {return this.ownerDocument || this.parentElement?.document() || null;}
  focus() {const document = this.document(); if (document && !this.disabled) document.activeElement = this;}
  addEventListener(name, handler) {(this.events[name] ||= []).push(handler);}
  async fire(name, event = {}) {for (const handler of this.events[name] || []) await handler({target: this, ...event});}
  matches(selector) {
    if (selector === '*') return true;
    const tag = selector.match(/^[a-z][a-z0-9]*/i)?.[0];
    if (tag && tag !== this.tag) return false;
    const id = selector.match(/#([\w-]+)/)?.[1];
    if (id && id !== this.id) return false;
    const classes = [...selector.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
    if (classes.some(name => !this.classList.contains(name))) return false;
    for (const match of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
      const [_, attribute, expected] = match;
      const value = attribute === 'open' ? this.open ? '' : null : this.getAttribute(attribute);
      if (value === null || expected !== undefined && value !== expected) return false;
    }
    return true;
  }
  closest(selector) {
    return selector.split(',').some(part => this.matches(part.trim())) ? this : this.parentElement?.closest(selector) || null;
  }
  querySelectorAll(selector) {
    const descendants = this.children.filter(child => typeof child === 'object')
      .flatMap(child => [child, ...child.querySelectorAll('*')]);
    return descendants.filter(child => selector.split(',').some(part => child.matches(part.trim())));
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
}

function rendererHarness(source, {enabled = true} = {}) {
  const root = new Element('main'), nodes = new Map(), requests = [];
  for (const id of ['moodle', 'moodle-heading', 'moodle-tasks', 'moodle-courses', 'moodle-status',
    'moodle-filter-upcoming', 'moodle-filter-past', 'moodle-filter-completed']) {
    const element = new Element(id.includes('filter-') ? 'button' : 'div');
    element.id = id;
    if (id.includes('filter-')) element.dataset.moodleGroup = id.split('filter-')[1];
    root.append(element); nodes.set(id, element);
  }
  const $ = id => nodes.get(id) || root.querySelector('#' + id);
  const sourceDetails = new Element('details'), sourceStatus = new Element('p');
  sourceStatus.id = 'moodle-source-status'; sourceDetails.hidden = true;
  sourceDetails.append(sourceStatus); root.append(sourceDetails); nodes.set(sourceStatus.id, sourceStatus);
  const body = new Element('body'); body.dataset.timezone = 'Asia/Qyzylorda';
  const document = {
    body, activeElement: body,
    createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    querySelector: selector => root.querySelector(selector), querySelectorAll: selector => root.querySelectorAll(selector),
  };
  root.ownerDocument = document;
  body.ownerDocument = document;
  const sourceBetween = (start, end) => {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert(from >= 0 && to > from, 'Shipped renderer boundary exists: ' + start);
    return source.slice(from, to);
  };
  const sandbox = {document, $, URL, Date: class extends Date {static now() {return fixedNow;}},
    async api(url, payload) {
      requests.push({url, payload});
      if (url !== '/api/moodle/task-status') throw new Error('Unexpected mutation: ' + url);
      const state = structuredClone(sandbox.currentState);
      const task = state.snapshot.tasks.find(item => item.id === payload.task_id);
      task.local_status = payload.done ? 'done' : 'active';
      task.view_group = payload.done ? 'completed' : Date.parse(task.due_at) <= fixedNow ? 'past' : 'upcoming';
      sandbox.currentState = state;
      return {json: async () => state};
    }};
  vm.createContext(sandbox);
  vm.runInContext(`let moodleEnabled=${enabled}, moodleGroup='upcoming', scope='synthetic-owner', moodleRevision=1;
    ${sourceBetween('  function clearMoodle(', '  async function loadMoodle(')}`, sandbox);
  return {root, nodes, $, document, requests, sandbox,
    render(state) {sandbox.currentState = structuredClone(state); sandbox.inputState = state; vm.runInContext('showMoodle(inputState)', sandbox);},
    run(code) {return vm.runInContext(code, sandbox);},
    installFilters() {
      const marker = "  if (moodleEnabled) {\n    for (const name";
      const from = source.indexOf(marker), to = source.indexOf("    $('moodle-refresh').addEventListener", from);
      assert(from >= 0 && to > from, 'Shipped filter listeners exist');
      vm.runInContext(source.slice(from, to) + '\n}', sandbox);
    },
    installInteractions() {
      const from = source.indexOf("    $('moodle-tasks').addEventListener('click',");
      const to = source.indexOf("\n  }\n  $('panel')", from);
      assert(from >= 0 && to > from, 'Shipped local completion handler exists');
      vm.runInContext(source.slice(from, to), sandbox);
    },
  };
}

const task = (id, changes = {}) => ({id, title: `Assignment ${id}`, course_title: 'WEB Technologies',
  description: 'Do the assignment.', due_at: '2026-10-24T18:59:00Z', submission_status: 'unknown',
  url: null, local_status: 'active', view_group: 'upcoming', ...changes});
const feed = tasks => ({connected: true, source_kind: 'calendar_feed', stale: false, syncing: false,
  last_success: '2026-10-22T15:00:00Z', snapshot: {fetched_at: '2026-10-22T15:00:00Z', courses: [], tasks}});
const rows = (h, group) => h.$('moodle-group-' + group).querySelectorAll('li.moodle-task-row');

const scenarios = {
  async grouping(source) {
    const h = rendererHarness(source); h.installFilters();
    h.render(feed([task('future'), task('late', {due_at: '2026-10-21T18:59:00Z', view_group: 'past'}),
      task('derived-late', {due_at: '2026-10-20T18:59:00Z', view_group: undefined}),
      task('done', {view_group: 'completed', local_status: 'done'})]));
    assert.equal(rows(h, 'upcoming').length, 1); assert.equal(rows(h, 'past').length, 2); assert.equal(rows(h, 'completed').length, 1);
    assert.equal(h.$('moodle-group-upcoming').hidden, false); assert.equal(h.$('moodle-group-past').hidden, true);
    await h.$('moodle-filter-past').fire('click');
    assert.equal(h.$('moodle-group-past').hidden, false); assert.equal(h.$('moodle-group-upcoming').hidden, true);
    assert.equal(h.$('moodle-filter-past').getAttribute('aria-pressed'), 'true');
    assert.equal(h.$('moodle-filter-upcoming').getAttribute('aria-pressed'), 'false');
    const selected = h.run('moodleGroup'); h.run("selectMoodleGroup('not-a-real-group')");
    assert.equal(h.run('moodleGroup'), selected, 'Invalid selectors cannot blank the selected task group');
    const row = rows(h, 'past')[0], description = row.querySelector('details'); description.open = true;
    h.render(feed([task('late', {due_at: '2026-10-21T18:59:00Z', view_group: 'past', description: 'Updated body'})]));
    assert.equal(h.$('moodle-group-past').hidden, false, 'Refresh retains the selected category');
    assert.equal(rows(h, 'past')[0].querySelector('details').open, true, 'Refresh does not close the description being read');
  },
  feedTruth(source) {
    const h = rendererHarness(source);
    h.render(feed([task('late', {due_at: '2026-10-21T18:59:00Z', view_group: 'past'}),
      task('done', {view_group: 'completed', local_status: 'done'})]));
    const late = rows(h, 'past')[0], done = rows(h, 'completed')[0];
    assert.match(late.textContent, /статус сдачи неизвестен/i);
    assert.doesNotMatch(late.textContent, /не сдано/i, 'Passing the due date cannot invent Moodle submission status');
    assert.match(done.textContent, /ваша отметка/i);
    assert.doesNotMatch(done.textContent, /(?:^|\s)(?:Сдано|Оценено|Не сдано)(?:\s|$)/i,
      'Local completion cannot claim a Moodle submission');
    assert.match(h.$('moodle-source-status').textContent, /оценки и статусы сдачи недоступны/i);
    assert.equal(h.$('moodle-source-status').parentElement.hidden, false);
    assert.equal(h.$('moodle-status').textContent, '', 'Routine provenance stays in the source disclosure');
    for (const [row, pressed] of [[late, 'false'], [done, 'true']]) {
      const button = row.querySelector('button[data-task-id]');
      assert.equal(button.getAttribute('aria-pressed'), pressed);
      assert(button.getAttribute('aria-label').includes(row.querySelector('h3').textContent), 'Completion is labelled for its specific assignment');
    }
  },
  deadlinesAndText(source) {
    const h = rendererHarness(source), hostile = '<img src=x onerror=secret()> ' + 'Long assignment '.repeat(25);
    h.render(feed([task('safe', {title: hostile, course_title: '<script>course()</script>', description: '<script>secret()</script>',
      due_at: '2026-10-24T18:59:00Z', url: 'javascript:alert(1)'})]));
    const row = rows(h, 'upcoming')[0], time = row.querySelector('time');
    assert.equal(row.querySelector('h3').textContent, hostile, 'Long titles remain complete text');
    assert.match(row.textContent, /<script>secret\(\)<\/script>/);
    assert.equal(row.querySelector('script'), null); assert.equal(row.querySelector('img'), null);
    assert.equal(row.querySelector('a'), null);
    assert.equal(time.dateTime || time.getAttribute('datetime'), '2026-10-24T18:59:00Z');
    assert.match(time.textContent, /24/); assert.match(time.textContent, /23:59/, 'Deadline is displayed in the configured UTC+5 timezone');
    assert.equal(h.run("moodleLink('https://lms.astanait.edu.kz/mod/assign/view.php?id=9')"), 'https://lms.astanait.edu.kz/mod/assign/view.php?id=9');
    for (const bad of ['https://lms.astanait.edu.kz.evil.test/mod/assign/view.php?id=9',
      'https://lms.astanait.edu.kz/mod/assign/view.php?id=9&token=secret',
      'https://lms.astanait.edu.kz/mod/assign/view.php?id=9&id=10',
      'https://lms.astanait.edu.kz/mod/assign/view.php?id=9#private',
      'https://user:secret@lms.astanait.edu.kz/mod/assign/view.php?id=9']) {
      h.sandbox.bad = bad; assert.equal(h.run('moodleLink(bad)'), null);
    }
  },
  restScoresAndSourceLinks(source) {
    const h = rendererHarness(source), state = feed([task('assign:9', {submission_status: 'graded', grade_value: 0,
      grade_max: 10, due_at: null, url: 'https://lms.astanait.edu.kz/mod/assign/view.php?id=9'})]);
    state.source_kind = 'rest'; state.snapshot.courses = [{title: 'Course', grade_value: 0, grade_max: 100}];
    h.render(state);
    const row = rows(h, 'upcoming')[0];
    assert.match(row.textContent, /0 \/ 10/); assert.match(row.textContent, /Оценено/);
    assert.match(row.textContent, /не указан/i);
    assert.match(h.$('moodle-courses').textContent, /0 \/ 100/);
    assert.equal(row.querySelector('button[data-task-id]'), null, 'REST data cannot invent support for local calendar-feed completion');
    const link = row.querySelector('a'); assert.equal(link.href, state.snapshot.tasks[0].url);
    assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer');
    state.snapshot.tasks[0].grade_value = null; state.snapshot.tasks[0].grade_max = null; h.render(state);
    assert.match(rows(h, 'upcoming')[0].textContent, /Ещё не оценено/);
    assert.doesNotMatch(rows(h, 'upcoming')[0].textContent, /0 \/ 10/);
  },
  async localCompletionAndRestore(source) {
    for (const group of ['upcoming', 'past']) {
      const h = rendererHarness(source); h.installInteractions(); h.installFilters();
      h.render(feed([task('uid-with:punctuation@example', {view_group: group,
        due_at: group === 'past' ? '2026-10-21T18:59:00Z' : '2026-10-24T18:59:00Z'})]));
      await h.$('moodle-filter-' + group).fire('click');
      const button = rows(h, group)[0].querySelector('button[data-task-id]');
      await h.$('moodle-tasks').fire('click', {target: button});
      assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, '/api/moodle/task-status');
      assert.deepEqual(JSON.parse(JSON.stringify(h.requests[0].payload)), {task_id: 'uid-with:punctuation@example', done: true});
      assert.equal(rows(h, group).length, 0); assert.equal(rows(h, 'completed').length, 1);
      assert.equal(h.sandbox.currentState.snapshot.tasks[0].submission_status, 'unknown');
      await h.$('moodle-filter-completed').fire('click');
      const restore = rows(h, 'completed')[0].querySelector('button[data-task-id]');
      await h.$('moodle-tasks').fire('click', {target: restore});
      assert.equal(h.requests.length, 2); assert.equal(h.requests[1].payload.done, false);
      assert.equal(rows(h, 'completed').length, 0); assert.equal(rows(h, group).length, 1, 'Restoring preserves its actual deadline category');
      assert.equal(h.$('moodle-group-completed').hidden, false, 'Mutation retains the user-selected category');
      assert(h.requests.every(request => request.url === '/api/moodle/task-status'), 'Task completion never calls a Calendar endpoint');
    }
  },
  async focusRecovery(source) {
    const h = rendererHarness(source); h.installInteractions(); h.installFilters();
    h.render(feed([task('first'), task('second')]));
    const first = rows(h, 'upcoming')[0].querySelector('button[data-task-id]'); first.focus();
    await h.$('moodle-tasks').fire('click', {target: first});
    const next = rows(h, 'upcoming')[0].querySelector('button[data-task-id]');
    assert.equal(h.document.activeElement, h.$('moodle-filter-upcoming'),
      'Completion moves focus to a stable category before disabling the focused button');
    assert.equal(next.dataset.taskId, 'second');
    next.focus();
    await h.$('moodle-tasks').fire('click', {target: next});
    assert.equal(h.document.activeElement, h.$('moodle-filter-upcoming'), 'Completing the last row focuses its category');
    await h.$('moodle-filter-completed').fire('click');
    const restoreFirst = rows(h, 'completed')[0].querySelector('button[data-task-id]'); restoreFirst.focus();
    await h.$('moodle-tasks').fire('click', {target: restoreFirst});
    const restoreLast = rows(h, 'completed')[0].querySelector('button[data-task-id]');
    assert.equal(h.document.activeElement, h.$('moodle-filter-completed'), 'Restore also retains stable focus through native button disable');
    restoreLast.focus();
    await h.$('moodle-tasks').fire('click', {target: restoreLast});
    assert.equal(h.document.activeElement, h.$('moodle-filter-completed'), 'Restoring the last row focuses its category');
    await h.$('moodle-filter-upcoming').fire('click');
    rows(h, 'upcoming')[0].querySelector('button[data-task-id]').focus();
    h.render(feed([task('first', {local_status: 'done', view_group: 'completed'}), task('second')]));
    assert.equal(h.document.activeElement, rows(h, 'upcoming')[0].querySelector('button[data-task-id]'),
      'A background refresh moving the focused task focuses the next visible row');
    h.render(feed([task('first', {local_status: 'done', view_group: 'completed'}),
      task('second', {local_status: 'done', view_group: 'completed'})]));
    assert.equal(h.document.activeElement, h.$('moodle-filter-upcoming'),
      'A background refresh removing the last focused row falls back to its category');
  },
  async refreshFocusAndNoSteal(source) {
    const h = rendererHarness(source); h.installInteractions();
    const state = feed([task('reading', {url: 'https://lms.astanait.edu.kz/mod/assign/view.php?id=9'})]);
    h.render(state);
    for (const selector of ['summary', 'a']) {
      const row = rows(h, 'upcoming')[0], oldControl = row.querySelector(selector);
      row.querySelector('details').open = true; oldControl.focus();
      state.snapshot.tasks[0].description += ' New source text.'; h.render(state);
      const newControl = rows(h, 'upcoming')[0].querySelector(selector);
      assert.notEqual(newControl, oldControl, 'The renderer actually replaced this control');
      assert.equal(h.document.activeElement, newControl, `Refreshing retains focus on the same task ${selector}`);
      assert.equal(rows(h, 'upcoming')[0].querySelector('details').open, true);
    }
    const focused = rows(h, 'upcoming')[0].querySelector('button[data-task-id]'); focused.focus();
    let release; const waiting = new Promise(resolve => {release = resolve;});
    const originalApi = h.sandbox.api; h.sandbox.api = async (...args) => {await waiting; return originalApi(...args);};
    const marking = h.$('moodle-tasks').fire('click', {target: focused});
    assert.equal(h.document.activeElement, h.$('moodle-filter-upcoming'), 'A pending save has a stable focus target');
    const outside = new Element('textarea'); outside.id = 'unrelated-composer'; h.root.append(outside); outside.focus();
    release(); await marking;
    assert.equal(h.document.activeElement, outside, 'An asynchronous completion cannot steal focus after the user leaves the task list');
    h.render(state);
    assert.equal(h.document.activeElement, outside, 'Background refresh cannot steal focus from another part of the page');
  },
  emptyUnavailableAndDisabled(source) {
    const h = rendererHarness(source); h.render(feed([]));
    for (const name of ['upcoming', 'past', 'completed']) {
      h.run(`selectMoodleGroup('${name}')`);
      const group = h.$('moodle-group-' + name);
      assert.equal(group.hidden, false); assert(group.textContent.trim(), 'Every category explains its empty state');
      assert.equal(rows(h, name).length, 0);
    }
    const state = feed([task('saved')]); state.error = 'unavailable'; state.stale = true; h.render(state);
    assert.match(h.$('moodle-status').textContent, /ранее полученные данные/);
    assert.equal(rows(h, 'upcoming').length, 1);
    state.snapshot = null; h.render(state);
    assert.equal(h.$('moodle-tasks').querySelectorAll('li.moodle-task-row').length, 0);
    assert.match(h.$('moodle-status').textContent, /пока не получены/);
    h.render(feed([task('private')])); h.run("clearMoodle('Войдите в приложение')");
    assert.equal(h.$('moodle-tasks').querySelectorAll('li.moodle-task-row').length, 0);
    assert.equal(h.$('moodle-status').textContent, 'Войдите в приложение');
    assert.equal(h.$('moodle-source-status').textContent, '');
    assert.equal(h.$('moodle-source-status').parentElement.hidden, true, 'Signing out removes private source metadata too');
    const disabled = rendererHarness(source, {enabled: false});
    disabled.installFilters();
    disabled.run("clearMoodle('Should not render')");
    assert.equal(disabled.$('moodle-status').textContent, '');
    assert.equal(disabled.$('moodle-filter-past').events.click, undefined);
  },
};

module.exports = {rendererHarness, Element};
if (require.main === module) {
  const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/client.py'), 'utf8').replace(/\r\n/g, '\n');
  const selected = process.argv[3] ? {[process.argv[3]]: scenarios[process.argv[3]]} : scenarios;
  (async () => {for (const [name, scenario] of Object.entries(selected)) {assert(scenario, 'Known scenario'); await scenario(source); console.log('PASS', name);}})()
    .catch(error => {console.error(error); process.exitCode = 1;});
}
