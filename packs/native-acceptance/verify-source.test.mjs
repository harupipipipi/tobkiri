/** Source/producer tests only. VM events do not establish native CSP behavior.
 *  The VM models DOM resource, timer and event outcomes; these tests prove
 *  report classification and cleanup only, never actual browser denial. */
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

const OWN_HASH = 'a'.repeat(64);
const WINDOWS_OWN_HASH = 'b'.repeat(64);
const PEER_HASH = 'c'.repeat(64);
const NATIVE_MODULE_URL = `tobkiri-pack://${OWN_HASH}/app.js`;
const WINDOWS_MODULE_URL = `http://tobkiri-pack.localhost/${WINDOWS_OWN_HASH}/app.js`;
const REMOTE_SCRIPT = 'https://example.invalid/native-acceptance-remote.js';
const REMOTE_STYLE = 'https://example.invalid/native-acceptance-remote.css';

const ownBase = (moduleUrl) => {
  for (const pattern of [/^tobkiri-pack:\/\/[0-9a-f]{64}\//,
    /^http:\/\/tobkiri-pack\.localhost\/[0-9a-f]{64}\//]) {
    const match = pattern.exec(moduleUrl);
    if (match) return match[0];
  }
  return null;
};

async function harness(options = {}) {
  const {
    connectPolicy, moduleUrl = NATIVE_MODULE_URL, stylesheetHref, stylesheetReady = true,
    computedStyle = { borderTopStyle: 'solid', borderTopWidth: '2px' },
    resourcePolicy = () => 'deny',
  } = options;
  const handlers = new Map();
  const elements = new Map();
  const listeners = new Map();
  const windowListeners = new Map();
  const calls = [];
  const requests = [];
  const created = [];
  const attempts = [];
  const liveTimers = new Set();
  const clock = { now: 0 };
  let timerSeq = 0;
  const element = (tag) => {
    const el = {
      tagName: tag.toUpperCase(), children: [], attributes: {}, parentNode: null,
      textContent: '', value: '', disabled: false, sheet: null, onload: null,
      onerror: null,
      append(...nodes) { for (const node of nodes) this.appendChild(node); },
      appendChild(node) { node.parentNode = this; this.children.push(node); },
      remove() {
        if (this.parentNode) {
          const siblings = this.parentNode.children;
          const index = siblings.indexOf(this);
          if (index >= 0) siblings.splice(index, 1);
          this.parentNode = null;
        }
      },
      setAttribute(name, value) { this.attributes[name] = String(value); },
      addEventListener() {},
      removeEventListener() {},
    };
    created.push(el);
    return el;
  };
  for (const [id, tag] of [['#status', 'p'], ['#receipt', 'pre'], ['#probes', 'button'],
    ['#ping', 'button'], ['#danger', 'button']]) {
    const el = element(tag);
    el.addEventListener = (_, fn) => handlers.set(id, fn);
    elements.set(id, el);
  }
  const api = { clock, firePagehide: () => windowListeners.get('pagehide')?.() };
  const actions = element('div');
  const base = ownBase(moduleUrl);
  const stylesheet = element('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = stylesheetHref
    ?? (base ? `${base}style.css` : 'https://pack.invalid/style.css');
  stylesheet.sheet = stylesheetReady ? { ownerNode: stylesheet } : null;
  const section = element('section');
  const head = element('head');
  head.append = (node) => {
    node.parentNode = head;
    head.children.push(node);
    const attempt = { kind: node.tagName === 'SCRIPT' ? 'script' : 'style',
      url: node.tagName === 'SCRIPT' ? node.src : node.href, node, removed: false,
      sequential: attempts.every((prior) => prior.removed) };
    attempts.push(attempt);
    const index = attempts.length - 1;
    const remove = node.remove.bind(node);
    node.remove = () => { attempt.removed = true; remove(); };
    queueMicrotask(() => {
      const behavior = resourcePolicy(attempt, index, api);
      const spec = typeof behavior === 'string' ? { result: behavior }
        : behavior && typeof behavior === 'object' ? behavior : { result: 'error' };
      const result = spec.result ?? 'error';
      const event = 'violation' in spec ? spec.violation : undefined;
      if (event !== false && (result === 'deny' || event !== undefined)) {
        listeners.get('securitypolicyviolation')?.({
          isTrusted: true, disposition: 'enforce',
          effectiveDirective: attempt.kind === 'script' ? 'script-src-elem' : 'style-src-elem',
          blockedURI: attempt.url, timeStamp: clock.now,
          ...(event && event !== true ? event : {}),
        });
      }
      if (result === 'load') node.onload?.();
      else if (result !== 'timeout' && result !== 'none') node.onerror?.();
    });
  };
  const denied = () => { throw new DOMException('isolated', 'SecurityError'); };
  const parent = Object.defineProperties({}, {
    document: { get: denied }, __TAURI_INTERNALS__: { get: denied },
  });
  const window = Object.defineProperties({
    parent,
    addEventListener: (name, fn) => windowListeners.set(name, fn),
    removeEventListener: (name, fn) => {
      if (windowListeners.get(name) === fn) windowListeners.delete(name);
    },
  }, {
    localStorage: { get: denied }, sessionStorage: { get: denied },
  });
  const document = {
    head,
    querySelector: (selector) => elements.get(selector)
      ?? { '.actions': actions, 'link[rel="stylesheet"]': stylesheet, section }[selector]
      ?? null,
    createElement: (tag) => element(tag),
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => {
      if (listeners.get(name) === fn) listeners.delete(name);
    },
  };
  const client = { async call(...args) {
    calls.push(args);
    return args[1] === 'ping' ? 'native-acceptance-provider-ready'
      : 'native-acceptance-global-opened-without-value-access';
  } };
  const context = vm.createContext({ window, document, URL, AbortController,
    performance: { now: () => clock.now },
    getComputedStyle: () => computedStyle,
    setTimeout(fn) {
      const id = ++timerSeq;
      liveTimers.add(id);
      queueMicrotask(() => { if (liveTimers.delete(id)) fn(); });
      return id;
    },
    clearTimeout(id) { liveTimers.delete(id); },
    async fetch(url, options) {
      requests.push({ url, options });
      if (connectPolicy) listeners.get('securitypolicyviolation')?.({
        isTrusted: true, effectiveDirective: 'connect-src', disposition: 'enforce',
        blockedURI: url, timeStamp: clock.now, ...connectPolicy });
      throw new TypeError('network unavailable');
    },
  });
  const ports = new vm.SyntheticModule(['connectPackUi'], function () {
    this.setExport('connectPackUi', () => Promise.resolve(client));
  }, { context });
  const app = new vm.SourceTextModule(source, { context,
    initializeImportMeta(meta) { meta.url = moduleUrl; } });
  await app.link((path) => { assert.equal(path, './ports.js'); return ports; });
  await app.evaluate();
  await Promise.resolve();
  const peerInput = created.find((el) => el.id === 'peer-pack-hash');
  return { handlers, calls, requests, attempts, listeners, liveTimers, clock,
    elements, head, actions, created, peerInput, firePagehide: api.firePagehide,
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
  assert.deepEqual(h.attempts.map(({ url }) => url), [REMOTE_SCRIPT, REMOTE_STYLE]);
  assert.equal(h.report().observations['same-pack-module'].outcome, 'executed');
  assert.equal(h.report().observations['same-pack-style'].outcome, 'applied');
  assert.equal(h.report().observations['cross-pack-script'].outcome, 'inconclusive');
  assert.equal(h.report().observations['remote-script'].outcome, 'denied-by-csp');
  assert.equal(h.report().peer_active_served_verified, false);
  assert.equal(h.calls.length, 0);
});

test('source handlers require trusted enforced connect-src events', async () => {
  for (const [policy, outcome] of [
    [{}, 'denied-by-csp'],
    [{ isTrusted: false }, 'inconclusive'],
    [{ effectiveDirective: 'script-src' }, 'inconclusive'],
    [{ disposition: 'report' }, 'inconclusive'],
  ]) {
    const h = await harness({ connectPolicy: policy });
    await h.handlers.get('#probes')();
    for (const name of ['direct-native-ipc', 'direct-network']) {
      assert.equal(h.report().observations[name].outcome, outcome);
    }
  }
});

test('VM models canonical native and Windows Pack namespaces', async () => {
  for (const moduleUrl of [NATIVE_MODULE_URL, WINDOWS_MODULE_URL]) {
    const h = await harness({ moduleUrl });
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    const observations = h.report().observations;
    assert.equal(observations['same-pack-module'].outcome, 'executed');
    assert.equal(observations['same-pack-style'].outcome, 'applied');
    const peerBase = moduleUrl.startsWith('http:')
      ? `http://tobkiri-pack.localhost/${PEER_HASH}/` : `tobkiri-pack://${PEER_HASH}/`;
    assert.deepEqual(h.attempts.map(({ kind }) => kind), ['script', 'style', 'script', 'style']);
    assert.deepEqual(h.attempts.map(({ url }) => url),
      [`${peerBase}float.js`, `${peerBase}style.css`, REMOTE_SCRIPT, REMOTE_STYLE]);
    for (const name of ['cross-pack-script', 'cross-pack-style', 'remote-script', 'remote-style']) {
      assert.equal(observations[name].outcome, 'denied-by-csp', `${moduleUrl} ${name}`);
    }
    assert.equal(h.report().peer_active_served_verified, false);
    assert.equal(h.calls.length, 0);
  }
});

test('the bounded peer field is created in the actions area', async () => {
  const h = await harness();
  const label = h.created.find((el) => el.tagName === 'LABEL');
  assert.equal(label.textContent, 'Peer Pack hash');
  assert.equal(label.htmlFor, 'peer-pack-hash');
  assert.equal(h.peerInput.id, 'peer-pack-hash');
  assert.equal(h.peerInput.maxLength, 64);
  assert.equal(h.peerInput.parentNode, h.actions);
  assert.match(h.created.find((el) => el.tagName === 'SPAN').textContent, /cannot attest/);
});

test('invalid, missing, self or prefixed peer values create no cross-Pack nodes', async () => {
  for (const [moduleUrl, self] of [
    [NATIVE_MODULE_URL, OWN_HASH], [WINDOWS_MODULE_URL, WINDOWS_OWN_HASH]]) {
    for (const value of ['', self, 'x'.repeat(64), PEER_HASH.toUpperCase(),
      `sha256:${PEER_HASH}`, ` ${PEER_HASH}`, `${PEER_HASH}\n`, `${PEER_HASH}?p`,
      `tobkiri-pack://${PEER_HASH}/`, PEER_HASH.slice(0, 63), `${PEER_HASH}0`]) {
      const h = await harness({ moduleUrl });
      h.peerInput.value = value;
      await h.handlers.get('#probes')();
      const observations = h.report().observations;
      for (const name of ['cross-pack-script', 'cross-pack-style']) {
        assert.equal(observations[name].outcome, 'inconclusive', `${moduleUrl} ${value}`);
      }
      assert.deepEqual(h.attempts.map(({ url }) => url),
        [REMOTE_SCRIPT, REMOTE_STYLE], `${moduleUrl} ${value}`);
      for (const name of ['remote-script', 'remote-style']) {
        assert.equal(observations[name].outcome, 'denied-by-csp');
      }
    }
  }
});

test('missing module or stylesheet positives skip all resource probes', async () => {
  for (const options of [
    { moduleUrl: 'file:///ui/app.js' },
    { moduleUrl: 'https://tobkiri-pack.localhost/app.js' },
    { moduleUrl: `tobkiri-pack://${OWN_HASH.toUpperCase()}/app.js` },
    { moduleUrl: `tobkiri-pack://${OWN_HASH}:4443/app.js` },
    { moduleUrl: `tobkiri-pack://${OWN_HASH}/elsewhere/app.js` },
    { moduleUrl: `tobkiri-pack://${OWN_HASH}/app.js?probe=1` },
    { stylesheetReady: false },
    { stylesheetHref: 'https://pack.invalid/style.css' },
    { computedStyle: { borderTopStyle: 'none', borderTopWidth: '0px' } },
  ]) {
    const h = await harness(options);
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    const observations = h.report().observations;
    for (const name of ['cross-pack-script', 'cross-pack-style', 'remote-script', 'remote-style']) {
      assert.equal(observations[name].outcome, 'skipped', JSON.stringify(options));
    }
    assert.equal(h.attempts.length, 0, JSON.stringify(options));
  }
});

test('module execution is recorded separately from stylesheet readiness', async () => {
  const h = await harness({ stylesheetReady: false });
  h.peerInput.value = PEER_HASH;
  await h.handlers.get('#probes')();
  const observations = h.report().observations;
  assert.equal(observations['same-pack-module'].outcome, 'executed');
  assert.equal(observations['same-pack-style'].outcome, 'inconclusive');
  assert.equal(observations['remote-script'].outcome, 'skipped');
});

test('only trusted enforced bound violations classify as denied-by-csp', async () => {
  for (const [violation, outcome] of [
    [{}, 'denied-by-csp'],
    [{ effectiveDirective: 'script-src' }, 'denied-by-csp'],
    [{ blockedURI: `tobkiri-pack://${PEER_HASH}` }, 'denied-by-csp'],
    [{ blockedURI: `tobkiri-pack://${PEER_HASH}/` }, 'denied-by-csp'],
    [{ isTrusted: false }, 'inconclusive'],
    [{ disposition: 'report' }, 'inconclusive'],
    [{ effectiveDirective: 'connect-src' }, 'inconclusive'],
    [{ effectiveDirective: 'style-src-elem' }, 'inconclusive'],
    [{ timeStamp: -1 }, 'inconclusive'],
    [{ timeStamp: 'not-a-number' }, 'inconclusive'],
    [{ timeStamp: 99999 }, 'inconclusive'],
    [{ blockedURI: `tobkiri-pack://${'d'.repeat(64)}/float.js` }, 'inconclusive'],
    [{ blockedURI: `tobkiri-pack://${OWN_HASH}/float.js` }, 'inconclusive'],
    [{ blockedURI: `tobkiri-pack://${PEER_HASH}/evil.js` }, 'inconclusive'],
    [{ blockedURI: `tobkiri-pack://${PEER_HASH}/float.js?x=1` }, 'inconclusive'],
    [{ blockedURI: `tobkiri-pack://${PEER_HASH}:4443/float.js` }, 'inconclusive'],
    [{ blockedURI: `tobkiri-pack://${PEER_HASH}.evil/float.js` }, 'inconclusive'],
    [{ blockedURI: 'tobkiri-pack:' }, 'inconclusive'],
    [{ blockedURI: 'tobkiri-pack://' }, 'inconclusive'],
    [{ blockedURI: `https://${PEER_HASH}/float.js` }, 'inconclusive'],
    [{ blockedURI: 'x'.repeat(5000) }, 'inconclusive'],
  ]) {
    const h = await harness({
      resourcePolicy: (attempt, index) => index === 0
        ? { result: 'error', violation } : 'deny' });
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    assert.equal(h.report().observations['cross-pack-script'].outcome, outcome,
      JSON.stringify(violation));
    assert.equal(h.report().observations['cross-pack-style'].outcome, 'denied-by-csp');
  }
});

test('violation directive families must match the resource kind', async () => {
  for (const [index, directive, outcome] of [
    [0, 'script-src', 'denied-by-csp'],
    [0, 'style-src', 'inconclusive'],
    [1, 'style-src', 'denied-by-csp'],
    [1, 'style-src-elem', 'denied-by-csp'],
    [1, 'script-src-elem', 'inconclusive'],
  ]) {
    const h = await harness({
      resourcePolicy: (attempt, i) => i === index
        ? { result: 'error', violation: { effectiveDirective: directive } } : 'deny' });
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    const name = index === 0 ? 'cross-pack-script' : 'cross-pack-style';
    assert.equal(h.report().observations[name].outcome, outcome, directive);
  }
});

test('stale or replayed events cannot bind to a later attempt', async () => {
  const h = await harness({
    resourcePolicy: (attempt, index, api) => {
      if (index === 0) { api.clock.now = 500; return 'deny'; }
      if (index === 1) return { result: 'error', violation: { timeStamp: 0 } };
      return 'deny';
    },
  });
  h.peerInput.value = PEER_HASH;
  await h.handlers.get('#probes')();
  const observations = h.report().observations;
  assert.equal(observations['cross-pack-script'].outcome, 'denied-by-csp');
  assert.equal(observations['cross-pack-style'].outcome, 'inconclusive');
});

test('origin-only or hashless redactions cannot bind a peer namespace', async () => {
  for (const [moduleUrl, redacted] of [
    [WINDOWS_MODULE_URL, ['http://tobkiri-pack.localhost',
      'http://tobkiri-pack.localhost/',
      `http://tobkiri-pack.localhost/${WINDOWS_OWN_HASH}/`]],
    [NATIVE_MODULE_URL, ['tobkiri-pack:', 'tobkiri-pack://',
      `tobkiri-pack://${OWN_HASH}/`]],
  ]) {
    for (const blockedURI of redacted) {
      const h = await harness({ moduleUrl,
        resourcePolicy: (attempt, index) => index === 0
          ? { result: 'error', violation: { blockedURI } } : 'deny' });
      h.peerInput.value = PEER_HASH;
      await h.handlers.get('#probes')();
      assert.equal(h.report().observations['cross-pack-script'].outcome,
        'inconclusive', blockedURI);
    }
  }
  // Peer namespace forms that retain the validated hash still bind.
  for (const [moduleUrl, bound] of [
    [WINDOWS_MODULE_URL, [`http://tobkiri-pack.localhost/${PEER_HASH}`,
      `http://tobkiri-pack.localhost/${PEER_HASH}/`,
      `http://tobkiri-pack.localhost/${PEER_HASH}/float.js`]],
    [NATIVE_MODULE_URL, [`tobkiri-pack://${PEER_HASH}`,
      `tobkiri-pack://${PEER_HASH}/`]],
  ]) {
    for (const blockedURI of bound) {
      const h = await harness({ moduleUrl,
        resourcePolicy: (attempt, index) => index === 0
          ? { result: 'error', violation: { blockedURI } } : 'deny' });
      h.peerInput.value = PEER_HASH;
      await h.handlers.get('#probes')();
      assert.equal(h.report().observations['cross-pack-script'].outcome,
        'denied-by-csp', blockedURI);
    }
  }
});

test('remote redaction binds only the exact reserved origin', async () => {
  for (const [blockedURI, outcome] of [
    [REMOTE_SCRIPT, 'denied-by-csp'],
    ['https://example.invalid', 'denied-by-csp'],
    ['https://example.invalid/', 'denied-by-csp'],
    ['https://example.invalid.evil', 'inconclusive'],
    ['https://example.invalid.evil/probe.js', 'inconclusive'],
    ['https://example.invalid:8443/native-acceptance-remote.js', 'inconclusive'],
    ['http://example.invalid', 'inconclusive'],
    ['https:', 'inconclusive'],
    ['https://example.invalid/native-acceptance-remote.js?x=1', 'inconclusive'],
  ]) {
    const h = await harness({
      resourcePolicy: (attempt, index) => index === 2
        ? { result: 'error', violation: { blockedURI } } : 'deny' });
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    assert.equal(h.report().observations['remote-script'].outcome, outcome,
      blockedURI);
  }
});

test('bare resource errors and timeouts without an event are inconclusive', async () => {
  for (const result of ['error', 'timeout']) {
    const h = await harness({ resourcePolicy: () => result });
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    for (const name of ['cross-pack-script', 'cross-pack-style', 'remote-script', 'remote-style']) {
      assert.equal(h.report().observations[name].outcome, 'inconclusive', result);
    }
  }
});

test('a successful resource load is unexpected even beside violations', async () => {
  for (const spec of ['load', { result: 'load',
    violation: { effectiveDirective: 'connect-src', blockedURI: 'https://unrelated.invalid/' } }]) {
    const h = await harness({ resourcePolicy: () => spec });
    h.peerInput.value = PEER_HASH;
    await h.handlers.get('#probes')();
    for (const name of ['cross-pack-script', 'cross-pack-style', 'remote-script', 'remote-style']) {
      assert.equal(h.report().observations[name].outcome, 'unexpected-load',
        JSON.stringify(spec));
    }
  }
});

test('resource probes run sequentially after positives and clean up fully', async () => {
  const h = await harness();
  h.peerInput.value = PEER_HASH;
  await h.handlers.get('#probes')();
  const keys = Object.keys(h.report().observations);
  assert.ok(keys.indexOf('same-pack-module') < keys.indexOf('cross-pack-script'));
  assert.ok(keys.indexOf('same-pack-style') < keys.indexOf('remote-script'));
  assert.equal(h.attempts.length, 4);
  for (const attempt of h.attempts) {
    assert.equal(attempt.removed, true);
    assert.equal(attempt.sequential, true);
    assert.equal(attempt.node.crossOrigin, 'anonymous');
    assert.equal(attempt.node.referrerPolicy, 'no-referrer');
    assert.equal(attempt.node.onload, null);
    assert.equal(attempt.node.onerror, null);
    if (attempt.kind === 'script') assert.equal(attempt.node.type, 'module');
    else assert.equal(attempt.node.rel, 'stylesheet');
  }
  assert.equal(h.head.children.length, 0);
  assert.equal(h.listeners.size, 0);
  assert.equal(h.liveTimers.size, 0);
  assert.equal(h.calls.length, 0);
  assert.equal(h.report().peer_active_served_verified, false);
  assert.equal(h.report().native_sheet_presentation, 'unobserved-by-this-fixture');
  assert.ok(Object.values(h.report().observations).every(({ detail }) => detail.length <= 256));
  // A repeated run cannot overlap attempts or leak listeners, nodes or timers.
  await h.handlers.get('#probes')();
  assert.equal(h.attempts.length, 8);
  assert.ok(h.attempts.every((attempt) => attempt.removed && attempt.sequential));
  assert.equal(h.head.children.length, 0);
  assert.equal(h.listeners.size, 0);
  assert.equal(h.liveTimers.size, 0);
});

test('pagehide cancels the active probe and leaves no delayed mutation', async () => {
  const h = await harness({
    resourcePolicy: (attempt, index, api) =>
      index === 0 ? (api.firePagehide(), 'timeout') : 'deny' });
  h.peerInput.value = PEER_HASH;
  await h.handlers.get('#probes')();
  const observations = h.report().observations;
  assert.equal(observations['same-pack-module'].outcome, 'executed');
  assert.equal(observations['cross-pack-script'], undefined);
  assert.equal(observations['remote-script'], undefined);
  assert.equal(h.attempts.length, 1);
  assert.equal(h.attempts[0].removed, true);
  assert.equal(h.listeners.size, 0);
  assert.equal(h.liveTimers.size, 0);
  assert.equal(h.head.children.length, 0);
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
  assert.equal(h.report().peer_active_served_verified, false);
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

// Pack-specific documents are checked alongside the Pack fixtures, not in the
// generic host mechanism. This is source evidence, not native CSP acceptance.
test('official Pack documents defer CSP to the bound native response header', () => {
  for (const doc of [
    'packs/local.harness-ui/ui/index.html',
    'packs/native-acceptance/ui/index.html',
  ]) {
    const html = readFileSync(resolve(repo, doc), 'utf8');
    assert.ok(!html.includes('Content-Security-Policy'), doc);
  }
});
