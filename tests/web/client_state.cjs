// State-only regression harness: runs extracted product functions with fake DOM/IO.
// Usage: node /tmp/campuspulse-chat-audit/state-regressions.js [repository root]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = process.argv[2] || process.cwd();
const source = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/client.py'), 'utf8');
function between(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert(from >= 0 && to > from, `Product function boundary missing: ${start}`);
  return source.slice(from, to);
}
function harness() {
  const elements = new Map(), data = new Map(), pending = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', hidden: false,
      replaceChildren() {}, setAttribute() {}, focus() {}, classList: {remove() {}},
    });
    return elements.get(id);
  };
  const sessionStorage = new Proxy({
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: key => data.delete(key),
  }, {
    ownKeys: () => [...data.keys()],
    getOwnPropertyDescriptor: () => ({enumerable: true, configurable: true}),
  });
  const sandbox = {
    $, sessionStorage, URLSearchParams, AbortSignal, document: {cookie: '', body: {dataset: {signinUrl: '/login'}}},
    window: {location: {assign(url) { sandbox.destination = url; }}},
    location: {search: '', hash: '#workspace'}, history: {replaceState() {}},
    chats: {reset() {}, async load() {}},
    controls() {}, showChat() {}, updateNavigation() {}, feedback() {}, clearMoodle() {},
    stored(name, value) {if (value !== undefined) pending.set(name, value); return pending.get(name) ?? null;},
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let scope='session-1', activeChat='default', pendingText=null, generation=0,
      chatGeneration=0, activeRun=null, cursor=0, chatAvailable=true,
      chatWorking=false, chatLimitReached=false, chatBusy=false, busy=false,
      moodleEnabled=false, moodleRevision=0, moodleBusy=false;
    const chatDrafts=new Map();
    ${between('  const csrf = ', '  async function api(')}
    ${between('  async function api(', '  function clearMoodle(')}
    ${between('  const pendingChatKey', '  function selectChat')}
    ${between('  async function loadChat()', '  async function sendMessage')}
    ${between('  async function sendMessage(', '  async function refresh()')}
    ${between('  async function refresh()', '  async function act(')}
    ${between('  async function signOut()', "  $('signout').addEventListener")}
    function snapshot() { return {scope, pendingText, composer: $('recovery-message').value}; }
  `, sandbox);
  return {sandbox, data, pending, $, run: text => vm.runInContext(text, sandbox)};
}

async function transientRefreshRetainsDrafts() {
  const h = harness();
  h.$('recovery-message').value = 'Current unsent draft';
  h.run('saveDraft()');
  const other = 'campuspulse-draft-v1:session-1:other-chat';
  h.data.set(other, JSON.stringify({text: 'Other unsent draft', pendingText: null}));
  const expected = JSON.stringify([...h.data]);
  h.sandbox.api = async url => {
    if (url === '/auth/session') return {json: async () => ({session_id: 'session-1'})};
    const error = new Error('Temporary state endpoint failure'); error.status = 500; throw error;
  };
  await assert.rejects(h.run('refresh()'), /Temporary state endpoint failure/);
  assert.equal(JSON.stringify([...h.data]), expected, 'A transient state refresh must retain all session drafts');
}

async function completedPendingClearsAcceptedComposer() {
  const h = harness();
  h.$('recovery-message').value = '  Accepted message  ';
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='Accepted message'; saveDraft(); chatDrafts.clear(); restoreDraft('default')");
  h.sandbox.api = async () => ({json: async () => ({turns: [
    {request_key: 'request-1', message: 'Accepted message', status: 'completed'},
  ]})});
  await h.run('loadChat()');
  assert.equal(h.run('snapshot().composer'), '', 'A completed accepted message must not remain sendable as a fresh draft');
  assert.equal(h.run('snapshot().pendingText'), null);
  assert.equal(h.pending.get('pending-chat'), null);
  assert.equal(h.data.has('campuspulse-draft-v1:session-1:default'), false, 'The reconciled accepted draft must be removed from storage');
}

async function completedPendingPreservesUserEdits() {
  const h = harness();
  h.$('recovery-message').value = 'Edited next question';
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='Accepted message'; saveDraft()");
  h.sandbox.api = async () => ({json: async () => ({turns: [
    {request_key: 'request-1', message: 'Accepted message', status: 'completed'},
  ]})});
  await h.run('loadChat()');
  assert.equal(h.run('snapshot().composer'), 'Edited next question', 'Reconciliation must preserve user edits');
}

async function pendingWorkingThenCompletedReconciles() {
  const h = harness();
  h.$('recovery-message').value = 'Accepted message';
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='Accepted message'; saveDraft()");
  let status = 'working';
  h.sandbox.api = async () => ({json: async () => ({turns: [
    {request_key: 'request-1', message: 'Accepted message', status},
  ]})});
  await h.run('loadChat()');
  assert.equal(h.run('snapshot().composer'), 'Accepted message', 'A working turn must retain its text until settled');
  status = 'completed';
  await h.run('loadChat()');
  assert.equal(h.run('snapshot().composer'), '', 'A pending request first observed working must still reconcile when it completes');
  assert.equal(h.pending.get('pending-chat'), null);
}

async function unknownRequestRetainsIdempotencyKey() {
  const h = harness();
  h.$('recovery-message').value = 'Edited question';
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='Previous question'; saveDraft()");
  h.sandbox.api = async () => ({json: async () => ({turns: []})});
  await h.run('loadChat()');
  assert.equal(h.run('snapshot().pendingText'), 'Previous question');
  assert.equal(h.run('snapshot().composer'), 'Edited question');
  assert.equal(h.pending.get('pending-chat'), 'request-1');
  assert.equal(JSON.parse(h.data.get('campuspulse-draft-v1:session-1:default')).pendingText, 'Previous question');
}

async function refreshDuringSendRetainsIdempotencyKey() {
  const h = harness();
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='In flight'; chatBusy=true");
  h.sandbox.api = async () => ({json: async () => ({turns: []})});
  await h.run('loadChat()');
  assert.equal(h.run('snapshot().pendingText'), 'In flight');
  assert.equal(h.pending.get('pending-chat'), 'request-1');
}

async function expiredSessionDoesNotTrapDraft() {
  const h = harness();
  h.$('recovery-message').value = 'My question';
  h.sandbox.keyFor = () => 'request-1';
  h.sandbox.fetch = async () => ({ok: false, status: 403});
  await h.run('sendMessage({preventDefault() {}})');
  assert.equal(h.run('snapshot().pendingText'), null);
  assert.equal(h.run('snapshot().composer'), 'My question');
  assert.match(h.$('chat-status').textContent, /Войдите заново/);
  assert.equal(h.pending.get('pending-chat'), null);
}

async function failedBootstrapPreservesAmbiguousEarlierSend() {
  const h = harness();
  h.$('recovery-message').value = 'My question';
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='My question'");
  h.sandbox.keyFor = () => 'request-1';
  h.sandbox.fetch = async () => { throw new Error('Network unavailable'); };
  await h.run('sendMessage({preventDefault() {}})');
  assert.equal(h.run('snapshot().pendingText'), 'My question');
  assert.equal(h.pending.get('pending-chat'), 'request-1');
}

async function bootstrapRecoversWithoutJavascriptCookie() {
  const h = harness(), requests = [];
  const token = 'recovered_session_token_123456789';
  h.sandbox.fetch = async (url, options) => {
    requests.push({url, options});
    return url === '/auth/session' ?
      {ok: true, status: 200, json: async () => ({session_id: 'session-1', csrf_token: token})} :
      {ok: true, status: 200};
  };
  await h.run("api('/api/chat', {request_key:'request-1', message:'Hello'})");
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.headers['X-CSRF-Bootstrap'], '1');
  assert.equal(requests[1].options.headers['X-CSRF-Token'], token);
  assert.equal(requests[1].options.method, 'POST');
  assert.equal(h.sandbox.document.cookie, '');
}

async function switchedSessionNeverSendsMutation() {
  const h = harness(), requests = [];
  h.sandbox.fetch = async url => {
    requests.push(url);
    return {ok: true, status: 200, json: async () => ({
      session_id: 'another-session', csrf_token: 'another_session_token_123456789',
    })};
  };
  await assert.rejects(h.run("api('/api/chat', {message:'Private draft'})"), error => error.status === 403);
  assert.deepEqual(requests, ['/auth/session']);
}

async function refreshedPageDoesNotReportExpiredSession() {
  const h = harness(), requests = [];
  h.sandbox.fetch = async url => {
    requests.push(url);
    h.run('++generation');
    return {ok: true, status: 200, json: async () => ({
      session_id: 'session-1', csrf_token: 'valid_session_token_123456789',
    })};
  };
  await assert.rejects(h.run("api('/api/chat', {message:'Hello'})"),
    error => error.code === 'request_not_sent' && error.status !== 403);
  assert.deepEqual(requests, ['/auth/session']);
}

async function rejectedMutationIsNeverAutomaticallyReplayed() {
  const h = harness(), requests = [];
  h.sandbox.fetch = async url => {
    requests.push(url);
    return url === '/auth/session' ?
      {ok: true, status: 200, json: async () => ({session_id: 'session-1', csrf_token: 'valid_session_token_123456789'})} :
      {ok: false, status: 403};
  };
  await assert.rejects(h.run("api('/api/chat', {message:'Hello'})"), error => error.status === 403);
  assert.deepEqual(requests, ['/auth/session', '/api/chat']);
}

async function concurrentRepairRetriesOnlyBootstrap() {
  const h = harness(), requests = [];
  h.sandbox.fetch = async url => {
    requests.push(url);
    if (requests.length === 1) return {ok: false, status: 409};
    return url === '/auth/session' ?
      {ok: true, status: 200, json: async () => ({session_id: 'session-1', csrf_token: 'valid_session_token_123456789'})} :
      {ok: true, status: 200};
  };
  await h.run("api('/api/chat', {message:'Hello'})");
  assert.deepEqual(requests, ['/auth/session', '/auth/session', '/api/chat']);
}

async function expiredSignoutReachesLogin() {
  const h = harness();
  h.$('recovery-message').value = 'Unsent draft';
  h.sandbox.fetch = async () => ({ok: false, status: 403});
  await h.run('signOut()');
  assert.equal(h.sandbox.destination, '/login');
  assert.equal(h.run('snapshot().composer'), 'Unsent draft');
}

async function validSignoutRevokesBeforeNavigating() {
  const h = harness(), requests = [];
  h.sandbox.fetch = async url => {
    requests.push(url);
    assert.equal(h.sandbox.destination, undefined);
    return url === '/auth/session' ?
      {ok: true, status: 200, json: async () => ({session_id: 'session-1', csrf_token: 'valid_session_token_123456789'})} :
      {ok: true, status: 200};
  };
  await h.run('signOut()');
  assert.deepEqual(requests, ['/auth/session', '/auth/signout']);
  assert.equal(h.sandbox.destination, '/login');
}

async function staleRefreshDoesNotClearNewRequest() {
  const h = harness();
  let resolveHistory;
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='Previous question'");
  h.sandbox.api = () => new Promise(resolve => { resolveHistory = resolve; });
  const refresh = h.run('loadChat()');
  h.pending.set('pending-chat', 'request-2');
  h.run("pendingText='New question'");
  resolveHistory({json: async () => ({turns: [
    {request_key: 'request-1', message: 'Previous question', status: 'completed'},
  ]})});
  await refresh;
  assert.equal(h.pending.get('pending-chat'), 'request-2');
  assert.equal(h.run('snapshot().pendingText'), 'New question');
}

(async () => {
  let failures = 0;
  for (const test of [transientRefreshRetainsDrafts, completedPendingClearsAcceptedComposer, completedPendingPreservesUserEdits, pendingWorkingThenCompletedReconciles, unknownRequestRetainsIdempotencyKey, refreshDuringSendRetainsIdempotencyKey, expiredSessionDoesNotTrapDraft, failedBootstrapPreservesAmbiguousEarlierSend, staleRefreshDoesNotClearNewRequest, bootstrapRecoversWithoutJavascriptCookie, switchedSessionNeverSendsMutation, refreshedPageDoesNotReportExpiredSession, rejectedMutationIsNeverAutomaticallyReplayed, concurrentRepairRetriesOnlyBootstrap, expiredSignoutReachesLogin, validSignoutRevokesBeforeNavigating]) {
    try {await test(); console.log('PASS', test.name);}
    catch (error) {++failures; console.error('FAIL', test.name, error.message);}
  }
  process.exitCode = failures ? 1 : 0;
})();
