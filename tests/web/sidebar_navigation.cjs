'use strict';
// Only browser primitives are doubled; every transition uses the shipped JS.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = JSON.parse(fs.readFileSync(0, 'utf8'));
const key = 'campuspulse.sidebar.v1';

function harness(options = {}) {
  const elements = [], writes = [], listeners = {}, windowListeners = {};
  let observer = null;
  const saved = options.saved === undefined ? null : options.saved;
  class Element {
    constructor(tag = 'div', id = '') {
      this.tagName = tag; this.id = id; this.children = []; this.parentElement = null;
      this.attributes = {}; this.dataset = {}; this.events = {}; this.inert = false;
      this.hidden = false; this.disabled = false; this.capture = new Set();
      this.style = {values: {}, setProperty(name, value) {this.values[name] = value;}};
      elements.push(this);
    }
    setAttribute(name, value) {this.attributes[name] = String(value);}
    getAttribute(name) {return this.attributes[name] ?? null;}
    removeAttribute(name) {delete this.attributes[name];}
    addEventListener(name, handler) {(this.events[name] ||= []).push(handler);}
    fire(name, event = {}) {
      const input = {target: this, currentTarget: this, defaultPrevented: false,
        preventDefault() {this.defaultPrevented = true;}, ...event};
      for (const handler of this.events[name] || []) handler(input);
      return input;
    }
    append(...children) {for (const child of children) {child.parentElement = this; this.children.push(child);}}
    prepend(child) {child.parentElement = this; this.children.unshift(child);}
    after(child) {this.parentElement?.append(child);}
    contains(element) {return element === this || this.children.some(child => child.contains(element));}
    focus() {if (!this.inert && !this.disabled) document.activeElement = this;}
    matches(selector) {
      if (selector.startsWith('#')) return this.id === selector.slice(1);
      if (selector.startsWith('.')) return (this.className || '').split(' ').includes(selector.slice(1));
      if (selector === '[hidden]') return this.hidden;
      if (selector === '[inert]') return this.inert;
      return false;
    }
    closest(selector) {
      return selector.split(',').some(part => this.matches(part.trim())) ? this : this.parentElement?.closest(selector) || null;
    }
    querySelectorAll(selector) {
      const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
      if (selector === '*') return descendants;
      if (selector.startsWith('a[href]')) return descendants.filter(child => ['button', 'a', 'input', 'select'].includes(child.tagName) && !child.disabled);
      return descendants.filter(child => selector.split(',').some(part => child.matches(part.trim())));
    }
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
    getClientRects() {return this.hidden ? [] : [{}];}
    setPointerCapture(id) {this.capture.add(id);}
    hasPointerCapture(id) {return this.capture.has(id);}
    releasePointerCapture(id) {this.capture.delete(id); this.fire('lostpointercapture');}
  }
  const root = new Element('html'); root.clientWidth = options.width || 1440;
  const body = new Element('body'); root.append(body);
  body.dataset.sharedNavigation = options.standalone ? 'true' : 'false';
  const sidebar = new Element('aside', 'sidebar'); sidebar.className = 'sidebar'; body.append(sidebar);
  const header = new Element(); header.className = 'sidebar-header'; sidebar.append(header);
  const toggle = new Element('button', 'sidebar-toggle'); header.append(toggle);
  const brand = new Element('a'); brand.className = 'brand'; header.append(brand);
  const newChat = new Element('button', 'chat-new'); sidebar.append(newChat);
  const link = new Element('a'); link.className = 'nav-link'; sidebar.append(link);
  const library = new Element('section'); library.className = 'chat-library'; sidebar.append(library);
  const chatLink = new Element('a'); chatLink.className = 'chat-link'; library.append(chatLink);
  const footer = new Element(); footer.className = 'sidebar-expanded-only'; sidebar.append(footer);
  const signout = new Element('button', 'signout'); sidebar.append(signout);
  const mobile = new Element('button', 'mobile-nav-toggle'); body.append(mobile);
  const outside = new Element('main'); body.append(outside);
  const document = {
    documentElement: root, body, activeElement: outside,
    getElementById: id => elements.find(element => element.id === id) || null,
    createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    querySelector: () => options.dialogOpen ? {} : null,
    addEventListener(name, handler) {(listeners[name] ||= []).push(handler);},
  };
  const desktop = {matches: options.desktop !== false, addEventListener(name, handler) {this.changed = handler;}};
  const sandbox = {
    document, window: {matchMedia: () => desktop, addEventListener(name, handler) {windowListeners[name] = handler;}},
    MutationObserver: class {constructor(handler) {observer = handler;} observe() {}},
    localStorage: {
      getItem() {if (options.storageBlocked) throw new Error('denied'); return saved;},
      setItem(name, value) {if (options.storageBlocked) throw new Error('denied'); writes.push([name, JSON.parse(value)]);},
    },
  };
  vm.runInNewContext(source.head, sandbox);
  const beforeScript = {...root.dataset};
  vm.runInNewContext(source.script, sandbox);
  const handle = document.getElementById('sidebar-resize');
  const dispatch = (name, event = {}) => {
    const input = {target: outside, defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}, ...event};
    for (const handler of listeners[name] || []) handler(input);
    return input;
  };
  return {root, body, sidebar, toggle, brand, newChat, link, library, chatLink, footer, signout, mobile, handle, outside,
    document, writes, beforeScript, desktop, options,
    width: () => body.style.values['--cp-sidebar-width'],
    media(matches, width) {desktop.matches = matches; if (width) root.clientWidth = width; desktop.changed();},
    resize(width) {root.clientWidth = width; windowListeners.resize();},
    nav(open) {body.dataset.navOpen = String(open); observer();},
    storage(value) {windowListeners.storage({key, newValue: value});},
    dispatch,
  };
}

