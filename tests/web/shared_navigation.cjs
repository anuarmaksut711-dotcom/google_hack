'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync(0, 'utf8');

function setup({authenticated = true, csrfSession = 'ses_owner', bootstrapConflict = false,
                swapOnWorkspace = false, swapOnPost = false} = {}) {
  const nodes = new Map(), calls = [], destinations = [], instances = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden: true, disabled: false, listeners: {},
      addEventListener(event, handler) { this.listeners[event] = handler; },
      querySelectorAll() { return this.links || []; }});
    return nodes.get(id);
  };
  const storage = {'campuspulse-draft-v1:ses_owner:draft': 'own', 'campuspulse-draft-v1:ses_other:draft': 'other'};
  Object.defineProperty(storage, 'removeItem', {value(key) { delete storage[key]; }});
  class Chats {
    constructor(options) { Object.assign(this, options); this.mutating = false; this.data = null; instances.push(this); }
    $(id) { return node(id); }
    controls() { this.isBlocked = this.blocked(); }
    reset() { this.data = null; this.resets = (this.resets || 0) + 1; }
    message(text, error) { this.lastMessage = {text, error}; }
    async load() {
      this.data = await (await this.api('/api/chat/workspace')).json();
      node('chat-library').links = this.data.chats.map(chat => ({dataset: {chatId: chat.chat_id}, href: '#local'}));
      this.render();
    }
    render() { this.originalRender = (this.originalRender || 0) + 1; }
  }
  let bootstraps = 0, currentSession = 'ses_owner';
  const fetch = async (path, options) => {
    calls.push({path, options});
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.redirect, 'error');
    assert.equal(options.signal.timeout, 15000);
    if (path === '/auth/session') {
      if (!authenticated) return {ok: false, status: 403};
      const bootstrap = options.headers?.['X-CSRF-Bootstrap'] === '1';
      if (bootstrap && bootstrapConflict && ++bootstraps === 1) return {ok: false, status: 409};
      return {ok: true, status: 200, json: async () => ({status: 'authenticated',
        session_id: bootstrap ? csrfSession : currentSession, ...(bootstrap ? {csrf_token: 'a'.repeat(32)} : {})})};
    }
    if (path === '/api/chat/workspace') {
      if (swapOnWorkspace || swapOnPost && options.method === 'POST') currentSession = 'ses_other';
      return {ok: true, status: 200,
        json: async () => ({revision: 0, folders: [], chats: [{chat_id: 'default'}, {chat_id: 'f'.repeat(32)}]})};
    }
    if (path === '/auth/signout') return {ok: true, status: 200, json: async () => ({status: 'signed_out'})};
    throw new Error('Unexpected request: ' + path);
  };
  const context = {document: {body: {dataset: {sharedNavigation: 'true'}}, getElementById: node},
    window: {CampusPulseChats: Chats}, fetch, AbortSignal: {timeout: timeout => ({timeout})},
    location: {assign: url => destinations.push(url)}, sessionStorage: storage};
  vm.runInNewContext(script, context);
  return {node, calls, destinations, instances, storage};
}
const settle = async () => { for (let index = 0; index < 8; ++index) await new Promise(resolve => setImmediate(resolve)); };

(async () => {
  const live = setup();
  await settle();
  assert.equal(live.instances.length, 1);
  const chats = live.instances[0];
  assert.deepEqual(live.calls.map(item => [item.path, item.options.method || 'GET']),
    [['/auth/session', 'GET'], ['/api/chat/workspace', 'GET'], ['/auth/session', 'GET']]);
  assert.equal(live.node('signout').hidden, false);
  assert.equal(chats.isBlocked, false);
  assert.equal(chats.originalRender, 1);
  assert.deepEqual(live.node('chat-library').links.map(link => link.href), ['/#workspace', '/#chat/' + 'f'.repeat(32)]);
  chats.navigate('draft'); chats.navigate('draft-' + 'a'.repeat(32)); chats.navigate('default');
  chats.navigate('https://foreign.example'); chats.navigate('../settings');
  assert.deepEqual(live.destinations, ['/#new', '/#new/' + 'a'.repeat(32), '/#workspace']);
  const change = {action: 'rename_chat', item_id: 'f'.repeat(32), title: 'Математика', revision: 0};
  await chats.api('/api/chat/workspace', change);
  const posted = live.calls.findLast(call => call.options.method === 'POST');
  assert.equal(posted.path, '/api/chat/workspace'); assert.equal(posted.options.method, 'POST');
  assert.equal(posted.options.headers['X-CSRF-Token'], 'a'.repeat(32));
  assert.deepEqual(JSON.parse(posted.options.body), change);
  await assert.rejects(() => chats.api('/api/calendar/apply', {}), error => error.status === 403);
  assert.ok(live.calls.every(call => !call.path.startsWith('/api/calendar')));

  const swapped = setup({csrfSession: 'ses_other'});
  await settle();
  await assert.rejects(() => swapped.instances[0].api('/api/chat/workspace', change), error => error.status === 403);
  assert.ok(swapped.calls.every(call => call.options.method !== 'POST'));
  assert.equal(swapped.node('signout').hidden, true);
  assert.equal(swapped.instances[0].isBlocked, true);

  const lateRead = setup({swapOnWorkspace: true});
  await settle();
  assert.equal(lateRead.instances[0].data, null);
  assert.equal(lateRead.node('signout').hidden, true);
  assert.equal(lateRead.instances[0].isBlocked, true);
  assert.equal(lateRead.instances[0].originalRender, undefined);
  assert.ok(lateRead.calls.every(call => call.options.method !== 'POST'));

  const lateChange = setup({swapOnPost: true});
  await settle();
  await assert.rejects(() => lateChange.instances[0].api('/api/chat/workspace', change), error => error.status === 403);
  assert.equal(lateChange.instances[0].data, null);
  assert.equal(lateChange.node('signout').hidden, true);
  assert.equal(lateChange.instances[0].isBlocked, true);
  assert.equal(lateChange.calls.filter(call => call.options.method === 'POST').length, 1);

  const retried = setup({bootstrapConflict: true});
  await settle();
  await retried.instances[0].api('/api/chat/workspace', change);
  assert.equal(retried.calls.filter(call => call.options.headers?.['X-CSRF-Bootstrap'] === '1').length, 2);
  assert.equal(retried.calls.filter(call => call.options.method === 'POST').length, 1);

  const denied = setup({authenticated: false});
  await settle();
  assert.equal(denied.node('signout').hidden, true);
  assert.equal(denied.instances[0].isBlocked, true);
  assert.equal(denied.instances[0].lastMessage.error, true);
  assert.ok(denied.calls.every(call => call.options.method !== 'POST'));
  assert.deepEqual(denied.destinations, []);

  const logout = setup();
  await settle();
  await logout.node('signout').listeners.click();
  assert.equal(logout.calls.at(-1).path, '/auth/signout');
  assert.equal(logout.calls.at(-1).options.method, 'POST');
  assert.deepEqual(logout.destinations, ['/login']);
  assert.equal(logout.storage['campuspulse-draft-v1:ses_owner:draft'], undefined);
  assert.equal(logout.storage['campuspulse-draft-v1:ses_other:draft'], 'other');
  console.log('Shared navigation read-only startup, root links, CSRF isolation and explicit actions passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
