// Run the shipped recording handlers with controlled browser media boundaries.
// No microphone, network, provider, or recorded personal audio is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = process.argv[2] || process.cwd();
const source = fs.readFileSync(path.join(root, 'src/campuspulse_recovery/web/assets/client.py'), 'utf8');
const start = source.indexOf('  let speechAvailable');
const end = source.indexOf('  const moodleEnabled', start);
assert(start >= 0 && end > start, 'Recording adapter boundary is missing');
const recordingSource = source.slice(start, end);
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
async function until(predicate) {
  for (let index = 0; index < 30 && !predicate(); index++) await Promise.resolve();
  assert(predicate(), 'Expected asynchronous recording boundary was not reached');
}

function harness(options = {}) {
  const elements = new Map(), windowEvents = new Map(), timers = new Map(), frames = new Map();
  const streams = [], audios = [], recordings = [], requests = [];
  let now = 0, nextId = 0;
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', hidden: false, disabled: false, maxLength: 4000,
      attributes: {}, dataset: {}, style: {setProperty() {}}, events: new Map(),
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.events.set(name, callback); },
      dispatchEvent(event) { this.events.get(event.type)?.(event); },
      focus() {},
    });
    return elements.get(id);
  };
  let savedDraft = '';
  $('recovery-message').addEventListener('input', () => { savedDraft = $('recovery-message').value; });
  function stream() {
    const track = {stopped: false, stop() { this.stopped = true; }};
    const value = {track, getTracks: () => [track]};
    streams.push(value);
    return value;
  }
  class AudioContext {
    constructor() { this.closed = false; audios.push(this); }
    resume() { return options.resume?.(this) ?? Promise.resolve(); }
    close() { this.closed = true; return Promise.resolve(); }
    createAnalyser() {
      return {fftSize: 2048, getFloatTimeDomainData(samples) { samples.fill(options.silent ? 0 : 0.05); }};
    }
    createMediaStreamSource() { return {connect() {}}; }
  }
  class MediaRecorder {
    static isTypeSupported(type) { return options.supported !== false && type === 'audio/mp4'; }
    constructor(input, settings) {
      this.input = input; this.mimeType = settings.mimeType; this.state = 'inactive';
      recordings.push(this);
    }
    start() { this.state = 'recording'; }
    stop() {
      assert.equal(this.state, 'recording', 'A recording must only be stopped once');
      this.state = 'inactive';
      queueMicrotask(() => {
        this.ondataavailable?.({data: new Blob(['synthetic audio'], {type: this.mimeType})});
        this.stopped = Promise.resolve(this.onstop?.());
      });
    }
    error() {
      this.state = 'inactive';
      this.onerror?.({error: new Error('synthetic device failure')});
      this.ondataavailable?.({data: new Blob(['incomplete audio'], {type: this.mimeType})});
      this.stopped = Promise.resolve(this.onstop?.());
    }
  }
  const sandbox = {
    $, Blob, Event, Float32Array, AbortSignal, performance: {now: () => now},
    MediaRecorder,
    window: {isSecureContext: true, MediaRecorder, AudioContext,
      addEventListener(name, callback) {windowEvents.set(name, callback);}},
    navigator: {mediaDevices: {getUserMedia: settings => options.permission?.(settings) ?? Promise.resolve(stream())}},
    requestAnimationFrame(callback) { const id = ++nextId; frames.set(id, callback); return id; },
    cancelAnimationFrame(id) { frames.delete(id); },
    setTimeout(callback, delay) { const id = ++nextId; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    csrf: async () => 'fresh-csrf-token',
    saveDraft() { savedDraft = $('recovery-message').value; },
    fetch: async (url, request) => {
      requests.push({url, request});
      return options.transcribe?.(url, request) ?? {ok: true, json: async () => ({text: 'Распознанная фраза'})};
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let scope = 'session-one', generation = 0, busy = false, chatBusy = false,
      chatWorking = false, chatLimitReached = false;
    function controls() { voiceControls(); }
    ${recordingSource}
    speechAvailable = true;
    controls();
  `, sandbox);
  return {
    $, streams, audios, recordings, requests, timers, frames, stream, sandbox,
    run: text => vm.runInContext(text, sandbox),
    click: () => $('voice-toggle').events.get('click')(),
    pagehide: () => windowEvents.get('pagehide')(),
    saved: () => savedDraft,
    advance(milliseconds = 1500, count = 6) {
      now += milliseconds;
      for (let index = 0; index < count; index++) {
        const callbacks = [...frames.values()]; frames.clear();
        callbacks.forEach(callback => callback(now));
      }
    },
    async settle() {
      for (let index = 0; index < 8; index++) await Promise.resolve();
      await Promise.all(recordings.map(recording => recording.stopped));
    },
  };
}

async function recordingAppendsToDraftWithoutSendingChat() {
  const h = harness();
  h.$('recovery-message').value = 'Мой черновик';
  await h.click();
  assert.equal(h.run('voiceState'), 'recording');
  assert.equal(h.$('voice-toggle').attributes['aria-pressed'], 'true');
  h.advance();
  assert(h.$('voice-level').value > 0, 'The level meter should respond to input');
  await h.click(); await h.settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, '/api/chat/transcribe', 'Voice input must never auto-send a chat message');
  assert.equal(h.requests[0].request.headers['X-CSRF-Token'], 'fresh-csrf-token');
  assert.equal(h.requests[0].request.headers['Content-Type'], 'audio/mp4');
  assert.equal(h.requests[0].request.body.type, 'audio/mp4');
  assert(h.requests[0].request.body.size > 0);
  assert.equal(h.$('recovery-message').value, 'Мой черновик\nРаспознанная фраза');
  assert.equal(h.saved(), h.$('recovery-message').value, 'Transcription must pass through draft persistence');
  assert.equal(h.run('voiceState'), 'idle');
  assert(h.streams[0].track.stopped && h.audios[0].closed);
  assert.equal(h.timers.size, 0);
  assert.equal(h.frames.size, 0);
}

async function deniedMicrophonePreservesDraftAndCanRetry() {
  const denied = Object.assign(new Error('denied'), {name: 'NotAllowedError'});
  const h = harness({permission: () => Promise.reject(denied)});
  h.$('recovery-message').value = 'Не потерять текст';
  await h.click();
  assert.equal(h.run('voiceState'), 'idle');
  assert.equal(h.$('voice-toggle').disabled, false);
  assert.equal(h.$('recovery-message').value, 'Не потерять текст');
  assert.equal(h.requests.length, 0);
  assert.match(h.$('voice-status').textContent, /Разрешите доступ/);
}

async function shortOrSilentRecordingDoesNotUpload() {
  for (const silent of [false, true]) {
    const h = harness({silent});
    await h.click();
    h.advance(silent ? 2000 : 100, 6);
    await h.click(); await h.settle();
    assert.equal(h.requests.length, 0);
    assert.equal(h.run('voiceState'), 'idle');
    assert(h.streams[0].track.stopped && h.audios[0].closed);
    assert.match(h.$('voice-status').textContent, /достаточно звука/);
  }
}

async function maximumDurationStopsAndTranscribesOnce() {
  const h = harness();
  await h.click(); h.advance(60000);
  const timer = [...h.timers.values()].find(value => value.delay === 60000);
  assert(timer, 'The recorder must have a one-minute stop timer');
  timer.callback(); await h.settle();
  assert.equal(h.requests.length, 1);
  assert(h.streams[0].track.stopped);
  assert.equal(h.run('voiceState'), 'idle');
}

async function oversizedRecordingIsReleasedWithoutUpload() {
  const h = harness();
  await h.click(); h.advance();
  h.recordings[0].ondataavailable({data: new Blob([new Uint8Array(4 * 1024 * 1024 + 1)])});
  await h.settle();
  assert.equal(h.requests.length, 0);
  assert(h.streams[0].track.stopped && h.audios[0].closed);
  assert.equal(h.run('voiceState'), 'idle');
}

async function transcriptionFailureKeepsTypedText() {
  for (const status of [403, 503]) {
    const h = harness({transcribe: () => ({ok: false, status})});
    h.$('recovery-message').value = 'Существующий текст';
    await h.click(); h.advance(); await h.click(); await h.settle();
    assert.equal(h.$('recovery-message').value, 'Существующий текст');
    assert.equal(h.run('voiceState'), 'idle');
    assert.equal(h.$('voice-toggle').disabled, false);
    assert.match(h.$('voice-status').textContent, status === 403 ? /Сессия/ : /Не удалось/);
  }
}

async function transcriptTooLongDoesNotReplaceDraft() {
  const h = harness({transcribe: () => ({ok: true, json: async () => ({text: 'x'.repeat(4000)})})});
  h.$('recovery-message').value = 'Оставить текст';
  await h.click(); h.advance(); await h.click(); await h.settle();
  assert.equal(h.$('recovery-message').value, 'Оставить текст');
  assert.match(h.$('voice-status').textContent, /не поместился/);
}

async function expiredGenerationCannotAppendTranscription() {
  const reply = deferred();
  const h = harness({transcribe: () => reply.promise});
  h.$('recovery-message').value = 'До перехода';
  await h.click(); h.advance(); await h.click();
  await until(() => h.requests.length === 1);
  h.run('++generation');
  reply.resolve({ok: true, json: async () => ({text: 'Устаревшее распознавание'})});
  await h.settle();
  assert.equal(h.$('recovery-message').value, 'До перехода');
  assert.equal(h.run('voiceState'), 'idle');
}

async function pagehideCancelsPendingPermissionWithoutBlockingReturn() {
  const permission = deferred();
  const h = harness({permission: () => permission.promise});
  const pending = h.click();
  assert.equal(h.run('voiceState'), 'requesting');
  h.pagehide();
  assert.equal(h.run('voiceState'), 'idle', 'Returning from browser history must not wait for an abandoned microphone prompt');
  permission.resolve(h.stream());
  await pending;
  assert(h.streams[0].track.stopped, 'A late permission result must release its track');
  assert.equal(h.recordings.length, 0);
}

async function abandonedPermissionCannotStopANewerRecording() {
  const permission = deferred();
  let first = true;
  const h = harness({permission: () => {
    if (first) { first = false; return permission.promise; }
    return Promise.resolve(h.stream());
  }});
  const oldStart = h.click();
  h.pagehide();
  await h.click();
  assert.equal(h.run('voiceState'), 'recording');
  const current = h.streams[0];
  permission.resolve(h.stream()); await oldStart;
  assert.equal(h.run('voiceState'), 'recording');
  assert.equal(current.track.stopped, false, 'Abandoned capture must not stop the new microphone stream');
  assert.equal(h.streams[1].track.stopped, true);
  h.advance(); await h.click(); await h.settle();
}

async function abandonedAudioResumeCannotStopANewerRecording() {
  const resume = deferred();
  let first = true;
  const h = harness({resume: () => {
    if (first) { first = false; return resume.promise; }
    return Promise.resolve();
  }});
  const oldStart = h.click();
  await until(() => h.audios.length === 1);
  h.pagehide();
  await h.click();
  assert.equal(h.run('voiceState'), 'recording');
  resume.resolve(); await oldStart;
  assert.equal(h.run('voiceState'), 'recording');
  assert.equal(h.streams[1].track.stopped, false);
  assert.equal(h.recordings.length, 1, 'An abandoned AudioContext must not create another recorder');
  h.advance(); await h.click(); await h.settle();
}

async function abandonedTranscriptionCannotResetNewRecording() {
  const reply = deferred();
  const h = harness({transcribe: () => reply.promise});
  await h.click(); h.advance(); await h.click();
  await until(() => h.requests.length === 1);
  h.pagehide();
  await h.click();
  assert.equal(h.run('voiceState'), 'recording');
  reply.resolve({ok: true, json: async () => ({text: 'Не добавлять этот текст'})});
  await h.recordings[0].stopped;
  assert.equal(h.$('recovery-message').value, '');
  assert.equal(h.run('voiceState'), 'recording', 'A stale transcription cannot unlock or reset a newer capture');
  assert.equal(h.streams[1].track.stopped, false);
  h.pagehide(); await h.settle();
}

async function pagehideDuringRecordingDiscardsAudioAndReleasesDevices() {
  const h = harness();
  h.$('recovery-message').value = 'Сохранить черновик';
  await h.click(); h.advance();
  h.pagehide(); await h.settle();
  assert.equal(h.run('voiceState'), 'idle');
  assert.equal(h.requests.length, 0);
  assert(h.streams[0].track.stopped && h.audios[0].closed);
  assert.equal(h.saved(), 'Сохранить черновик');
}

async function recorderFailureReleasesMicrophoneAndAllowsRetry() {
  const h = harness();
  await h.click(); h.advance();
  h.recordings[0].error(); await h.settle();
  assert.equal(h.requests.length, 0);
  assert(h.streams[0].track.stopped && h.audios[0].closed);
  assert.equal(h.run('voiceState'), 'idle');
  await h.click();
  assert.equal(h.run('voiceState'), 'recording');
  h.advance(); await h.click(); await h.settle();
  assert.equal(h.requests.length, 1);
}

async function unsupportedAudioDoesNotAskForMicrophone() {
  const h = harness({supported: false});
  assert.equal(h.$('voice-toggle').disabled, true);
  await h.click();
  assert.equal(h.streams.length, 0);
  assert.equal(h.requests.length, 0);
}

const tests = [
  recordingAppendsToDraftWithoutSendingChat, deniedMicrophonePreservesDraftAndCanRetry,
  shortOrSilentRecordingDoesNotUpload, maximumDurationStopsAndTranscribesOnce,
  oversizedRecordingIsReleasedWithoutUpload, transcriptionFailureKeepsTypedText,
  transcriptTooLongDoesNotReplaceDraft, expiredGenerationCannotAppendTranscription,
  pagehideCancelsPendingPermissionWithoutBlockingReturn, abandonedPermissionCannotStopANewerRecording,
  abandonedAudioResumeCannotStopANewerRecording, abandonedTranscriptionCannotResetNewRecording,
  pagehideDuringRecordingDiscardsAudioAndReleasesDevices, recorderFailureReleasesMicrophoneAndAllowsRetry,
  unsupportedAudioDoesNotAskForMicrophone,
];
(async () => {
  let failed = 0;
  for (const test of tests) {
    let timeout;
    try {
      await Promise.race([test(), new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error('Recording scenario did not finish')), 1000);
      })]);
      console.log(`PASS ${test.name}`);
    }
    catch (error) { failed++; console.error(`FAIL ${test.name}\n${error.stack}`); }
    finally { clearTimeout(timeout); }
  }
  if (failed) process.exitCode = 1;
})();
