// Run actual startup, session/CSRF bootstrap, source rendering, and refresh code.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(process.argv[2], 'src/campuspulse_recovery/web/assets/client.py'), 'utf8').replace(/\r\n/g, '\n');
function between(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert(from >= 0 && to > from, `Missing shipped boundary: ${start}`);
  return source.slice(from, to);
}
const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
const settle = async () => {for (let index = 0; index < 30; ++index) await Promise.resolve();};
class Element {
  constructor() {this.children = []; this.attributes = {}; this.dataset = {}; this.textContent = ''; this.hidden = false;
    this.classList = {remove() {}}; this.events = {};}
  addEventListener(name, callback) {this.events[name] = callback;}
  setAttribute(name, value) {this.attributes[name] = value;}
  replaceChildren(...children) {this.children = children;}
  append(...children) {this.children.push(...children);}
}
function harness({failAuth = false, scope = null} = {}) {
  const auth = deferred(), moodle = deferred(), state = deferred(), elements = new Map(), requests = [], failures = [];
  let chatLoads = 0;
  const $ = id => {if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id);};
  const session = {session_id: 'synthetic-alice', csrf_token: 'synthetic_session_token_123456789'};
  const snapshot = {connected: true, source_kind: 'rest', snapshot: {
    fetched_at: '2026-10-22T14:00:00Z', courses: [], tasks: []}};
  const sandbox = {$, URLSearchParams, AbortSignal, Event: class {},
    window: {dispatchEvent() {}}, document: {body: {dataset: {timezone: 'Asia/Qyzylorda'}}, createElement: () => new Element()},
    location: {search: '', hash: ''}, history: {replaceState() {}},
    DOMParser: class {parseFromString() {return {body: {childNodes: [new Element()]}};}},
    chats: {reset() {}, async load() {++chatLoads;}}, clearDrafts() {}, restoreDraft() {}, stored() {return null;},
    updateNavigation() {}, async loadChat() {++chatLoads;}, controls() {}, feedback(message, failed) {if (failed) failures.push(message);},
    async fetch(url, options) {
      requests.push({url, method: options.method || 'GET'});
      if (url === '/auth/session') {
        if (requests.filter(item => item.url === url).length === 1) await auth.promise;
        return failAuth ? {ok: false, status: 403} : {ok: true, status: 200, json: async () => session};
      }
      if (url === '/api/moodle') {await moodle.promise; return {ok: true, status: 200, json: async () => snapshot};}
      if (url === '/api/moodle/sync') return {ok: true, status: 200, json: async () => snapshot};
      if (url.startsWith('/ui/state?')) {await state.promise; return {ok: true, status: 200, text: async () => '<main></main>'};}
      throw new Error(`Unexpected browser request ${url}`);
    }};
  vm.createContext(sandbox);
  vm.runInContext(`let scope=${JSON.stringify(scope)}, generation=0, moodleEnabled=true, moodleBusy=false,
    moodleRevision=0,cursor=0,activeRun=null,pendingText=null,activeChat='default',chatAvailable=false,
    chatWorking=false,chatLimitReached=false,chatGeneration=0;const chatDrafts=new Map();
    ${between('  const csrf = ', '  function stored(')}
    ${between('  async function refresh()', '  async function act(')}`, sandbox);
  const startup = source.slice(source.lastIndexOf("  $('signout').addEventListener('click', signOut);"))
    .split("  $('signout').addEventListener('click', signOut);")[1].split('})();')[0];
  return {sandbox, $, requests, failures, auth, moodle, state, session,
    get chatLoads() {return chatLoads;},
    start() {vm.runInContext(startup, sandbox);},
    installTaskStatus() {vm.runInContext(between("    $('moodle-tasks').addEventListener('click',", "\n  }\n  $('panel')"), sandbox);},
    run(value) {return vm.runInContext(value, sandbox);}};
}

