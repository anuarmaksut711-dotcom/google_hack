const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const script = fs.readFileSync(0, 'utf8');
const preferenceKey = 'campuspulse-theme';

function harness(options = {}) {
  const saved = new Map(options.saved === undefined ? [] : [[preferenceKey, options.saved]]);
  const writes = [];
  const documentListeners = new Map();
  const windowListeners = new Map();
  const meta = {content: 'dark light'};
  const root = {dataset: {}};
  let ready = options.readyState !== 'loading';
  function icon(theme) {
    return {
      dataset: {themeIcon: theme},
      hidden: theme === 'dark',
      toggleAttribute(name, force) {
        assert.equal(name, 'hidden');
        this.hidden = force;
      },
    };
  }
  function button() {
    const label = {textContent: ''};
    const icons = [icon('light'), icon('dark')];
    const attributes = new Map();
    return {
      label, icons, attributes, disabled: false, title: '',
      setAttribute(name, value) {attributes.set(name, value);},
      querySelector(selector) {assert.equal(selector, '[data-theme-label]'); return label;},
      querySelectorAll(selector) {assert.equal(selector, '[data-theme-icon]'); return icons;},
      closest(selector) {assert.equal(selector, '[data-theme-toggle]'); return this;},
    };
  }
  const buttons = [button(), button()];
  const storage = {
    getItem(key) {
      assert.equal(key, preferenceKey);
      if (options.readFailure) throw new Error('Storage read denied');
      return saved.get(key) ?? null;
    },
    setItem(key, value) {
      assert.equal(key, preferenceKey);
      if (options.writeFailure) throw new Error('Storage write denied');
      writes.push([key, value]);
      saved.set(key, value);
    },
  };
  const sandbox = {
    document: {
      documentElement: root,
      readyState: ready ? 'complete' : 'loading',
      querySelector(selector) {assert.equal(selector, 'meta[name="color-scheme"]'); return meta;},
      querySelectorAll(selector) {assert.equal(selector, '[data-theme-toggle]'); return ready ? buttons : [];},
      addEventListener(name, handler) {documentListeners.set(name, handler);},
    },
    window: {addEventListener(name, handler) {windowListeners.set(name, handler);}},
  };
  Object.defineProperty(sandbox, 'localStorage', {
    get() {
      if (options.accessFailure) throw new Error('Storage unavailable');
      return storage;
    },
  });
  vm.runInNewContext(script, sandbox);
  return {
    buttons, meta, root, writes, saved,
    ready() {
      ready = true;
      sandbox.document.readyState = 'complete';
      documentListeners.get('DOMContentLoaded')?.();
    },
    click(target = buttons[0]) {documentListeners.get('click')({target});},
    storage(key, newValue) {windowListeners.get('storage')({key, newValue});},
  };
}

function assertAppearance(view, theme) {
  assert.equal(view.root.dataset.theme, theme);
  assert.equal(view.meta.content, theme);
  for (const button of view.buttons) {
    const action = theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему';
    const label = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
    assert.equal(button.attributes.get('aria-label'), action);
    assert.equal(button.title, action);
    assert.equal(button.label.textContent, label);
    assert.equal(button.icons.find(icon => icon.dataset.themeIcon === 'light').hidden, theme !== 'dark');
    assert.equal(button.icons.find(icon => icon.dataset.themeIcon === 'dark').hidden, theme !== 'light');
  }
}

function savedChoiceAppliesBeforeDomReady() {
  const view = harness({saved: 'light', readyState: 'loading'});
  assert.equal(view.root.dataset.theme, 'light');
  assert.equal(view.meta.content, 'light');
  assert.equal(view.buttons[0].label.textContent, '');
  assert.equal(view.writes.length, 0);
  view.ready();
  assertAppearance(view, 'light');
}

function absentAndInvalidPreferencesStayDark() {
  for (const saved of [undefined, 'dark', '', 'system', '<script>light</script>']) {
    const view = harness({saved});
    assertAppearance(view, 'dark');
    assert.equal(view.writes.length, 0);
  }
}

function togglesPersistAndSynchronizeEveryControl() {
  const view = harness();
  view.click();
  assertAppearance(view, 'light');
  assert.equal(view.saved.get(preferenceKey), 'light');
  const child = {closest: () => view.buttons[1]};
  view.click(child);
  assertAppearance(view, 'dark');
  assert.deepEqual(view.writes, [[preferenceKey, 'light'], [preferenceKey, 'dark']]);
}

function savedChoiceSurvivesPageReload() {
  const first = harness();
  first.click();
  const reloaded = harness({saved: first.saved.get(preferenceKey)});
  assertAppearance(reloaded, 'light');
  assert.equal(reloaded.writes.length, 0);
}

function storageFailuresDoNotBreakTheControl() {
  for (const failure of ['readFailure', 'writeFailure', 'accessFailure']) {
    const view = harness({[failure]: true});
    assertAppearance(view, 'dark');
    view.click();
    assertAppearance(view, 'light');
    view.click();
    assertAppearance(view, 'dark');
  }
}

function unrelatedAndDisabledClicksDoNothing() {
  const view = harness();
  view.click({closest: () => null});
  view.click({});
  view.buttons[0].disabled = true;
  view.click();
  assertAppearance(view, 'dark');
  assert.equal(view.writes.length, 0);
}

function crossTabChangesUpdateWithoutWritingBack() {
  const view = harness();
  view.storage(preferenceKey, 'light');
  assertAppearance(view, 'light');
  view.storage('unrelated-key', 'dark');
  assertAppearance(view, 'light');
  view.storage(preferenceKey, 'dark');
  assertAppearance(view, 'dark');
  view.storage(preferenceKey, 'light');
  view.storage(null, null);
  assertAppearance(view, 'dark');
  view.storage(preferenceKey, 'unexpected-value');
  assertAppearance(view, 'dark');
  assert.equal(view.writes.length, 0);
}

function crossTabChangeBeforeDomReadyReachesNewControls() {
  const view = harness({readyState: 'loading'});
  view.storage(preferenceKey, 'light');
  view.ready();
  assertAppearance(view, 'light');
  view.click();
  assertAppearance(view, 'dark');
}

for (const test of [
  savedChoiceAppliesBeforeDomReady, absentAndInvalidPreferencesStayDark,
  togglesPersistAndSynchronizeEveryControl, savedChoiceSurvivesPageReload,
  storageFailuresDoNotBreakTheControl, unrelatedAndDisabledClicksDoNothing,
  crossTabChangesUpdateWithoutWritingBack, crossTabChangeBeforeDomReadyReachesNewControls,
]) {
  test();
  console.log('PASS', test.name);
}
