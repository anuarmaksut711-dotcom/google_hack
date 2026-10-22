'use strict';
// Execute the shipped module with synthetic browser/WebGL boundaries.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {pathToFileURL} = require('node:url');

const root = process.argv[2] || process.cwd();
const filename = path.join(root, 'src/campuspulse_recovery/web/assets/logo/welcome.js');
// VM scripts cannot load ESM imports or import.meta; the module body stays intact.
const source = fs.readFileSync(filename, 'utf8')
  .replace(/^import\s+\{[^}]+\}\s+from\s+['"][^'"]+['"];?\s*$/gm, '')
  .replace(/import\.meta\.url/g, JSON.stringify(pathToFileURL(filename).href));

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler, options) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({handler, once: Boolean(options?.once)});
  }
  removeEventListener(type, handler) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item.handler !== handler));
  }
  dispatchEvent(event) {
    event.target ||= this;
    for (const item of [...(this.listeners.get(event.type) || [])]) {
      if (item.once) this.removeEventListener(event.type, item.handler);
      item.handler(event);
    }
  }
}

class Element extends Events {
  constructor(id) {
    super();
    this.id = id;
    this.dataset = {};
    this.value = '';
    this.hidden = false;
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
    };
  }
  closest() { return this.editable ? this : null; }
  getBoundingClientRect() { return {width: 280, height: 280}; }
  getContext() { return {putImageData() {}}; }
}