async function noRequestOrSourceSyncBeforeSessionBootstrap() {
  const h = harness();
  await h.run('loadMoodle()');
  assert.equal(h.requests.length, 0, 'An uninitialised scope cannot begin a source sync');
  h.start(); await settle();
  assert.deepEqual(h.requests, [{url: '/auth/session', method: 'GET'}]);
  assert.equal(h.$('panel').attributes['aria-busy'], 'true');
  assert.equal(h.$('moodle').attributes['aria-busy'], undefined, 'No spurious source spinner before authentication');
  h.auth.resolve(); await settle();
  assert.equal(h.run('scope'), h.session.session_id);
  assert(h.requests.some(item => item.url === '/api/moodle'));
  assert(h.requests.some(item => item.url.startsWith('/ui/state?')), 'State rendering starts independently of Moodle latency');
  assert(!h.requests.some(item => item.url === '/api/moodle/sync'));
  h.moodle.resolve(); await settle();
  assert(h.requests.some(item => item.url === '/api/moodle/sync' && item.method === 'POST'));
  assert.match(h.$('moodle-status').textContent, /Последнее обновление/);
  assert(!h.$('moodle-status').textContent.includes('Войдите'));
  assert.equal(h.$('moodle').attributes['aria-busy'], 'false');
  assert.equal(h.chatLoads, 0, 'Moodle can finish while a slower UI/state response is still in flight');
  h.state.resolve(); await settle(); assert.equal(h.chatLoads, 2);
  assert.deepEqual(h.failures, []);
}

async function earlyMoodleReadinessAndSlowMoodleBothLeaveAuthenticatedData() {
  for (const sourceReadyFirst of [true, false]) {
    const h = harness(); h.start();
    if (sourceReadyFirst) h.moodle.resolve();
    await settle();
    assert.deepEqual(h.requests, [{url: '/auth/session', method: 'GET'}], 'Even a ready source waits for scoped authentication');
    h.auth.resolve(); h.state.resolve(); await settle();
    assert.equal(h.chatLoads, 2, 'A slow source does not block chat or panel startup');
    if (!sourceReadyFirst) {
      assert.equal(h.$('moodle').attributes['aria-busy'], 'true');
      assert.equal(h.$('moodle-refresh').disabled, true);
      h.moodle.resolve();
    }
    await settle();
    assert.match(h.$('moodle-status').textContent, /Последнее обновление/);
    assert.equal(h.requests.filter(item => item.url === '/api/moodle/sync').length, 1);
    assert.equal(h.$('moodle-refresh').disabled, false);
  }
}

async function rejectedAuthenticationClearsStaleTasksWithoutFetchingSources() {
  const h = harness({failAuth: true, scope: 'previous-session'});
  h.$('moodle-tasks').append('Previous account private task');
  h.start(); h.auth.resolve(); await settle();
  assert.equal(h.run('scope'), null);
  assert.deepEqual(h.$('moodle-tasks').children, []);
  assert.match(h.$('moodle-status').textContent, /Войдите/);
  assert.equal(h.$('moodle').attributes['aria-busy'], 'false');
  assert.equal(h.$('moodle-refresh').disabled, true);
  assert.equal(h.requests.some(item => item.url.startsWith('/api/moodle')), false);
  assert.equal(h.requests.some(item => item.method === 'POST'), false, 'Rejected auth must never dispatch source sync');
}

