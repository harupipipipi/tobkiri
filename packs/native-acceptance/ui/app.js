import { connectPackUi } from './ports.js';

const shape = 'local.native-acceptance-provider.review@1.0';
const status = document.querySelector('#status');
const receipt = document.querySelector('#receipt');
const probeButton = document.querySelector('#probes');
const pingButton = document.querySelector('#ping');
const dangerButton = document.querySelector('#danger');
const actions = document.querySelector('.actions');
const results = new Map();
let client;
let busy = false;
let pageGone = false;
let cancelActiveProbe;

const record = (name, outcome, detail) => {
  results.set(name, { outcome, detail: String(detail).slice(0, 256) });
  receipt.textContent = JSON.stringify({
    format: 'tobkiri.native-acceptance-observations/1',
    native_sheet_presentation: 'unobserved-by-this-fixture',
    peer_active_served_verified: false,
    observations: Object.fromEntries(results),
  }, null, 2);
};

// The operator supplies a separately verified peer Pack hash. This fixture
// cannot attest that the peer is active or served, and never treats a denied
// cross-namespace request as peer access or serving evidence.
const peerLabel = document.createElement('label');
peerLabel.textContent = 'Peer Pack hash';
peerLabel.htmlFor = 'peer-pack-hash';
const peerInput = document.createElement('input');
peerInput.id = 'peer-pack-hash';
peerInput.type = 'text';
peerInput.maxLength = 64;
peerInput.autocomplete = 'off';
peerInput.spellcheck = false;
const peerNote = document.createElement('span');
peerNote.textContent = 'Operator-supplied hash of a separately verified peer '
  + 'Pack; this fixture cannot attest that it is active or served.';
actions?.append(peerLabel, peerInput, peerNote);

const HASH64 = /^[0-9a-f]{64}$/;
const NATIVE_MODULE = /^tobkiri-pack:\/\/([0-9a-f]{64})\/app\.js$/;
const WINDOWS_MODULE = /^http:\/\/tobkiri-pack\.localhost\/([0-9a-f]{64})\/app\.js$/;

// The verified Pack namespace is derived only from this module's own URL;
// location.origin is opaque here. Credentials, ports, queries, fragments,
// noncanonical paths and every other scheme or host are rejected.
const packNamespace = () => {
  try {
    const raw = import.meta.url;
    const url = new URL(raw);
    const native = NATIVE_MODULE.exec(raw);
    if (native && url.protocol === 'tobkiri-pack:' && url.host === native[1]
      && url.pathname === '/app.js' && !url.port && !url.username && !url.password
      && !url.search && !url.hash) {
      return { platform: 'native', hash: native[1], base: `tobkiri-pack://${native[1]}/` };
    }
    const windows = WINDOWS_MODULE.exec(raw);
    if (windows && url.protocol === 'http:' && url.host === 'tobkiri-pack.localhost'
      && url.pathname === `/${windows[1]}/app.js` && !url.port && !url.username
      && !url.password && !url.search && !url.hash) {
      return { platform: 'windows', hash: windows[1], base: `http://tobkiri-pack.localhost/${windows[1]}/` };
    }
    return null;
  } catch {
    return null;
  }
};
const namespace = packNamespace();

const securityErrorProbe = async (name, action) => {
  try {
    await action();
    record(name, 'unexpected-access', 'The browser allowed this boundary probe.');
  } catch (error) {
    record(name, error?.name === 'SecurityError' ? 'denied' : 'inconclusive', error?.name ?? 'UnknownError');
  }
};

