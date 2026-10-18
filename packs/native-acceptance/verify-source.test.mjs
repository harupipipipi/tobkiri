/** Source/producer tests only. VM events do not establish native CSP behavior. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const fixture = dirname(fileURLToPath(import.meta.url));
const repo = resolve(fixture, '../..');
const source = readFileSync(`${fixture}/ui/app.js`, 'utf8');
const sha = (bytes) => `sha256:${createHash('sha256').update(bytes).digest('hex')}`;

async function harness(policy) {
  const handlers = new Map();
  const elements = new Map();
  const listeners = new Map();
  const calls = [];
  const requests = [];
  for (const id of ['#status', '#receipt', '#probes', '#ping', '#danger']) {
    elements.set(id, { textContent: '', disabled: false,
      addEventListener: (_, fn) => handlers.set(id, fn) });
  }
  const denied = () => { throw new DOMException('isolated', 'SecurityError'); };
  const parent = Object.defineProperties({}, {
    document: { get: denied }, __TAURI_INTERNALS__: { get: denied },
  });
  const window = Object.defineProperties({ parent }, {
    localStorage: { get: denied }, sessionStorage: { get: denied },
  });
  const document = { querySelector: (id) => elements.get(id),
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name) => listeners.delete(name) };
  const client = { async call(...args) {
    calls.push(args);
    return args[1] === 'ping' ? 'native-acceptance-provider-ready'
      : 'native-acceptance-global-opened-without-value-access';
  } };
  const context = vm.createContext({ window, document, URL, AbortController,
    setTimeout(fn, ms) { if (ms === 100) queueMicrotask(fn); return 1; },
    clearTimeout() {},
    async fetch(url, options) {
      requests.push({ url, options });
      if (policy) listeners.get('securitypolicyviolation')?.({ isTrusted: true,
        effectiveDirective: 'connect-src', disposition: 'enforce', blockedURI: url, ...policy });
      throw new TypeError('network unavailable');
    },
  });
  const ports = new vm.SyntheticModule(['connectPackUi'], function () {
    this.setExport('connectPackUi', () => Promise.resolve(client));
  }, { context });
  const app = new vm.SourceTextModule(source, { context });
  await app.link((path) => { assert.equal(path, './ports.js'); return ports; });
  await app.evaluate();
  await Promise.resolve();
  return { handlers, calls, requests,
    report: () => JSON.parse(elements.get('#receipt').textContent) };
}

test('actual UI manifest hashes and canonical SDK/WIT copies match', () => {
  const manifest = JSON.parse(readFileSync(`${fixture}/ui/pack.json`, 'utf8'));
  assert.equal(manifest.files.length, 5);
  for (const file of manifest.files) {
    const bytes = readFileSync(`${fixture}/ui/${file.path}`);
    assert.equal(bytes.length, file.size);
    assert.equal(sha(bytes), file.sha256);
  }
  for (const [copy, original] of [
    ['ui/ports.js', 'sdk/js/pack-ui/index.js'],
    ['ui/float.js', 'sdk/js/pack-ui/float.js'],
    ['provider/wit/deps/core.wit', 'crates/tobkiri-wit/wit/core/core.wit'],
  ]) assert.deepEqual(readFileSync(`${fixture}/${copy}`), readFileSync(`${repo}/${original}`));
  assert.deepEqual(manifest.permissions.network.origins, []);
  assert.equal(manifest.permissions.cross_profile_storage, false);
  assert.equal(manifest.permissions.native, false);
});

test('source handlers never treat a bare fetch failure as CSP acceptance', async () => {
  const h = await harness();
  assert.equal(h.calls.length, 0);
  await h.handlers.get('#probes')();
  for (const name of ['direct-native-ipc', 'direct-network']) {
    assert.equal(h.report().observations[name].outcome, 'inconclusive');
  }
  for (const name of ['parent-document', 'parent-ipc-object', 'localStorage', 'sessionStorage']) {
    assert.equal(h.report().observations[name].outcome, 'denied');
  }
  assert.deepEqual(h.requests.map(({ url }) => url), [
    'ipc://localhost/pack_ui_register', 'https://native-acceptance.invalid/probe',
  ]);
  for (const { options } of h.requests) {
    assert.equal(options.body, '{}');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.referrerPolicy, 'no-referrer');
    assert.equal(options.method, 'POST');
  }
  assert.equal(h.calls.length, 0);
});

test('source handlers require trusted enforced connect-src events', async () => {
  for (const [policy, outcome] of [
    [{}, 'denied-by-csp'],
    [{ isTrusted: false }, 'inconclusive'],
    [{ effectiveDirective: 'script-src' }, 'inconclusive'],
    [{ disposition: 'report' }, 'inconclusive'],
  ]) {
    const h = await harness(policy);
    await h.handlers.get('#probes')();
    for (const name of ['direct-native-ipc', 'direct-network']) {
      assert.equal(h.report().observations[name].outcome, outcome);
    }
  }
});

test('only the explicit source button requests the fixed Dangerous operation', async () => {
  const h = await harness();
  assert.equal(h.calls.length, 0);
  await h.handlers.get('#ping')();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][1], 'ping');
  await h.handlers.get('#danger')();
  assert.deepEqual(Array.from(h.calls[1].slice(0, 2)), [
    'local.native-acceptance-provider.review@1.0', 'open-global',
  ]);
  assert.equal(JSON.stringify(h.calls[1][2]), '{}');
  assert.equal(h.calls[1][3].timeoutMs, 300000);
  assert.equal(h.report().observations['dangerous-global-open'].outcome, 'provider-returned-after-approval');
  assert.equal(h.report().native_sheet_presentation, 'unobserved-by-this-fixture');
  assert.ok(Object.values(h.report().observations).every(({ detail }) => detail.length <= 256));
});

test('actual staging CLI rejects non-components before creating output', () => {
  const output = `${fixture}/__rejected_component_output__`;
  assert.equal(existsSync(output), false);
  const result = spawnSync(process.execPath, [`${fixture}/stage.mjs`,
    '--component', `${fixture}/README.md`, '--output', output], { encoding: 'utf8', cwd: repo });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /not a WebAssembly component/);
  assert.equal(existsSync(output), false);
});