async function sameSessionRefreshKeepsExistingContentUntilFreshResponsesArrive() {
  const h = harness({scope: 'synthetic-alice'}), oldPanel = new Element(), oldTasks = new Element();
  h.$('panel').append(oldPanel); h.$('moodle-tasks').append(oldTasks);
  h.start(); await settle();
  assert.equal(h.$('panel').children[0], oldPanel, 'Refreshing should retain the current panel while checking the session');
  assert.equal(h.$('panel').attributes['data-stale'], 'true');
  h.auth.resolve(); await settle();
  assert.equal(h.$('panel').children[0], oldPanel, 'A slow state projection should not blank existing UI');
  assert.equal(h.$('moodle-tasks').children[0], oldTasks, 'Same-session source refresh retains cached content while loading');
  assert.equal(h.$('moodle').attributes['aria-busy'], 'true');
  assert.equal(h.$('moodle-refresh').disabled, true);
  h.moodle.resolve(); await settle();
  assert.equal(h.$('moodle').attributes['aria-busy'], 'false');
  assert.match(h.$('moodle-status').textContent, /Последнее обновление/);
  h.state.resolve(); await settle();
  assert.equal(h.$('panel').attributes['data-stale'], 'false');
  assert.notEqual(h.$('panel').children[0], oldPanel);
}

async function inFlightOldSessionSourceCannotReturnAfterAuthenticationFailure() {
  const h = harness({failAuth: true, scope: 'previous-session'});
  const oldRead = h.run('loadMoodle()');
  assert.equal(h.$('moodle').attributes['aria-busy'], 'true');
  h.start(); h.auth.resolve(); await settle();
  h.moodle.resolve(); await oldRead; await settle();
  assert.match(h.$('moodle-status').textContent, /Войдите/);
  assert.deepEqual(h.$('moodle-tasks').children, []);
  assert.equal(h.requests.some(item => item.url === '/api/moodle/sync'), false);
  assert.equal(h.$('moodle').attributes['aria-busy'], 'false');
}

async function olderAuthResponseCannotReplaceNewerSessionOrRestartItsSources() {
  const h = harness(), older = deferred(), newer = deferred(), originalFetch = h.sandbox.fetch;
  const previousSession = {session_id: 'synthetic-older', csrf_token: 'synthetic_older_token_123456789'};
  const currentSession = {session_id: 'synthetic-newer', csrf_token: 'synthetic_newer_token_123456789'};
  let authReads = 0;
  h.sandbox.fetch = async (url, options) => {
    if (url !== '/auth/session') return originalFetch(url, options);
    h.requests.push({url, method: options.method || 'GET'});
    const index = ++authReads;
    if (index === 1) await older.promise;
    if (index === 2) await newer.promise;
    return {ok: true, status: 200, json: async () => index === 1 ? previousSession : currentSession};
  };
  const first = h.run('refresh()'), second = h.run('refresh()');
  newer.resolve(); h.moodle.resolve(); h.state.resolve();
  await second; await settle();
  assert.equal(h.run('scope'), currentSession.session_id);
  assert.equal(h.requests.filter(item => item.url === '/api/moodle').length, 1);
  assert.equal(h.requests.filter(item => item.url === '/api/moodle/sync').length, 1);
  const currentRevision = h.run('moodleRevision'), currentStatus = h.$('moodle-status').textContent;
  const currentTasks = [...h.$('moodle-tasks').children];
  older.resolve(); await first; await settle();
  assert.equal(h.run('scope'), currentSession.session_id, 'An older auth response cannot replace a newer verified session');
  assert.equal(h.run('moodleRevision'), currentRevision, 'A stale refresh must not clear or restart the current source');
  assert.equal(h.$('moodle-status').textContent, currentStatus);
  assert.deepEqual(h.$('moodle-tasks').children, currentTasks);
  assert.equal(h.requests.filter(item => item.url === '/api/moodle').length, 1, 'No old-session source read may start');
  assert.equal(h.requests.filter(item => item.url === '/api/moodle/sync').length, 1, 'No old-session source sync may start');
  assert.equal(h.chatLoads, 2, 'A stale bootstrap must not reload the chat catalog/history');
}

