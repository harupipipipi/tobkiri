// Script/controller tests only; actual native rejection needs the macOS job.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, 'root.js'), 'utf8');
const commands = [
  'core_start_profile', 'core_stop_session', 'core_host_output_ack',
  'pack_ui_invoke', 'pack_ui_register', 'pack_ui_cancel', 'pack_ui_ack',
  'pack_ui_detach', 'pack_ui_stream_pull', 'pack_ui_stream_cancel',
  'core_terminal_reply',
];

test('attack and root inventory match actual native command names, not just count', () => {
  const root = path.resolve(__dirname, '../..');
  const actualSource = fs.readFileSync(path.join(
    root, 'tobkiri_launcher/src-tauri/src/core_host_commands.rs',
  ), 'utf8');
  const actual = [...actualSource.matchAll(/"((?:core_|pack_ui_)[a-z_]+)"/g)]
    .map((match) => match[1]);
  const rootList = script.match(/const commands = \[([\s\S]*?)\];/)[1];
  const rootCommands = [...rootList.matchAll(/'([^']+)'/g)].map((match) => match[1]);
  const attack = fs.readFileSync(path.join(__dirname, 'attack.js'), 'utf8');
  const attackCommands = [...attack.matchAll(/command: '([^']+)'/g)]
    .map((match) => match[1]);
  assert.ok(actual.length > 0);
  assert.equal(new Set(actual).size, actual.length);
  assert.deepEqual(rootCommands, actual);
  assert.deepEqual(attackCommands, actual);
  assert.deepEqual(commands, actual);
  const nativeHost = fs.readFileSync(path.join(
    root, 'tobkiri_launcher/src-tauri/src/core_host.rs',
  ), 'utf8');
  const handler = nativeHost.match(/tauri::generate_handler!\[([\s\S]*?)\]/)[1];
  assert.deepEqual(handler.split(',').map((name) => name.trim()).filter(Boolean), actual);
  const controller = fs.readFileSync(path.join(__dirname, 'run.py'), 'utf8');
  assert.equal(Number(controller.match(/^COMMANDS = ([0-9]+)$/m)[1]), actual.length);
});
function child() {
  return {
    type: 'tobkiri-native-ipc-probe-child', version: 1,
    parent_dom_blocked: true, parent_internals_blocked: true, storage_blocked: true,
    surfaces: { sdk: false, ipc: false, webkit: true },
    attempts: commands.map((command) => ({ command, via: 'webkit', result: 'sent' })),
  };
}

async function run(change = () => {}, eventChange = () => {}, expire = false, notReady = false) {
  let listener, deadline, frame, resolveReport;
  const operations = [];
  const reported = new Promise((resolve) => { resolveReport = resolve; });
  class HTMLFormElement {}
  class HTMLInputElement {}
  class HTMLButtonElement {}
  const input = new HTMLInputElement();
  const start = new HTMLButtonElement();
  start.disabled = notReady;
  const form = new HTMLFormElement();
  form.requestSubmit = () => {
    operations.push('form-submit');
    frame = {
      src: 'tobkiri-pack://' + 'a'.repeat(64) + '/ui/index.html',
      contentWindow: {},
      getAttribute: () => 'allow-scripts',
    };
    queueMicrotask(() => {
      const data = child();
      change(data);
      const event = { origin: 'null', source: frame.contentWindow, data };
      eventChange(event);
      listener(event);
      if (expire) deadline();
    });
  };
  const document = {
    getElementById: (id) => id === 'profile-form' ? form : id === 'start' ? start : input,
    querySelector: () => frame,
    querySelectorAll: () => frame ? [frame] : [],
    set title(value) { resolveReport(JSON.parse(value.split('TOBKIRI_NATIVE_IPC_PROBE:')[1])); },
  };
  const window = {
    addEventListener: (_, fn) => { listener = fn; },
    removeEventListener: () => {},
    setTimeout: (fn, ms) => { if (ms === 20000) deadline = fn; else queueMicrotask(fn); },
    clearTimeout: () => {},
    __TAURI_INTERNALS__: {
      invoke(command) {
        assert.equal(this, window.__TAURI_INTERNALS__);
        operations.push(command);
        return Promise.resolve();
      },
    },
  };
  vm.runInNewContext(script, {
    window, document, HTMLFormElement, HTMLInputElement, HTMLButtonElement,
  });
  const report = await reported;
  return { report, operations };
}

test('ordinary start and fixed final stop with exact child coverage', async () => {
  const { report, operations } = await run();
  assert.deepEqual(operations, ['form-submit', 'core_stop_session']);
  assert.equal(report.failure, 'none');
  assert.equal(report.raw_sent, 11);
  assert.equal(report.root_stop, true);
});

test('a loaded document with unready frontend never becomes a start control', async () => {
  const { report, operations } = await run(() => {}, () => {}, false, true);
  assert.equal(report.failure, 'frontend-ready');
  assert.deepEqual(operations, []);
});

test('extra fields, duplicate coverage, and cyclic data cannot trigger native stop', async () => {
  for (const change of [
    (value) => { value.command = 'arbitrary_native_operation'; },
    (value) => { value.attempts[1] = value.attempts[0]; },
    (value) => { value.attempts[0].command = 'arbitrary_native_operation'; },
    (value) => { value.surfaces.sdk = value; },
  ]) {
    const { report, operations } = await run(change, () => {}, true);
    assert.ok(['schema', 'timeout'].includes(report.failure));
    assert.deepEqual(operations, ['form-submit']);
  }
});

test('a different frame or origin is not a completion report', async () => {
  for (const change of [
    (event) => { event.origin = 'tauri://localhost'; },
    (event) => { event.source = {}; },
  ]) {
    const { report, operations } = await run(() => {}, change, true);
    assert.equal(report.failure, 'timeout');
    assert.deepEqual(operations, ['form-submit']);
  }
});

test('missing and unsettled SDK coverage remains visible for native rejection', async () => {
  const { report } = await run((value) => {
    value.surfaces.sdk = true;
    value.attempts.push(...commands.map((command) => ({
      command, via: 'sdk', result: 'timeout',
    })));
  });
  assert.equal(report.sdk_attempts, 11);
  assert.equal(report.sdk_rejected, 0);
});