async function harness({draft = '', hasChat = false, reduced = false} = {}) {
  const nodes = new Map(['welcome-logo', 'welcome-canvas', 'welcome-fallback',
    'recovery-message', 'workspace', 'start'].map(id => [id, new Element(id)]));
  const node = id => nodes.get(id);
  node('recovery-message').editable = true;
  node('recovery-message').value = draft;
  if (hasChat) node('workspace').classList.add('has-chat');
  const document = Object.assign(new Events(), {hidden: false, getElementById: node});
  const window = new Events();
  const motion = Object.assign(new Events(), {matches: reduced});
  const frames = new Map(), tickers = new Set(), intersections = [], resizes = [];
  let frameId = 0;
  const scene = {renders: 0, disposals: 0, sizes: [],
    render() { this.renders++; },
    resize(...size) { this.sizes.push(size); },
    dispose() { this.disposals++; },
  };
  const timelines = [];
  let creations = 0;
  const gsap = {
    ticker: {lagSmoothing() {}, add: callback => tickers.add(callback), remove: callback => tickers.delete(callback)},
    timeline(settings) {
      const timeline = {paused: settings.paused, killed: false, position: 0,
        addLabel() { return this; }, to() { return this; },
        seek(value) { this.position = value; return this; },
        time() { return this.position; },
        pause() { this.paused = true; return this; },
        resume() { this.paused = false; return this; },
        kill() { this.killed = true; return this; },
        progress(value) { if (value === 1) this.position = 5.2; return this; },
        finish() { this.position = 5.2; settings.onComplete(); },
      };
      timelines.push(timeline);
      return timeline;
    },
  };
  class IntersectionObserver {
    constructor(callback) { this.callback = callback; this.disconnected = false; intersections.push(this); }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
    notify(isIntersecting) {
      if (!this.disconnected) this.callback([{target: this.target, isIntersecting}]);
    }
  }
  class ResizeObserver {
    constructor(callback) { this.callback = callback; this.disconnected = false; resizes.push(this); }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
  }
  const sandbox = {document, window, Element, gsap, URL, Uint8ClampedArray,
    ImageData: class {}, IntersectionObserver, ResizeObserver,
    matchMedia: () => motion,
    requestAnimationFrame(callback) { const id = ++frameId; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
    async createParticleScene() { creations++; return scene; },
  };
  await vm.runInNewContext(`(async () => {\n${source}\n})()`, sandbox, {filename});
  const flushFrame = () => {
    for (const [id, callback] of [...frames]) { frames.delete(id); callback(); }
  };
  flushFrame();
  return {node, document, window, motion, scene, timelines, tickers, intersections, resizes, frames,
    get creations() { return creations; },
    input(field, text) {
      field.value = text;
      document.dispatchEvent({type: 'input', target: field});
    },
    intersect(visible) { intersections.forEach(observer => observer.notify(visible)); },
    visibility(hidden) { document.hidden = hidden; document.dispatchEvent({type: 'visibilitychange'}); },
    tick() { for (const callback of [...tickers]) callback(); },
  };
}

const cases = [];
function test(name, run) { cases.push({name, run}); }

function assertRetained(h, message) {
  assert.equal(h.creations, 1, message);
  assert.equal(h.scene.disposals, 0, message);
  assert.equal(h.node('welcome-logo').classList.contains('is-dismissed'), false, message);
  assert.equal(h.timelines[0].killed, false, message);
}

test('a restored draft keeps the welcome scene', async () => {
  const h = await harness({draft: 'Помоги подготовиться к экзамену'});
  assertRetained(h, 'Restoring a saved draft must preserve the welcome screen');
  assert.equal(h.node('recovery-message').value, 'Помоги подготовиться к экзамену');
  assert.equal(h.node('welcome-logo').dataset.state, 'playing');
  assert(h.scene.renders > 0);
});

test('typing in the composer or another editable field keeps the scene', async () => {
  const h = await harness();
  h.input(h.node('recovery-message'), 'Нужен план на неделю');
  assertRetained(h, 'The first composer input must not destroy the welcome scene');
  const search = new Element('sidebar-search');
  search.editable = true;
  h.input(search, 'История чата');
  assertRetained(h, 'Typing in an unrelated input must not destroy the welcome scene');
  h.timelines[0].finish();
  h.input(h.node('recovery-message'), 'Нужен план на неделю!');
  assertRetained(h, 'Typing after the animation settles must preserve the logo');
  assert.equal(h.node('welcome-logo').dataset.state, 'settled');
});

test('an existing conversation can return to a new chat with the same logo', async () => {
  const h = await harness({hasChat: true});
  h.document.dispatchEvent({type: 'campuspulse:chat-history'});
  h.intersect(false);
  assertRetained(h, 'Loading chat history must preserve the reusable welcome scene');
  assert.equal(h.timelines[0].paused, true);
  h.node('workspace').classList.remove('has-chat');
  h.intersect(true);
  assertRetained(h, 'Returning to a new chat must retain a working welcome scene');
  assert.equal(h.timelines[0].paused, false);
  h.node('start').dispatchEvent({type: 'click'});
  assertRetained(h, 'Starting a request must not permanently dismiss the logo');
});

test('offscreen and background animation pauses and resumes', async () => {
  const h = await harness();
  const timeline = h.timelines[0];
  h.visibility(true);
  const backgroundRenders = h.scene.renders;
  h.tick();
  assert.equal(timeline.paused, true);
  assert.equal(h.tickers.size, 0);
  assert.equal(h.scene.renders, backgroundRenders);
  h.visibility(false);
  assert.equal(timeline.paused, false);
  h.tick();
  assert(h.scene.renders > backgroundRenders);
  h.intersect(false);
  assert.equal(timeline.paused, true);
  assert.equal(h.tickers.size, 0);
  h.intersect(true);
  assert.equal(timeline.paused, false);
  assert.equal(h.tickers.size, 1);
  assertRetained(h, 'Hiding the scene must pause it without disposing it');
});

test('reduced motion renders a settled logo without animation', async () => {
  for (const initial of [true, false]) {
    const h = await harness({reduced: initial});
    if (!initial) {
      h.motion.matches = true;
      h.motion.dispatchEvent({type: 'change'});
    }
    assertRetained(h, 'Reduced motion must preserve the logo');
    assert.equal(h.node('welcome-logo').dataset.state, 'settled');
    assert.equal(h.timelines[0].paused, true);
    assert.equal(h.tickers.size, 0);
    assert(h.scene.renders > 0);
  }
});

test('leaving the page releases rendering resources once', async () => {
  const h = await harness();
  h.window.dispatchEvent({type: 'pagehide'});
  assert.equal(h.scene.disposals, 1);
  assert.equal(h.timelines[0].killed, true);
  assert.equal(h.tickers.size, 0);
  assert.equal(h.frames.size, 0);
  assert(h.resizes.every(observer => observer.disconnected));
  assert(h.intersections.every(observer => observer.disconnected));
  const renders = h.scene.renders;
  h.window.dispatchEvent({type: 'pagehide'});
  h.visibility(true);
  h.visibility(false);
  h.intersect(true);
  h.tick();
  assert.equal(h.scene.disposals, 1);
  assert.equal(h.scene.renders, renders);
});

(async () => {
  let failures = 0;
  for (const {name, run} of cases) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { failures++; console.error(`FAIL ${name}\n${error.stack}`); }
  }
  process.exitCode = failures ? 1 : 0;
})();