const blockedConnect = async (name, url) => {
  let violation;
  const expected = new URL(url);
  const onViolation = (event) => {
    const blocked = event.blockedURI ?? '';
    if (event.isTrusted === true && event.effectiveDirective === 'connect-src' && event.disposition === 'enforce'
      && (blocked === expected.protocol.slice(0, -1) || blocked === expected.protocol
        || blocked.startsWith(`${expected.protocol}//${expected.host}`))) {
      violation = { directive: event.effectiveDirective, blocked };
    }
  };
  document.addEventListener('securitypolicyviolation', onViolation);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1500);
  let resolved = false;
  let errorName = 'none';
  try {
    await fetch(url, {
      method: 'POST', mode: 'no-cors', credentials: 'omit', referrerPolicy: 'no-referrer',
      body: '{}', signal: controller.signal,
    });
    resolved = true;
  } catch (error) {
    errorName = error?.name ?? 'UnknownError';
  } finally {
    clearTimeout(timer);
    // CSP events are queued separately from fetch's rejection. A transport or
    // DNS error alone is not evidence that native CSP blocked the operation.
    await new Promise((resolve) => setTimeout(resolve, 100));
    document.removeEventListener('securitypolicyviolation', onViolation);
  }
  record(name, violation ? 'denied-by-csp' : resolved ? 'unexpected-response' : 'inconclusive',
    violation ? `${violation.directive}: ${violation.blocked}` : errorName);
};

const SCRIPT_DIRECTIVES = new Set(['script-src', 'script-src-elem']);
const STYLE_DIRECTIVES = new Set(['style-src', 'style-src-elem']);
const PROBE_TIMEOUT_MS = 1500;
const EVENT_GRACE_MS = 100;
const MAX_BLOCKED_URI = 4096;
const REMOTE_SCRIPT = 'https://example.invalid/native-acceptance-remote.js';
const REMOTE_STYLE = 'https://example.invalid/native-acceptance-remote.css';

// The same-Pack stylesheet must actually be applied before negative probes
// run; a missing module or CSS positive yields only skipped negatives.
const stylesheetPositive = async () => {
  try {
    if (!namespace) {
      return { ok: false, detail: 'No canonical Pack namespace was validated for this module.' };
    }
    const expected = `${namespace.base}style.css`;
    const link = document.querySelector('link[rel="stylesheet"]');
    if (!link || link.href !== expected) {
      return { ok: false, detail: `The declared stylesheet ${expected} is not linked.` };
    }
    for (let waited = 0; link.sheet == null && waited < 1500; waited += 50) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (link.sheet == null) {
      return { ok: false, detail: 'The same-Pack stylesheet did not become ready within 1500 ms.' };
    }
    const section = document.querySelector('section');
    const style = section ? getComputedStyle(section) : null;
    return style?.borderTopStyle === 'solid' && style?.borderTopWidth === '2px'
      ? { ok: true, detail: `${expected} applied its declared 2px solid section border.` }
      : { ok: false, detail: 'The declared section border was not observed in computed style.' };
  } catch (error) {
    return { ok: false, detail: `Stylesheet observation threw ${error?.name ?? 'UnknownError'}.` };
  }
};

// A trusted enforced violation only counts when it is bound to this attempt:
// the full target URI or an exact canonical namespace form that still carries
// the validated peer hash. Scheme-only, host-only and unrelated values are
// not evidence; the Windows origin drops the hash and cannot bind a peer.
const namespaceBind = (peerBase, target) => (blocked) =>
  blocked === target || blocked === peerBase || blocked === peerBase.slice(0, -1);
const remoteBind = (target) => (blocked) => blocked === target
  || blocked === 'https://example.invalid' || blocked === 'https://example.invalid/';

