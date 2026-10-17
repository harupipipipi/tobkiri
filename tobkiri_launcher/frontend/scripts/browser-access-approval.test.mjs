import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../public/browser-access-approval.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const context = { origin: 'https://example.test', profile_id: 'local', target: 'chat', expires_in: 5 };
function harness({ synchronousFailure = false } = {}) {
  let now = 0;
  const elements = Object.fromEntries(['status', 'allow', 'deny', 'origin', 'profile', 'target'].map(id => [id, { disabled: true, textContent: '' }]));
  const calls = [];
  const events = {};
  let interval;
  const sandbox = {
    URL, location: { href: 'http://localhost/browser-access-approval.html?request_id=req&nonce=nonce' },
    performance: { now: () => now }, queueMicrotask, document: { getElementById: id => elements[id] },
    setInterval: callback => { interval = callback; },
    window: { addEventListener: (name, callback) => { events[name] = callback; }, __TAURI_INTERNALS__: {
      invoke: (name, args) => {
        if (synchronousFailure) throw new Error('native unavailable');
        return new Promise((resolve, reject) => calls.push({ name, args, resolve, reject }));
      }
    } }
  };
  vm.runInNewContext(script, sandbox);
  return { elements, calls, focus: () => events.focus(), advance: ms => { now += ms; interval(); } };
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

test('initial focus rejection recovers on refocus using same request and nonce', async () => {
  const h = harness(); h.calls[0].reject(new Error('not focused')); await flush();
  assert.equal(h.elements.allow.disabled, true);
  h.focus(); h.calls[1].resolve(context); await flush();
  assert.equal(h.elements.allow.disabled, false);
  assert.equal(h.elements.origin.textContent, context.origin);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[1].args)), { requestId: 'req', nonce: 'nonce' });
});
test('focus bursts do not duplicate in-flight reads and retries are bounded', async () => {
  const h = harness(); for (let i = 0; i < 20; i++) h.focus();
  assert.equal(h.calls.length, 1);
  for (let i = 0; i < 6; i++) { h.calls[i].reject(new Error('focus')); await flush(); h.focus(); }
  assert.equal(h.calls.length, 6);
});
test('context countdown expires without focus refreshing its deadline', async () => {
  const h = harness(); h.calls[0].resolve(context); await flush();
  h.advance(2000); h.focus(); assert.equal(h.calls.length, 1);
  assert.match(h.elements.status.textContent, /3 秒/);
  h.advance(3000); h.focus(); assert.equal(h.calls.length, 1);
  assert.equal(h.elements.allow.disabled, true);
  assert.match(h.elements.status.textContent, /有効期限が切れ/);
});
test('pending and rejected decisions cannot reenable buttons or retry context', async () => {
  const h = harness(); h.calls[0].resolve(context); await flush();
  h.elements.allow.onclick(); h.focus(); h.elements.deny.onclick();
  assert.equal(h.calls.length, 2); assert.equal(h.calls[1].name, 'browser_access_decide');
  assert.equal(h.elements.allow.disabled, true);
  h.calls[1].reject(new Error('lost focus')); await flush(); h.focus();
  assert.equal(h.calls.length, 2); assert.equal(h.elements.deny.disabled, true);
});
test('late context response after page deadline is discarded', async () => {
  const h = harness(); h.advance(300000); h.calls[0].resolve(context); await flush();
  assert.equal(h.elements.allow.disabled, true); assert.equal(h.elements.origin.textContent, '');
  h.focus(); assert.equal(h.calls.length, 1);
});
test('delayed context completion does not start a fresh TTL', async () => {
  const h = harness(); h.advance(6000); h.calls[0].resolve(context); await flush();
  assert.equal(h.elements.allow.disabled, true); assert.match(h.elements.status.textContent, /有効期限が切れ/);
});
test('splash and public approval documents remain identical', () => {
  assert.equal(readFileSync(new URL('../../src-tauri/splash/browser-access-approval.html', import.meta.url), 'utf8'), html);
});

test('invalid native TTL cannot enable decisions', async () => {
  for (const expires_in of [NaN, Infinity, 0, -1, 0.5, 301, true, '5', null, undefined]) {
    const h = harness(); h.calls[0].resolve({ ...context, expires_in }); await flush();
    assert.equal(h.elements.allow.disabled, true); h.focus(); assert.equal(h.calls.length, 1);
  }
});
test('synchronous native invocation failures are caught on initial load and focus', async () => {
  const h = harness({ synchronousFailure: true }); await flush(); h.focus(); await flush();
  assert.equal(h.elements.allow.disabled, true);
  assert.match(h.elements.status.textContent, /確認できませんでした/);
});

test('focus during failed in-flight read is coalesced into one recovery read', async () => {
  const h = harness(); for (let i = 0; i < 20; i++) h.focus();
  assert.equal(h.calls.length, 1);
  h.calls[0].reject(new Error('initial focus race')); await flush();
  assert.equal(h.calls.length, 2);
  h.calls[1].resolve(context); await flush();
  assert.equal(h.elements.allow.disabled, false); assert.equal(h.calls.length, 2);
});
