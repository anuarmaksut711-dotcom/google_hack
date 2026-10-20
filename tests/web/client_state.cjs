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
      replaceChildren() {}, setAttribute() {}, classList: {remove() {}},
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
    $, sessionStorage, URLSearchParams,
    location: {search: '', hash: '#workspace'}, history: {replaceState() {}},
    chats: {reset() {}, async load() {}},
    controls() {}, showChat() {}, updateNavigation() {}, feedback() {},
    stored(name, value) {if (value !== undefined) pending.set(name, value); return pending.get(name) ?? null;},
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let scope='session-1', activeChat='default', pendingText=null, generation=0,
      chatGeneration=0, activeRun=null, cursor=0, chatAvailable=true,
      chatWorking=false, chatLimitReached=false;
    const chatDrafts=new Map();
    ${between('  const pendingChatKey', '  function selectChat')}
    ${between('  async function loadChat()', '  async function sendMessage')}
    ${between('  async function refresh()', '  async function act(')}
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

(async () => {
  let failures = 0;
  for (const test of [transientRefreshRetainsDrafts, completedPendingClearsAcceptedComposer, completedPendingPreservesUserEdits, pendingWorkingThenCompletedReconciles]) {
    try {await test(); console.log('PASS', test.name);}
    catch (error) {++failures; console.error('FAIL', test.name, error.message);}
  }
  process.exitCode = failures ? 1 : 0;
})();
