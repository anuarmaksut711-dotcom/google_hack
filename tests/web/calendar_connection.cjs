// Exercise the shipped Calendar mutation adapter at its browser/network boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(0, 'utf8');
const helper = script.slice(0, script.indexOf('function fail('));
const token = 'valid_calendar_csrf_token_0123456789';

function harness({cookie = '', replies = [], postStatus = 200, storage = new Map()} = {}) {
  const requests = [];
  const sandbox = {
    document: {cookie, getElementById: () => ({textContent: ''})}, AbortSignal,
    sessionStorage: {getItem: key => storage.get(key) ?? null},
    fetch: async (path, options = {}) => {
      requests.push({path, options});
      if (path === '/auth/session') {
        const next = replies.shift() || {status: 200, session_id: 'session-one', csrf_token: token};
        return {ok: next.status === 200, status: next.status, json: async () => next};
      }
      return {ok: postStatus === 200, status: postStatus, json: async () => ({connected: true})};
    },
  };
  vm.createContext(sandbox); vm.runInContext(helper, sandbox);
  return {requests, post: (path, value = {}) => sandbox.calendarPost(path, value)};
}

async function cookieLessSessionBootstrapsEveryAuthenticatedCalendarMutation() {
  const h = harness();
  const paths = ['/api/account/google-calendar/start', '/api/account/google-calendar/complete',
    '/api/account/google-calendar/disconnect', '/api/google-calendar/plans/approve',
    '/api/google-calendar/plans/apply', '/api/google-calendar/plans/verify'];
  for (const path of paths) {
    await h.post(path, {state: 'original-state'});
    const [bootstrap, mutation] = h.requests.slice(-2);
    assert.equal(bootstrap.path, '/auth/session');
    assert.equal(bootstrap.options.headers['X-CSRF-Bootstrap'], '1');
    assert.equal(bootstrap.options.credentials, 'same-origin');
    assert.equal(bootstrap.options.cache, 'no-store');
    assert.equal(mutation.path, path);
    assert.equal(mutation.options.method, 'POST');
    assert.equal(mutation.options.redirect, 'error', 'A redirect must not replay the one-use OAuth completion');
    assert.equal(mutation.options.headers['X-CSRF-Token'], token);
    assert.equal(JSON.parse(mutation.options.body).state, 'original-state');
  }
  assert.equal(h.requests.length, 12);
}

async function staleVisibleCookieDoesNotChooseTheMutationToken() {
  const h = harness({cookie: '__Host-cp-csrf=stale_cookie_token_0123456789'});
  await h.post('/api/account/google-calendar/start');
  assert.equal(h.requests.at(-1).options.headers['X-CSRF-Token'], token);
}

async function preloginKeepsItsOriginalChallengeAndNeverUsesSessionBootstrap() {
  const h = harness({storage: new Map([['cp-calendar-login-csrf', 'original-challenge']])});
  await h.post('/auth/google-calendar/signin/start');
  await h.post('/auth/google-calendar/signin/complete', {code: 'one-use-code', state: 'original-state'});
  assert.equal(h.requests.length, 2);
  assert(h.requests.every(request => request.options.headers['X-CSRF-Token'] === 'original-challenge'));
  const missing = harness();
  await assert.rejects(missing.post('/auth/google-calendar/signin/complete'), /signin/);
  assert.equal(missing.requests.length, 0);
}

async function revokedOrMalformedSessionNeverDispatchesTheMutation() {
  for (const reply of [{status: 403}, {status: 503},
    {status: 200, session_id: 'session-one', csrf_token: 'bad token'},
    {status: 200, csrf_token: token}]) {
    const h = harness({replies: [reply]});
    await assert.rejects(h.post('/api/account/google-calendar/complete', {code: 'one-use-code'}));
    assert.deepEqual(h.requests.map(request => request.path), ['/auth/session']);
  }
}

async function switchedSessionCannotMutateTheCalendarShownByThisDocument() {
  const h = harness({replies: [
    {status: 200, session_id: 'session-one', csrf_token: token},
    {status: 200, session_id: 'session-two', csrf_token: token},
  ]});
  await h.post('/api/account/google-calendar/start');
  await assert.rejects(h.post('/api/account/google-calendar/disconnect'), /signin/);
  assert.deepEqual(h.requests.map(request => request.path),
    ['/auth/session', '/api/account/google-calendar/start', '/auth/session']);
}

async function concurrentRepairRetriesOnlyTheBootstrap() {
  const h = harness({replies: [{status: 409}, {status: 200, session_id: 'session-one', csrf_token: token}]});
  await h.post('/api/account/google-calendar/complete', {code: 'one-use-code'});
  assert.deepEqual(h.requests.map(request => request.path),
    ['/auth/session', '/auth/session', '/api/account/google-calendar/complete']);
}

async function rejectedMutationIsNeverAutomaticallyReplayed() {
  for (const postStatus of [403, 503]) {
    const h = harness({postStatus});
    await assert.rejects(h.post('/api/account/google-calendar/complete', {code: 'one-use-code'}));
    assert.deepEqual(h.requests.map(request => request.path),
      ['/auth/session', '/api/account/google-calendar/complete']);
  }
}

(async () => {
  for (const test of [cookieLessSessionBootstrapsEveryAuthenticatedCalendarMutation,
    staleVisibleCookieDoesNotChooseTheMutationToken, preloginKeepsItsOriginalChallengeAndNeverUsesSessionBootstrap,
    revokedOrMalformedSessionNeverDispatchesTheMutation, switchedSessionCannotMutateTheCalendarShownByThisDocument,
    concurrentRepairRetriesOnlyTheBootstrap, rejectedMutationIsNeverAutomaticallyReplayed]) {
    await test(); console.log('PASS', test.name);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