// Persisted collapse is applied before paint and never disables the icon rail.
{
  const h = harness({saved: JSON.stringify({width: 312, collapsed: true})});
  assert.equal(h.beforeScript.sidebarCollapsed, 'true');
  assert.equal(h.width(), '72px'); assert.equal(h.sidebar.inert, false);
  assert.equal(h.sidebar.getAttribute('aria-hidden'), null);
  assert.equal(h.library.inert, true); assert.equal(h.brand.inert, true);
  assert.equal(h.link.inert, false); assert.equal(h.newChat.inert, false); assert.equal(h.signout.inert, false);
  assert.equal(h.toggle.getAttribute('aria-expanded'), 'false');
  h.toggle.fire('click');
  assert.equal(h.width(), '312px'); assert.equal(h.library.inert, false);
  assert.equal(h.toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(h.writes.at(-1)[0], key); assert.equal(h.writes.at(-1)[1].collapsed, false);
}
// Focus never remains in concealed history, including external storage updates.
{
  const h = harness(); h.chatLink.focus(); h.toggle.fire('click');
  assert.equal(h.document.activeElement, h.toggle);
  h.storage(JSON.stringify({width: 360, collapsed: false}));
  assert.equal(h.width(), '360px'); assert.equal(h.library.inert, false);
  h.brand.focus(); h.storage(JSON.stringify({width: 360, collapsed: true}));
  assert.equal(h.document.activeElement, h.toggle);
}
// Keyboard resize is bounded and preserves expansion width across toggles.
{
  const h = harness();
  h.handle.fire('keydown', {key: 'End'}); assert.equal(h.width(), '400px');
  h.handle.fire('keydown', {key: 'Home'}); assert.equal(h.width(), '72px');
  h.handle.fire('keydown', {key: 'Enter'}); assert.equal(h.width(), '400px');
  h.handle.fire('keydown', {key: 'ArrowLeft', shiftKey: true}); assert.equal(h.width(), '360px');
  h.resize(800); assert.equal(h.width(), '280px');
  h.resize(1440); assert.equal(h.width(), '360px');
  h.handle.fire('keydown', {key: 'Home'});
  h.handle.fire('keydown', {key: 'ArrowRight'}); assert.equal(h.width(), '240px');
  h.handle.fire('keydown', {key: 'ArrowLeft'}); assert.equal(h.width(), '72px');
}
// Pointer cancellation restores the pre-drag state and never persists a partial drag.
{
  const h = harness();
  const down = (id = 1) => h.handle.fire('pointerdown', {isPrimary: true, button: 0, pointerId: id, clientX: 280});
  down(); h.handle.fire('pointermove', {pointerId: 1, clientX: 100});
  assert.equal(h.width(), '72px'); h.handle.fire('pointercancel');
  assert.equal(h.width(), '280px'); assert.equal(h.writes.length, 0);
  assert.equal(h.body.dataset.sidebarResizing, undefined);
  down(2); h.handle.fire('pointermove', {pointerId: 2, clientX: 340}); h.handle.fire('pointerup');
  assert.equal(h.width(), '340px'); assert.equal(h.writes.at(-1)[1].width, 340);
  down(3); h.handle.fire('pointermove', {pointerId: 3, clientX: 100}); h.handle.fire('keydown', {key: 'Escape'});
  assert.equal(h.width(), '340px'); assert.equal(h.writes.length, 1);
  down(4); h.handle.fire('pointermove', {pointerId: 4, clientX: 100}); h.resize(1440);
  assert.equal(h.width(), '340px'); assert.equal(h.body.dataset.sidebarResizing, undefined);
}
// Standalone drawers retain mobile accessibility, focus restoration and modal guards.
{
  const h = harness({desktop: false, width: 390, standalone: true, saved: JSON.stringify({collapsed: true, width: 300})});
  assert.equal(h.sidebar.inert, true); assert.equal(h.handle.tabIndex, -1);
  h.mobile.fire('click'); assert.equal(h.body.dataset.navOpen, 'true'); assert.equal(h.sidebar.inert, false);
  assert.equal(h.library.inert, false); assert.equal(h.document.activeElement, h.toggle);
  h.signout.focus(); assert.equal(h.dispatch('keydown', {key: 'Tab'}).defaultPrevented, true);
  assert.equal(h.document.activeElement, h.toggle);
  h.options.dialogOpen = true; h.dispatch('keydown', {key: 'Escape'}); assert.equal(h.body.dataset.navOpen, 'true');
  h.options.dialogOpen = false; h.dispatch('keydown', {key: 'Escape'});
  assert.equal(h.sidebar.inert, true); assert.equal(h.document.activeElement, h.mobile);
  h.mobile.fire('click'); h.dispatch('click', {target: h.outside}); assert.equal(h.body.dataset.navOpen, 'false');
  h.mobile.fire('click'); h.toggle.fire('click'); assert.equal(h.body.dataset.navOpen, 'false');
  h.media(true, 1440); assert.equal(h.width(), '72px'); assert.equal(h.sidebar.inert, false);
  h.media(false, 390); assert.equal(h.body.dataset.navOpen, 'false');
}
// Main chat owns its existing mobile handlers; observing navOpen still updates inert.
{
  const h = harness({desktop: false, width: 390});
  assert.equal(h.mobile.events.click, undefined); h.nav(true); assert.equal(h.sidebar.inert, false);
  h.nav(false); assert.equal(h.sidebar.inert, true);
}
// Bad or blocked localStorage cannot break controls or persist attacker-sized widths.
for (const settings of [{storageBlocked: true}, {saved: '{broken'}, {saved: JSON.stringify({width: -20, collapsed: 'true'})}]) {
  const h = harness(settings); assert.equal(h.width(), '280px');
  h.toggle.fire('click'); assert.equal(h.width(), '72px'); h.toggle.fire('click'); assert.equal(h.width(), '280px');
}
{
  const h = harness({saved: JSON.stringify({width: 1e12, collapsed: false})});
  assert.equal(h.width(), '400px');
}
console.log('Sidebar navigation state, focus, persistence, resize and mobile regressions passed.');