async function lateTaskStatusCannotPublishAfterAuthenticationOrSessionChanges() {
  for (const transition of ['denied_auth', 'changed_session']) {
    for (const outcome of [200, 403]) {
      const h = harness({scope: 'synthetic-alice'}), taskResponse = deferred(), originalFetch = h.sandbox.fetch;
      const newer = {session_id: 'synthetic-bob', csrf_token: 'synthetic_bob_token_123456789'};
      let authReads = 0;
      h.sandbox.fetch = async (url, options) => {
        if (url === '/auth/session') {
          h.requests.push({url, method: options.method || 'GET'});
          const first = ++authReads === 1;
          return !first && transition === 'denied_auth' ? {ok: false, status: 403} :
            {ok: true, status: 200, json: async () => first ? h.session : newer};
        }
        if (url === '/api/moodle/task-status') {
          h.requests.push({url, method: options.method || 'GET'});
          await taskResponse.promise;
          return outcome === 200 ? {ok: true, status: 200, json: async () => ({connected: true, snapshot: null})} :
            {ok: false, status: outcome};
        }
        return originalFetch(url, options);
      };
      h.installTaskStatus();
      const oldButton = {disabled: false, dataset: {taskId: 'old-owner-task', done: 'true'}};
      const marking = h.$('moodle-tasks').events.click({target: {closest: () => oldButton}});
      await settle();
      assert.equal(h.requests.filter(item => item.url === '/api/moodle/task-status').length, 1);
      const refreshing = h.run('refresh()'); h.moodle.resolve(); h.state.resolve();
      if (transition === 'denied_auth') await assert.rejects(refreshing, error => error.status === 403);
      else await refreshing;
      await settle();
      const status = h.$('moodle-status').textContent, children = [...h.$('moodle-tasks').children];
      assert.equal(h.run('scope'), transition === 'denied_auth' ? null : newer.session_id);
      taskResponse.resolve(); await marking; await settle();
      assert.equal(h.$('moodle-status').textContent, status,
        `${transition}/${outcome}: old success/failure cannot change the current owner's status`);
      assert.deepEqual(h.$('moodle-tasks').children, children,
        `${transition}/${outcome}: old task response cannot republish foreign tasks`);
      assert.equal(oldButton.disabled, true, 'Stale callback must not update the old account button');
    }
  }
}

async function currentSessionTaskStatusStillPublishesSuccessAndRestoresRetryOnFailure() {
  for (const successful of [true, false]) {
    const h = harness({scope: 'synthetic-alice'}), originalFetch = h.sandbox.fetch;
    h.auth.resolve(); h.installTaskStatus(); h.$('moodle-status').textContent = 'Before task update';
    h.sandbox.fetch = async (url, options) => url === '/api/moodle/task-status' ?
      {ok: successful, status: successful ? 200 : 503, json: async () => ({connected: true, snapshot: null})} :
      originalFetch(url, options);
    const button = {disabled: false, dataset: {taskId: 'current-task', done: 'true'}};
    await h.$('moodle-tasks').events.click({target: {closest: () => button}});
    if (successful) assert.match(h.$('moodle-status').textContent, /Задания ещё не загружены/);
    else {
      assert.equal(button.disabled, false, 'A current-session failure permits retry');
      assert.match(h.$('moodle-status').textContent, /Не удалось сохранить вашу отметку/);
    }
  }
}

(async () => {
  for (const test of [noRequestOrSourceSyncBeforeSessionBootstrap,
    earlyMoodleReadinessAndSlowMoodleBothLeaveAuthenticatedData,
    rejectedAuthenticationClearsStaleTasksWithoutFetchingSources,
    sameSessionRefreshKeepsExistingContentUntilFreshResponsesArrive,
    inFlightOldSessionSourceCannotReturnAfterAuthenticationFailure,
    olderAuthResponseCannotReplaceNewerSessionOrRestartItsSources,
    lateTaskStatusCannotPublishAfterAuthenticationOrSessionChanges,
    currentSessionTaskStatusStillPublishesSuccessAndRestoresRetryOnFailure]) {
    await test(); console.log('PASS', test.name);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
