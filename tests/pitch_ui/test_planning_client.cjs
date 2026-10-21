'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/recovery_planning.py'), 'utf8');
const script = source.split("SCRIPT = r'''")[1].split("'''", 1)[0];
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for (let i = 0; i < 12; ++i) await tick(); }

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.value = ''; this.children = []; this.listeners = {};
    this.disabled = false; this.hidden = false; this.textContent = ''; this.attrs = {};
  }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(name, value) { this.attrs[name] = value; }
  append(...items) {
    this.children.push(...items);
    if (this.tagName === 'select' && !this.value) this.value = items.find(item => item.value)?.value || '';
  }
  replaceChildren(...items) { this.children = []; if (this.tagName === 'select') this.value = ''; this.append(...items); }
  querySelector() { return this.submit; }
}

async function fixture(options = {}) {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id === 'planning-task' ? 'select' : 'div'));
    return nodes.get(id);
  };
  for (const id of ['planning-status', 'planning-reload', 'effort-form', 'windows-form', 'planning-task',
      'planning-minutes', 'planning-start', 'planning-end', 'planning-add', 'planning-zone',
      'planning-windows', 'planning-empty', 'planning-windows-empty']) $(id);
  $('effort-form').submit = new Element('button');
  $('windows-form').submit = new Element('button');
  const calls = [], location = {replace: value => { location.redirect = value; }};
  let state = {revision: 0, updated_at: null, estimates: [], working_windows: null};
  let hold = null, failLoad = false;
  const response = (status, value) => ({status, ok: status >= 200 && status < 300, json: async () => structuredClone(value)});
  const fetch = async (url, init = {}) => {
    calls.push({url, ...init});
    if (url === '/auth/session') return response(200, {status: 'authenticated',
      session_id: options.changedSession && init.headers?.['X-CSRF-Bootstrap'] ? 'other-session' : 'session-one',
      run_id: 'session-run', expires_at: '2026-10-22T00:00:00Z',
      csrf_token: options.invalidToken ? 'bad token' : 'synthetic_csrf_token_0123456789'});
    if (url === '/api/moodle/planning') {
      if (init.method === 'POST') {
        if (options.hold) await new Promise(resolve => { hold = resolve; });
        if (options.uncertain) throw new TypeError('network response lost');
        const value = JSON.parse(init.body);
        state = {...state, revision: state.revision + 1,
          ...(value.estimates ? {estimates: value.estimates} : {}),
          ...('working_windows' in value ? {working_windows: value.working_windows} : {})};
        return response(200, state);
      }
      if (failLoad) return response(503, {error: 'unavailable'});
      return response(200, state);
    }
    if (url === '/api/moodle') return response(200, {connected: true, source_kind: 'calendar_feed',
      snapshot: {tasks: options.empty ? [] : [{id: 'homework@lms', title: 'Homework', course_title: 'Course', local_status: 'todo'}]}});
    throw Error('Unexpected URL: ' + url);
  };
  const document = {
    cookie: '', getElementById: $, createElement: tag => new Element(tag),
    querySelectorAll: () => [...nodes.values(), $('effort-form').submit, $('windows-form').submit],
  };
  vm.runInNewContext(script, {document, fetch, location, Intl, Date, Number, Error, AbortSignal,
    URLSearchParams, setTimeout, clearTimeout});
  await settle();
  const fire = (id, event) => $(id).listeners[event]({preventDefault() {}});
  return {$, calls, fire, location, release: () => hold?.(), failLoad: () => { failLoad = true; }};
}