const resourceProbe = async (name, kind, target, bind, claim) => {
  const directives = kind === 'script' ? SCRIPT_DIRECTIVES : STYLE_DIRECTIVES;
  const startStamp = performance.now();
  const live = { active: true };
  let violation;
  const onViolation = (event) => {
    if (!live.active || event.isTrusted !== true || event.disposition !== 'enforce'
      || !directives.has(event.effectiveDirective)) return;
    const stamp = typeof event.timeStamp === 'number' ? event.timeStamp : Number.NaN;
    if (!(stamp >= startStamp && stamp <= performance.now() + EVENT_GRACE_MS)) return;
    const blocked = String(event.blockedURI ?? '');
    if (blocked.length <= MAX_BLOCKED_URI && bind(blocked)) {
      violation = { directive: event.effectiveDirective, blocked };
    }
  };
  document.addEventListener('securitypolicyviolation', onViolation);
  let timer;
  let node;
  let result;
  try {
    node = document.createElement(kind === 'script' ? 'script' : 'link');
    result = await new Promise((resolve) => {
      let settled = false;
      const done = (value) => { if (!settled) { settled = true; resolve(value); } };
      node.onload = () => done('load');
      node.onerror = () => done('error');
      if (kind === 'script') {
        node.type = 'module';
        node.src = target;
      } else {
        node.rel = 'stylesheet';
        node.href = target;
      }
      node.crossOrigin = 'anonymous';
      node.referrerPolicy = 'no-referrer';
      cancelActiveProbe = () => done('cancelled');
      document.head.append(node);
      timer = setTimeout(() => done('timeout'), PROBE_TIMEOUT_MS);
    });
    // CSP violation events may be queued separately from the resource's own
    // error signal; a small bounded grace precedes classification.
    await new Promise((resolve) => setTimeout(resolve, EVENT_GRACE_MS));
  } catch {
    result = 'thrown';
  } finally {
    live.active = false;
    cancelActiveProbe = undefined;
    if (timer !== undefined) clearTimeout(timer);
    if (node) {
      node.onload = null;
      node.onerror = null;
      node.remove();
    }
    document.removeEventListener('securitypolicyviolation', onViolation);
  }
  if (result === 'cancelled') return;
  const outcome = result === 'load' ? 'unexpected-load'
    : violation ? 'denied-by-csp' : 'inconclusive';
  record(name, outcome, outcome === 'denied-by-csp'
    ? `${violation.directive}: ${violation.blocked}; ${claim}`
    : outcome === 'unexpected-load' ? `The browser loaded ${target}.`
    : `Settled as ${result} without a bound enforced violation event.`);
};

window.addEventListener('pagehide', () => {
  pageGone = true;
  cancelActiveProbe?.();
});

