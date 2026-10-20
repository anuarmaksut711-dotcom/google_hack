const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(0, 'utf8');

(async () => {
  const storage = new Map(), requests = [], navigations = [];
  let click;
  function page(pathname, cookie, search = '') {
    const status = {textContent: ''};
    const button = {disabled: false, addEventListener(name, callback) { click = callback; }};
    const context = {
      document: {cookie, getElementById: id => id === 'calendar-status' ? status : button},
      location: {pathname, search, assign: url => navigations.push(url), replace: url => navigations.push(url)},
      history: {replaceState() {}}, URLSearchParams,
      sessionStorage: {
        getItem: key => storage.get(key) ?? null,
        setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key),
      },
      fetch: async (path, options = {}) => {
        requests.push({path, options});
        if (path === '/api/account/google-calendar') return {ok: false, status: 403};
        if (path === '/auth/challenge') return {ok: true, json: async () => ({csrf_token: 'login-challenge'})};
        assert.equal(options.headers['X-CSRF-Token'], 'login-challenge');
        return {ok: true, json: async () => ({authorization_url: 'https://accounts.google.com/authorize'})};
      },
    };
    vm.runInNewContext(script, context);
    return {status, button};
  }
  const start = page('/google-calendar/connect', '');
  await new Promise(resolve => setImmediate(resolve));
  await click({target: start.button});
  assert.equal(requests.at(-1).path, '/auth/google-calendar/signin/start');
  assert.equal(navigations.at(-1), 'https://accounts.google.com/authorize');

  // A new document after Google redirects retains the challenge token in this
  // tab, while session CSRF may be absent or left over from a previous login.
  page('/auth/google-calendar/callback', '__Host-cp-calendar-flow=signin; __Host-cp-csrf=stale-session', '?code=code&state=state');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.at(-1).path, '/auth/google-calendar/signin/complete');
  assert.equal(JSON.parse(requests.at(-1).options.body).code, 'code');
  assert.equal(navigations.at(-1), '/#calendar');
  assert.equal(storage.size, 0);
  console.log('PASS Calendar sign-in preserves challenge CSRF across redirect and clears it afterward');
})().catch(error => { console.error(error); process.exitCode = 1; });