(async () => {
  const selected = process.argv[3];
  const posts = h => h.calls.filter(call => call.method === 'POST');
  if (selected === 'csrf') {
    const h = await fixture();
    h.$('planning-minutes').value = '90'; h.fire('effort-form', 'submit'); await settle();
    assert.equal(posts(h).length, 1, 'valid session without a readable CSRF cookie can save');
    assert.equal(posts(h)[0].headers['X-CSRF-Token'], 'synthetic_csrf_token_0123456789');
    assert.deepEqual(JSON.parse(posts(h)[0].body), {expected_revision: 0,
      estimates: [{task_id: 'homework@lms', remaining_minutes: 90}]});
    assert(h.calls.some(call => call.url === '/auth/session' && call.headers?.['X-CSRF-Bootstrap'] === '1'));
  } else if (selected === 'changed_session' || selected === 'invalid_token') {
    const h = await fixture({changedSession: selected === 'changed_session', invalidToken: selected === 'invalid_token'});
    h.$('planning-minutes').value = '90'; h.fire('effort-form', 'submit'); await settle();
    assert.equal(posts(h).length, 0, 'session/token mismatch cannot send a planning mutation');
    assert(h.calls.some(call => call.url === '/auth/session' && call.headers?.['X-CSRF-Bootstrap'] === '1'));
  } else if (selected === 'quarter_hour') {
    const h = await fixture();
    h.$('planning-start').value = '2026-10-22T10:05'; h.$('planning-end').value = '2026-10-22T11:00';
    h.fire('planning-add', 'click');
    assert.equal(h.$('planning-windows').children.length, 0, 'invalid 15-minute grid never enters the draft');
    h.$('planning-start').value = '2026-10-22T10:00'; h.fire('planning-add', 'click');
    assert.equal(h.$('planning-windows').children.length, 1);
    h.fire('planning-add', 'click'); assert.equal(h.$('planning-windows').children.length, 1, 'repeat add cannot duplicate');
  } else if (selected === 'invalid_effort') {
    const h = await fixture();
    h.$('planning-minutes').value = '1.5'; h.fire('effort-form', 'submit'); await settle();
    assert.equal(posts(h).length, 0, 'fractional effort cannot reach the strict integer API');
    assert.match(h.$('planning-status').textContent, /цел|минут/);
  } else if (selected === 'empty_feed') {
    const h = await fixture({empty: true});
    assert.equal(h.$('effort-form').submit.disabled, true);
    assert.equal(h.$('planning-minutes').disabled, true, 'effort editing requires an existing task');
    assert.equal(h.$('planning-empty').hidden, false, 'empty source has an explicit visible explanation');
    assert.equal(h.$('windows-form').submit.disabled, false, 'availability can still be saved');
  } else if (selected === 'double_save') {
    const h = await fixture({hold: true});
    h.$('planning-minutes').value = '90'; h.fire('effort-form', 'submit'); await settle();
    h.fire('effort-form', 'submit'); await settle();
    assert.equal(posts(h).length, 1, 'repeat submit never dispatches another save while busy');
    assert.equal(h.$('effort-form').submit.disabled, true);
    h.release(); await settle(); assert.equal(h.$('effort-form').submit.disabled, false);
  } else if (selected === 'uncertain_save') {
    const h = await fixture({uncertain: true});
    h.$('planning-minutes').value = '90'; h.fire('effort-form', 'submit'); await settle();
    assert.equal(posts(h).length, 1);
    assert.equal(h.$('effort-form').submit.disabled, true, 'lost save response requires reread before another mutation');
    assert.equal(h.$('planning-minutes').value, '90', 'the draft remains reviewable after uncertainty');
    h.fire('effort-form', 'submit'); await settle(); assert.equal(posts(h).length, 1);
    h.fire('planning-reload', 'click'); await settle(); assert.equal(h.$('effort-form').submit.disabled, false);
  } else if (selected === 'reload_failure') {
    const h = await fixture(); h.failLoad(); h.fire('planning-reload', 'click'); await settle();
    assert.equal(h.$('effort-form').submit.disabled, true, 'a failed reread cannot leave old revision writable');
    assert.equal(h.$('planning-reload').disabled, false);
  } else throw Error('Unknown case: ' + selected);
})().catch(error => { console.error(error); process.exitCode = 1; });
