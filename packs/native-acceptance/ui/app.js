import { connectPackUi } from './ports.js';

const shape = 'local.native-acceptance-provider.review@1.0';
const status = document.querySelector('#status');
const receipt = document.querySelector('#receipt');
const probeButton = document.querySelector('#probes');
const pingButton = document.querySelector('#ping');
const dangerButton = document.querySelector('#danger');
const results = new Map();
let client;
let busy = false;

const record = (name, outcome, detail) => {
  results.set(name, { outcome, detail: String(detail).slice(0, 256) });
  receipt.textContent = JSON.stringify({
    format: 'tobkiri.native-acceptance-observations/1',
    native_sheet_presentation: 'unobserved-by-this-fixture',
    observations: Object.fromEntries(results),
  }, null, 2);
};

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
    // Fixed empty bodies, no credentials/referrer, no user data and no query
    // parameters. The reserved .invalid domain is never an exfiltration sink.
    // IPC register has no valid namespace/session/ticket in this request.
    await blockedConnect('direct-native-ipc', 'ipc://localhost/pack_ui_register');
    await blockedConnect('direct-network', 'https://native-acceptance.invalid/probe');
    status.textContent = 'Boundary observations recorded. Inconclusive results require investigation.';
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