probeButton.addEventListener('click', async () => {
  if (busy) return;
  busy = true;
  probeButton.disabled = true;
  try {
    record('direct-ipc-globals',
      typeof window.__TAURI__ === 'undefined' && typeof window.__TAURI_INTERNALS__ === 'undefined'
        ? 'absent' : 'unexpected-exposure',
      'Only the presence of global IPC objects is inspected; no authority or payload is read.');
    await securityErrorProbe('parent-document', () => { void window.parent.document.documentElement; });
    await securityErrorProbe('parent-ipc-object', () => { void window.parent.__TAURI_INTERNALS__; });
    for (const name of ['localStorage', 'sessionStorage']) {
      await securityErrorProbe(name, () => {
        const storage = window[name];
        storage.setItem('tobkiri-native-acceptance-probe', 'fixture');
        storage.removeItem('tobkiri-native-acceptance-probe');
      });
    }
    // Same-Pack positives: this module and its static dependency executing in
    // the verified namespace, and the declared stylesheet applied, gate every
    // negative resource attempt below.
    record('same-pack-module', namespace ? 'executed' : 'inconclusive',
      namespace
        ? `This module and its static ./ports.js dependency evaluated under ${namespace.base}`
        : 'import.meta.url is not a supported canonical verified Pack module URL.');
    const stylesheet = await stylesheetPositive();
    record('same-pack-style', stylesheet.ok ? 'applied' : 'inconclusive', stylesheet.detail);
    if (!stylesheet.ok) {
      for (const name of ['cross-pack-script', 'cross-pack-style', 'remote-script', 'remote-style']) {
        record(name, 'skipped',
          'A same-Pack module or stylesheet positive is missing; denial is not claimed.');
      }
    } else {
      // Snapshot the operator-supplied peer hash for this run only. An
      // invalid, missing or self hash leaves both cross-Pack probes
      // inconclusive and creates no request nodes.
      const peerValue = String(peerInput?.value ?? '');
      const peer = HASH64.test(peerValue) && peerValue !== namespace.hash ? peerValue : null;
      if (peer) {
        const peerBase = namespace.platform === 'windows'
          ? `http://tobkiri-pack.localhost/${peer}/` : `tobkiri-pack://${peer}/`;
        const peerScript = `${peerBase}float.js`;
        await resourceProbe('cross-pack-script', 'script', peerScript,
          namespaceBind(peerBase, peerScript),
          'CSP blocked a different Pack namespace before fetch; peer activity or serving is not attested.');
        if (!pageGone) {
          const peerStyle = `${peerBase}style.css`;
          await resourceProbe('cross-pack-style', 'style', peerStyle,
            namespaceBind(peerBase, peerStyle),
            'CSP blocked a different Pack namespace before fetch; peer activity or serving is not attested.');
        }
      } else {
        for (const name of ['cross-pack-script', 'cross-pack-style']) {
          record(name, 'inconclusive',
            'The peer field is missing, invalid or the running Pack itself; no request node was created.');
        }
      }
      if (!pageGone) {
        await resourceProbe('remote-script', 'script', REMOTE_SCRIPT, remoteBind(REMOTE_SCRIPT),
          'CSP blocked the fixed remote origin before fetch.');
      }
      if (!pageGone) {
        await resourceProbe('remote-style', 'style', REMOTE_STYLE, remoteBind(REMOTE_STYLE),
          'CSP blocked the fixed remote origin before fetch.');
      }
    }
    if (!pageGone) {
      // Fixed empty bodies, no credentials/referrer, no user data and no query
      // parameters. The reserved .invalid domain is never an exfiltration sink.
      // IPC register has no valid namespace/session/ticket in this request.
      await blockedConnect('direct-native-ipc', 'ipc://localhost/pack_ui_register');
      await blockedConnect('direct-network', 'https://native-acceptance.invalid/probe');
      status.textContent = 'Boundary observations recorded. Inconclusive results require investigation.';
    }
  } finally {
    busy = false;
    probeButton.disabled = false;
  }
});

pingButton.addEventListener('click', async () => {
  if (!client || busy) return;
  busy = true;
  try {
    const value = await client.call(shape, 'ping', {});
    record('declared-provider-port', value === 'native-acceptance-provider-ready' ? 'succeeded' : 'unexpected-value',
      value === 'native-acceptance-provider-ready' ? value : 'The provider returned an unexpected value.');
  } catch (error) {
    record('declared-provider-port', 'failed', error.message);
  } finally { busy = false; }
});

dangerButton.addEventListener('click', async () => {
  if (!client || busy) return;
  busy = true;
  dangerButton.disabled = true;
  status.textContent = 'Waiting for the real Core request and native review.';
  try {
    const value = await client.call(shape, 'open-global', {}, { timeoutMs: 300000 });
    record('dangerous-global-open', value === 'native-acceptance-global-opened-without-value-access'
      ? 'provider-returned-after-approval' : 'unexpected-value',
    'This result does not certify the native sheet appearance or button behavior.');
    status.textContent = 'Provider returned. Record the native sheet observation separately.';
  } catch (error) {
    record('dangerous-global-open', 'denied-or-failed', error.message);
    status.textContent = 'Core refused or could not complete the request. Inspect native evidence separately.';
  } finally {
    busy = false;
    dangerButton.disabled = false;
  }
});

connectPackUi().then((connection) => {
  client = connection;
  pingButton.disabled = false;
  dangerButton.disabled = false;
  status.textContent = 'Connected through the one-use Pack channel.';
  record('pack-channel', 'connected', 'No Core token, namespace, receiver or host ticket was given to this fixture.');
});
