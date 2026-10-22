const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const script = fs.readFileSync(0, 'utf8');

function harness({saved = 'light', rootTheme = 'light', readyState = 'loading', hasMeta = true} = {}) {
  const root = {dataset: {theme: rootTheme}};
  const meta = hasMeta ? {content: 'dark light'} : null;
  const accesses = [];
  const listeners = [];
  const sandbox = {
    document: {
      documentElement: root,
      readyState,
      querySelector(selector) {assert.equal(selector, 'meta[name="color-scheme"]'); return meta;},
      addEventListener(name) {listeners.push(['document', name]);},
    },
    window: {
      addEventListener(name) {listeners.push(['window', name]);},
      matchMedia() {accesses.push('system preference'); return {matches: true};},
    },
  };
  Object.defineProperty(sandbox, 'localStorage', {get() {
    accesses.push('localStorage');
    if (saved === 'denied') throw new Error('Storage unavailable');
    return {getItem() {return saved;}, setItem() {throw new Error('No preference writes expected');}};
  }});
  vm.runInNewContext(script, sandbox);
  return {root, meta, accesses, listeners};
}

function darkAppliesBeforeThePageIsReady() {
  const view = harness();
  assert.equal(view.root.dataset.theme, 'dark');
  assert.equal(view.meta.content, 'dark');
}

function oldPreferencesNeverChangeThePage() {
  for (const saved of ['light', 'dark', 'system', '', null, 'denied']) {
    const view = harness({saved});
    assert.equal(view.root.dataset.theme, 'dark');
    assert.equal(view.meta.content, 'dark');
    assert.deepEqual(view.accesses, []);
  }
}

function noSwitchingOrCrossTabListenersRemain() {
  const view = harness({readyState: 'complete'});
  assert.deepEqual(view.listeners, []);
  assert.deepEqual(view.accesses, []);
}

function pagesWithoutSchemeMetadataStillStayDark() {
  const view = harness({hasMeta: false});
  assert.equal(view.root.dataset.theme, 'dark');
  assert.equal(view.meta, null);
  assert.deepEqual(view.listeners, []);
}

for (const test of [darkAppliesBeforeThePageIsReady, oldPreferencesNeverChangeThePage,
  noSwitchingOrCrossTabListenersRemain, pagesWithoutSchemeMetadataStillStayDark]) {
  test();
  console.log('PASS', test.name);
}
