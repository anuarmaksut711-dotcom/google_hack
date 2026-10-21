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
  const elements = new Map(), data = new Map(), pending = new Map(), pollers = [];
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', hidden: false,
      querySelectorAll() { return []; }, scrollHeight: 500, scrollTop: 0, clientHeight: 200,
      replaceChildren() {}, setAttribute() {}, focus() {}, scrollTo() {}, classList: {remove() {}},
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
    window: {location: {assign(url) { sandbox.destination = url; }}, CampusPulseStudyTrace: {
      Poller: class {
        constructor(options) { this.options = options; this.stopped = false; pollers.push(this); }
        start(requestKey) { this.requestKey = requestKey; }
        stop() { this.stopped = true; }
      },
      update(node, turn) { sandbox.lastProgress = {node, turn}; },
    }},
    location: {pathname: '/', search: '', hash: '#workspace'}, history: {replaceState() {}},
    chats: {data: {chat_available: true, speech_available: true}, reset() {}, selection() {}, async load() {}},
    controls() {}, showChat(chat) {
      sandbox.lastChat = chat;
      sandbox.renderTarget = chat;
      vm.runInContext(`renderedChat=renderTarget; chatWorking=renderTarget.turns.some(turn => turn.status==='working');
        chatAvailable=renderTarget.available`, sandbox);
      delete sandbox.renderTarget;
    }, updateNavigation() {}, feedback() {}, clearMoodle() {},
    keyFor(name) { if (!pending.get(name)) pending.set(name, 'stable-first-send'); return pending.get(name); },
    stored(name, value) {if (value !== undefined) pending.set(name, value); return pending.get(name) ?? null;},
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let scope='session-1', activeChat='default', pendingText=null, generation=0,
      chatGeneration=0, activeRun=null, cursor=0, chatAvailable=true,
      chatWorking=false, chatLimitReached=false, chatBusy=false, busy=false,
      moodleEnabled=false, moodleRevision=0, moodleBusy=false, renderedChat=null, chatPoller=null;
    const chatDrafts=new Map();
    ${between('  const draftChat', '  let activeChat')}
    ${between('  const csrf = ', '  async function api(')}
    ${between('  async function api(', '  function clearMoodle(')}
    ${between('  const pendingChatKey', '  function selectChat')}
    ${between('  function watchChatProgress(', '  async function loadChat()')}
    ${between('  async function loadChat()', '  async function sendMessage')}
    ${between('  async function sendMessage(', '  async function refresh()')}
    ${between('  async function refresh()', '  async function act(')}
    ${between('  async function signOut()', "  $('signout').addEventListener")}
    function snapshot() { return {scope, pendingText, composer: $('recovery-message').value}; }
  `, sandbox);
  return {sandbox, data, pending, pollers, $, run: text => vm.runInContext(text, sandbox)};
}

async function progressReadsNeverPostOrDiscardLastObservationOnFailure() {
  const h = harness(), requests = [];
  h.pending.set('pending-chat', 'request-1');
  h.run("pendingText='Original question'; renderedChat={turns:[{request_key:'request-1',status:'working'}]}");
  h.sandbox.api = async (...args) => {
    requests.push(args);
    return {json: async () => ({turns: [{request_key: 'request-1', status: 'working'}]})};
  };
  h.run("watchChatProgress('request-1')");
  const poller = h.pollers[0], observed = await poller.options.read();
  assert.equal(observed.turns[0].request_key, 'request-1');
  assert.deepEqual(requests, [['/api/chat?chat_id=default']], 'Progress uses a GET with no payload or CSRF mutation');
  const previous = h.run('renderedChat');
  poller.options.failed(Object.assign(new Error('Temporary failure'), {status: 503}));
  assert.equal(h.run('renderedChat'), previous, 'Polling errors preserve the last rendered chat');
  assert.equal(h.run('pendingText'), 'Original question');
  assert.equal(h.pending.get('pending-chat'), 'request-1', 'A failed progress GET must not clear the original retry key');
  assert.match(h.$('chat-status').textContent, /Не удалось обновить ход работы/);
  assert.equal(requests.length, 1, 'A polling error must never replay the message POST');
}

async function progressUpdatesExistingDisclosureAndPreservesScrollPosition() {
  const h = harness(), disclosure = {open: false};
  const reply = {dataset: {requestKey: 'request-1'}, querySelector: () => disclosure};
  h.$('chat-history').querySelectorAll = () => [reply];
  h.$('conversation-scroll').scrollTop = 41;
  h.run("watchChatProgress('request-1')");
  const chat = {available: true, turns: [{request_key: 'request-1', status: 'working',
    agent_progress: {phase: 'tools', tool_calls: [{tool: 'read_assignments', status: 'running'}]}}]};
  h.pollers[0].options.update(chat, chat.turns[0]);
  assert.equal(h.sandbox.lastProgress.node, disclosure, 'Live observations update the existing disclosure in place');
  assert.equal(disclosure.open, false, 'A background observation must not reopen details the user collapsed');
  assert.equal(h.run('renderedChat'), chat);
  assert.equal(h.run('chatWorking'), true);
  assert.equal(h.sandbox.lastChat, undefined, 'A progress change must not rebuild conversation history');
  assert.equal(h.$('conversation-scroll').scrollTop, 41);
}

async function progressSelectionAndSessionGuardsIgnoreOldChat() {
  for (const mutation of ["++generation", "scope='new-session'", "activeChat='new-chat'", "++chatGeneration"]) {
    const h = harness();
    h.run("watchChatProgress('request-1')");
    assert.equal(h.pollers[0].options.valid(), true);
    h.run(mutation);
    assert.equal(h.pollers[0].options.valid(), false, `Invalidate observation after ${mutation}`);
  }
  const h = harness();
  h.run("watchChatProgress('request-1'); watchChatProgress('request-2')");
  assert.equal(h.pollers[0].stopped, true, 'Starting another observation stops the previous loop');
  assert.equal(h.pollers[1].requestKey, 'request-2');
}

async function terminalProgressReconcilesOriginalDraftAndPreservesEdits() {
  for (const edited of [false, true]) {
    const h = harness();
    h.$('recovery-message').value = edited ? 'New unsent question' : 'Accepted question';
    h.pending.set('pending-chat', 'request-1');
    h.run("pendingText='Accepted question'; watchChatProgress('request-1')");
    const turn = {request_key: 'request-1', status: 'completed', message: 'Accepted question'};
    h.pollers[0].options.update({turns: [turn]}, turn);
    assert.equal(h.$('recovery-message').value, edited ? 'New unsent question' : '');
    assert.equal(h.pending.get('pending-chat'), null);
    assert.equal(h.run('pendingText'), null);
  }
}

async function bootstrapFailureClearsOptimisticWorkingAndAllowsRetry() {
  const h = harness(), requests = [];
  h.sandbox.showChat({available: true, turns: []});
  h.$('recovery-message').value = 'Keep my unsent question';
  h.sandbox.fetch = async url => {requests.push(url); throw new Error('Bootstrap network unavailable');};
  await h.run('sendMessage({preventDefault(){}})');
  assert.deepEqual(requests, ['/auth/session'], 'No chat POST was dispatched when CSRF bootstrap failed');
  assert.equal(h.sandbox.lastChat.turns.length, 0, 'A definitively unsent request must remove its optimistic working row');
  assert.equal(h.run('chatWorking'), false, 'The composer must not remain blocked on a request that was never sent');
  assert.equal(h.run('chatBusy'), false);
  assert.equal(h.run('pendingText'), null);
  assert.equal(h.pending.get('pending-chat'), null, 'A known-unsent request permits a fresh explicit retry');
  assert.equal(h.$('recovery-message').value, 'Keep my unsent question');
  assert(h.pollers.length > 0 && h.pollers.every(poller => poller.stopped), 'No optimistic observation loop may remain running');
}

async function rejectedDispatchedPostPreservesRetryKeyAndShowsUnknown() {
  const h = harness(), requests = [];
  h.sandbox.showChat({available: true, turns: []});
  h.$('recovery-message').value = 'Possibly dispatched question';
  h.sandbox.fetch = async (url, options) => {
    requests.push({url, method: options.method || 'GET'});
    return url === '/auth/session' ?
      {ok: true, status: 200, json: async () => ({session_id: 'session-1', csrf_token: 'valid_session_token_123456789'})} :
      {ok: false, status: 403};
  };
  await h.run('sendMessage({preventDefault(){}})');
  assert.deepEqual(requests, [{url: '/auth/session', method: 'GET'}, {url: '/api/chat', method: 'POST'}]);
  assert.equal(h.sandbox.lastChat.turns.length, 1);
  assert.equal(h.sandbox.lastChat.turns[0].status, 'unknown', 'A server rejection after dispatch cannot be presented as continuing work');
  assert.equal(h.run('chatWorking'), false);
  assert.equal(h.run('pendingText'), 'Possibly dispatched question');
  assert.equal(h.pending.get('pending-chat'), 'stable-first-send', 'Preserve the original idempotency key when dispatch was attempted');
  assert.equal(h.$('recovery-message').value, 'Possibly dispatched question');
  assert(h.pollers.length > 0 && h.pollers.every(poller => poller.stopped), 'Rejected session must stop progress polling');
}

async function idempotentPostStillWorkingRetainsDraftKeyAndPolling() {
  const h = harness(), requests = [];
  h.sandbox.showChat({available: true, turns: []});
  h.$('recovery-message').value = 'Original in-flight question';
  h.pending.set('pending-chat', 'original-key');
  h.run("pendingText='Original in-flight question'");
  h.sandbox.api = async (url, payload) => {
    requests.push({url, payload});
    return {json: async () => ({available: true, turns: [{request_key: payload.request_key,
      message: payload.message, status: 'working'}]})};
  };
  await h.run('sendMessage({preventDefault(){}})');
  assert.equal(requests.length, 1, 'The explicit retry makes one POST with the saved key');
  assert.equal(requests[0].payload.request_key, 'original-key');
  assert.equal(h.pending.get('pending-chat'), 'original-key', 'A working response is not a settled request');
  assert.equal(h.run('pendingText'), 'Original in-flight question');
  assert.equal(h.$('recovery-message').value, 'Original in-flight question');
  assert.equal(h.run('chatWorking'), true);
  assert.equal(h.run('chatBusy'), false);
  assert.equal(h.pollers.at(-1).stopped, false, 'Progress observation must continue until a terminal response');
  assert.equal(h.pollers.at(-1).requestKey, 'original-key');
}

async function openingDraftDoesNotReadOrCreateServerChat() {
  const h = harness();
  h.run("activeChat='draft'");
  h.sandbox.api = async () => { throw new Error('A blank draft must not contact the conversation API'); };
  await h.run('loadChat()');
  assert.equal(h.sandbox.lastChat?.turns.length, 0, 'New chat must show an empty local conversation');
  assert.equal(h.sandbox.lastChat?.available, true);
  assert.equal(h.sandbox.lastChat?.speech_available, true, 'Opening a draft must preserve voice input capability');
  assert.equal(h.run('chatAvailable'), true);
}

async function firstDraftSendCreatesOneChatAndMovesPendingState() {
  const h = harness();
  const folder = 'a'.repeat(32), realId = 'b'.repeat(32);
  h.run(`activeChat='draft-${folder}'`);
  h.$('recovery-message').value = 'Объясни алгоритм Крускала';
  const created = [], sent = [];
  h.sandbox.chats.materialize = async (folderId, requestKey) => {
    created.push([folderId, requestKey]); return realId;
  };
  h.sandbox.api = async (url, payload) => {
    sent.push([url, payload]);
    assert.equal(payload.chat_id, realId, 'Only a registered chat ID may reach the message API');
    return {json: async () => ({available: true, turns: [{request_key: payload.request_key,
      message: payload.message, status: 'completed'}]})};
  };
  await h.run('sendMessage({preventDefault(){}})');
  assert.deepEqual(created, [[folder, 'stable-first-send']], 'Register the draft once, on first send');
  assert.equal(sent.length, 1);
  assert.equal(h.run('activeChat'), realId);
  assert.equal(h.$('recovery-message').value, '');
  assert.equal(h.pending.get('pending-chat:' + realId), null);
  assert.equal(h.pending.get('pending-chat:draft-' + folder), null);
  assert.equal(h.data.has('campuspulse-draft-v1:session-1:draft-' + folder), false);
}

async function failedDraftCreationKeepsTextAndStableRetryKey() {
  const h = harness();
  h.run("activeChat='draft'");
  h.$('recovery-message').value = 'Несохранённый вопрос';
  const keys = [];
  h.sandbox.chats.materialize = async (folder, key) => {keys.push(key); throw new Error('Temporary creation failure');};
  await h.run('sendMessage({preventDefault(){}})');
  h.$('recovery-message').value = 'Исправленный вопрос';
  await h.run('sendMessage({preventDefault(){}})');
  assert.deepEqual(keys, ['stable-first-send', 'stable-first-send']);
  assert.equal(h.run('activeChat'), 'draft');
  assert.equal(h.$('recovery-message').value, 'Исправленный вопрос');
  assert.equal(JSON.parse(h.data.get('campuspulse-draft-v1:session-1:draft')).text, 'Исправленный вопрос');
}

async function repeatedNewChatButtonsNeverWriteCatalog() {
  const workspaceSource = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/workspace.py'), 'utf8');
  const script = workspaceSource.split('SCRIPT = r"""')[1].split('"""')[0];
  const elements = new Map(), opened = [];
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {events: {}, addEventListener(name, callback) {this.events[name] = callback;}});
      return elements.get(id);
    },
    querySelector() { return null; },
  };
  const sandbox = {window: {addEventListener() {}}, document};
  vm.createContext(sandbox); vm.runInContext(script, sandbox);
  const chats = new sandbox.window.CampusPulseChats({
    api() {throw new Error('Opening a draft must never write the catalog');},
    selected: () => 'draft', navigate: id => opened.push(id), changed() {}, blocked: () => false, session: () => 'session-1',
  });
  chats.data = {revision: 0, folders: [], chats: []};
  for (let index=0; index<5; index++) elements.get('chat-new').events.click();
  chats.startChat('a'.repeat(32)); chats.startChat('a'.repeat(32));
  assert.deepEqual(opened, ['draft', 'draft', 'draft', 'draft', 'draft', 'draft-' + 'a'.repeat(32), 'draft-' + 'a'.repeat(32)]);
  assert.equal(chats.data.chats.length, 0);
}

async function draftKeysAreIsolatedBetweenTabsAndStableAfterReload() {
  const shared = new Map(), tabOne = new Map(), tabTwo = new Map();
  const storage = data => ({getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value)});
  let sequence = 0;
  function tab(session) {
    const sandbox = {sessionStorage: storage(session), localStorage: storage(shared),
      crypto: {randomUUID: () => String(++sequence)}};
    vm.createContext(sandbox);
    vm.runInContext(`let scope='session-1'; ${between('  function stored(', '  function reference(')}`, sandbox);
    return name => vm.runInContext(`keyFor(${JSON.stringify(name)})`, sandbox);
  }
  const one = tab(tabOne), two = tab(tabTwo);
  const first = one('pending-chat:draft'), second = two('pending-chat:draft');
  assert.notEqual(first, second, 'Independent tabs must not merge their first messages into the same chat');
  assert.equal(tab(tabOne)('pending-chat:draft'), first, 'A reload in the original tab must retain its creation key');
  assert.equal(one('pending-chat:registered'), two('pending-chat:registered'), 'Existing chat recovery remains shared');
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

async function deletingSelectedChatClearsItsLocalStateAndOpensDraft() {
  const h = harness(), id = 'c'.repeat(32);
  h.run(`activeChat='${id}'; pendingText='Pending old text'`);
  h.$('recovery-message').value = 'Deleted draft';
  h.run('saveDraft()');
  h.pending.set('pending-chat:' + id, 'old-key');
  h.sandbox.chats.data.chats = [];
  h.run(`removedWorkspaceItem({action:'delete_chat',item_id:'${id}'})`);
  assert.equal(h.run('activeChat'), 'draft');
  assert.equal(h.$('recovery-message').value, '');
  assert.equal(h.pending.get('pending-chat:' + id), null);
  assert.equal(h.data.has('campuspulse-draft-v1:session-1:' + id), false);
  assert.equal(h.run('chatDrafts.has("' + id + '")'), false);
}

async function deletingOtherChatPreservesCurrentDraft() {
  const h = harness(), id = 'd'.repeat(32);
  h.$('recovery-message').value = 'Keep current draft';
  h.run(`removedWorkspaceItem({action:'delete_chat',item_id:'${id}'})`);
  assert.equal(h.run('activeChat'), 'default');
  assert.equal(h.$('recovery-message').value, 'Keep current draft');
}

async function deletedProjectDraftSendsIntoGeneralList() {
  const h = harness(), folder = 'a'.repeat(32), id = 'b'.repeat(32);
  h.run(`activeChat='draft-${folder}'`);
  h.sandbox.chats.data.folders = [];
  h.$('recovery-message').value = 'Keep project draft';
  let destination;
  h.sandbox.chats.materialize = async target => { destination = target; return id; };
  h.sandbox.api = async (url, payload) => ({json: async () => ({turns: []})});
  await h.run('sendMessage({preventDefault(){}})');
  assert.equal(destination, null);
  assert.equal(h.run('activeChat'), id);
}

async function deletedAmbiguousCreationAllowsExplicitFreshSend() {
  const h = harness(), id = 'b'.repeat(32), attempts = [];
  let sequence = 0;
  h.run("activeChat='draft'");
  h.sandbox.keyFor = name => {
    if (!h.pending.get(name)) h.pending.set(name, 'key-' + (++sequence));
    return h.pending.get(name);
  };
  h.$('recovery-message').value = 'Preserved question';
  h.sandbox.chats.materialize = async (target, key) => {
    attempts.push(key);
    if (attempts.length === 1) throw new Error('Created but response lost');
    if (attempts.length === 2) throw Object.assign(new Error('Deleted'), {status:403, code:'workspace_item_deleted'});
    return id;
  };
  h.sandbox.api = async () => ({json: async () => ({turns: []})});
  await h.run('sendMessage({preventDefault(){}})');
  await h.run('sendMessage({preventDefault(){}})');
  assert.equal(h.$('recovery-message').value, 'Preserved question');
  assert(!h.$('chat-status').textContent.includes('Войдите заново'));
  await h.run('sendMessage({preventDefault(){}})');
  assert.deepEqual(attempts, ['key-1', 'key-1', 'key-2']);
  assert.equal(h.run('activeChat'), id);
}

async function lostCreationResponseKeepsOriginalProjectPayloadAfterProjectDeletion() {
  const h = harness(), folder = 'a'.repeat(32), id = 'b'.repeat(32), attempts = [];
  h.run(`activeChat='draft-${folder}'`);
  h.sandbox.chats.data.folders = [{folder_id: folder}];
  h.$('recovery-message').value = 'Recover accepted creation';
  h.sandbox.chats.materialize = async (target, key) => {
    attempts.push([target, key]);
    if (attempts.length === 1) throw new Error('Successful response lost');
    return id;
  };
  h.sandbox.api = async () => ({json: async () => ({turns: []})});
  await h.run('sendMessage({preventDefault(){}})');
  h.sandbox.chats.data.folders = [];
  await h.run('sendMessage({preventDefault(){}})');
  assert.deepEqual(attempts, [[folder, 'stable-first-send'], [folder, 'stable-first-send']]);
  assert.equal(h.run('activeChat'), id);
}

async function deletedBookmarkPreservesUnsentTextWithoutReadingDeletedHistory() {
  const h = harness(), id = 'e'.repeat(32);
  h.run(`activeChat='${id}'; pendingText='Old unknown message'`);
  h.$('recovery-message').value = 'Keep this unsent text';
  h.sandbox.chats.data.chats = [];
  h.sandbox.api = async () => { throw new Error('Deleted chat must not be requested'); };
  h.pending.set('pending-chat:' + id, 'deleted-key');
  await h.run('loadChat()');
  assert.equal(h.run('activeChat'), 'draft-' + id);
  assert.equal(h.$('recovery-message').value, 'Keep this unsent text');
  assert.equal(h.run('pendingText'), null);
  assert.equal(h.pending.get('pending-chat:' + id), null);
  assert.equal(h.sandbox.lastChat?.turns.length, 0);
}

(async () => {
  let failures = 0;
  for (const test of [progressReadsNeverPostOrDiscardLastObservationOnFailure,
    progressUpdatesExistingDisclosureAndPreservesScrollPosition, progressSelectionAndSessionGuardsIgnoreOldChat,
    terminalProgressReconcilesOriginalDraftAndPreservesEdits, bootstrapFailureClearsOptimisticWorkingAndAllowsRetry,
    rejectedDispatchedPostPreservesRetryKeyAndShowsUnknown, idempotentPostStillWorkingRetainsDraftKeyAndPolling]) {
    try {await test(); console.log('PASS', test.name);}
    catch (error) {++failures; console.error('FAIL', test.name, error.message);}
  }
  for (const test of [deletedAmbiguousCreationAllowsExplicitFreshSend, lostCreationResponseKeepsOriginalProjectPayloadAfterProjectDeletion, deletedBookmarkPreservesUnsentTextWithoutReadingDeletedHistory, deletingSelectedChatClearsItsLocalStateAndOpensDraft, deletingOtherChatPreservesCurrentDraft, deletedProjectDraftSendsIntoGeneralList, transientRefreshRetainsDrafts, completedPendingClearsAcceptedComposer, completedPendingPreservesUserEdits, pendingWorkingThenCompletedReconciles, unknownRequestRetainsIdempotencyKey, refreshDuringSendRetainsIdempotencyKey, expiredSessionDoesNotTrapDraft, failedBootstrapPreservesAmbiguousEarlierSend, staleRefreshDoesNotClearNewRequest, bootstrapRecoversWithoutJavascriptCookie, switchedSessionNeverSendsMutation, refreshedPageDoesNotReportExpiredSession, rejectedMutationIsNeverAutomaticallyReplayed, concurrentRepairRetriesOnlyBootstrap, expiredSignoutReachesLogin, validSignoutRevokesBeforeNavigating, openingDraftDoesNotReadOrCreateServerChat, firstDraftSendCreatesOneChatAndMovesPendingState, failedDraftCreationKeepsTextAndStableRetryKey, repeatedNewChatButtonsNeverWriteCatalog, draftKeysAreIsolatedBetweenTabsAndStableAfterReload]) {
    try {await test(); console.log('PASS', test.name);}
    catch (error) {++failures; console.error('FAIL', test.name, error.message);}
  }
  process.exitCode = failures ? 1 : 0;
})();
