import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { MessageChannel as NodeChannel } from 'node:worker_threads';
import { PackPortClient } from '../../../../sdk/js/pack-ui/index.js';
import { mountPackUi, packUiUrl } from './isolatedPackUi.ts';
import { HOST_MAX_BODY_BYTES, HOST_MAX_INFLIGHT, HOST_MAX_RESPONSE_TICKET_BYTES, HostOutputWatermark, UiTransportQueue, appendBoundedHistory, escapedEnvelopeByteBound, jsonEncodedByteLength } from './boundedTransport.ts';

const attachment = { uiSessionId: 'ui_core_minted', generation: 1, packHash: `sha256:${'a'.repeat(64)}`, entry: 'ui/index.html' };

function setup(
  invoke: (_session: string, _generation: number, _call: unknown, _json: string, _signal: AbortSignal) => Promise<unknown> = async () => ({ ok: true }),
  hooks: {
    register?: (_session: string, _generation: number, _call: unknown) => Promise<void>;
    cancel?: (_session: string, _generation: number, _call: unknown) => Promise<void>;
  } = {},
) {
  const dom = new JSDOM('<div id="mount"></div>', { url: 'tauri://localhost/' });
  const calls: unknown[][] = [];
  const registrations: unknown[][] = [];
  const cancellations: unknown[][] = [];
  const closed: string[] = [];
  const replies: unknown[] = [];
  const posted: unknown[][] = [];
  const acknowledged: unknown[][] = [];
  const port1 = { onmessage: null as ((event: { data: unknown }) => void) | null, postMessage: (r: unknown) => replies.push(r), start() {}, close() {} };
  const port2 = { close() {} };
  const mounted = mountPackUi(dom.window.document.getElementById('mount')!, attachment, {
    register: (session, generation, call) => {
      registrations.push([session, generation, call]);
      return hooks.register?.(session, generation, call) ?? Promise.resolve();
    },
    invoke: (session, generation, call, json, signal) => { calls.push([session, generation, call, json, signal]); return invoke(session, generation, call, json, signal); },
    cancel: (session, generation, call) => {
      cancellations.push([session, generation, call]);
      return hooks.cancel?.(session, generation, call) ?? Promise.resolve();
    },
    acknowledge: async (...args) => { acknowledged.push(args); },
    close: async (...args) => { closed.push(args[0]); },
  }, () => ({ port1, port2 }) as unknown as MessageChannel);
  mounted.frame.contentWindow!.postMessage = ((...args: unknown[]) => { posted.push(args); }) as typeof window.postMessage;
  mounted.frame.dispatchEvent(new dom.window.Event('load'));
  return { dom, mounted, calls, registrations, cancellations, closed, replies, posted, acknowledged, send: (data: unknown) => port1.onmessage?.({ data }) };
}

test('opaque frame receives only its one-use port, never a token or global IPC', async () => {
  const s = setup();
  assert.equal(s.mounted.frame.getAttribute('sandbox'), 'allow-scripts');
  assert.equal(s.mounted.frame.getAttribute('referrerpolicy'), 'no-referrer');
  assert.equal(s.mounted.frame.className, 'pack-ui-frame');
  assert.equal(s.posted.length, 1);
  assert.deepEqual(s.posted[0][0], { type: 'tobkiri-pack-channel' });
  assert.equal(s.posted[0][1], '*');
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: { text: 'hello' } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.calls[0][0], attachment.uiSessionId);
  assert.equal(s.calls[0][1], attachment.generation);
  assert.deepEqual(s.replies, [{ id: 1, result: { ok: true } }]);
  assert.deepEqual(s.acknowledged, [[attachment.uiSessionId, attachment.generation, 1]]);
  s.mounted.close();
});

test('host frame keeps sandbox, no-referrer and its stable host class', () => {
  const s = setup();
  assert.equal(s.mounted.frame.getAttribute('sandbox'), 'allow-scripts');
  assert.equal(s.mounted.frame.getAttribute('referrerpolicy'), 'no-referrer');
  assert.ok(s.mounted.frame.classList.contains('pack-ui-frame'));
  s.mounted.close();
  const main = readFileSync(new URL('./main.ts', import.meta.url), 'utf8');
  assert.ok(main.includes("import './style.css'"));
  const sheet = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
  assert.ok(sheet.includes('iframe.pack-ui-frame'));
});

test('forged identities and approval fields are rejected before trusted dispatch', () => {
  const s = setup();
  for (const extra of [{ approved: true }, { risk: 'dangerous' }, { ui_session_id: 'another_ui' }, { profile: 'another_profile' }, { pack: 'other.pack' }]) {
    s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {}, ...extra });
  }
  assert.equal(s.calls.length, 0);
  assert.equal(s.replies.length, 4);
  s.mounted.close();
});

test('frame correlation IDs are contiguous and cannot select host ticket IDs', async () => {
  const s = setup();
  s.send({ type: 'call', id: Number.MAX_SAFE_INTEGER, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  s.send({ type: 'call', id: 3, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0][2] && (s.calls[0][2] as { id: number }).id, 1);
  s.mounted.close();
});

test('cancellation keeps the ticket until response delivery and frame replacement retires the receiver', async () => {
  const s = setup(async (_session, _generation, _call, _json, signal) => new Promise((resolve) => { signal.addEventListener('abort', () => resolve('cancelled')); }));
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  const signal = s.calls[0][4] as AbortSignal;
  s.send({ type: 'cancel', id: 1 });
  assert.equal(signal.aborted, true);
  s.mounted.frame.dispatchEvent(new s.dom.window.Event('load'));
  s.mounted.close();
  assert.deepEqual(s.closed, [attachment.uiSessionId]);
  assert.equal(s.posted.length, 1);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(s.replies, [{ type: 'tobkiri-pack-closed' }]);
});

test('abort before registration ACK waits, then cancels and dispatches exactly once', async () => {
  let acknowledge!: () => void;
  const registration = new Promise<void>((resolve) => { acknowledge = resolve; });
  const s = setup(async (_session, _generation, _call, _json, signal) => new Promise((resolve) => {
    signal.addEventListener('abort', () => resolve('cancelled'), { once: true });
  }), { register: () => registration });
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.registrations.length, 1);
  assert.equal(s.calls.length, 0);
  s.send({ type: 'cancel', id: 1 });
  assert.equal(s.cancellations.length, 0);
  assert.equal(s.calls.length, 0);
  acknowledge();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.cancellations.length, 1);
  assert.deepEqual(s.cancellations[0][2], { id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0][2] && (s.calls[0][2] as { id: number }).id, 1);
  s.mounted.close();
});

test('pre-dispatch abort waits for the native cancel ACK before invoking', async () => {
  let registered!: () => void;
  let cancelled!: () => void;
  const registration = new Promise<void>((resolve) => { registered = resolve; });
  const cancellation = new Promise<void>((resolve) => { cancelled = resolve; });
  const s = setup(async () => null, {
    register: () => registration,
    cancel: () => cancellation,
  });
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  s.send({ type: 'cancel', id: 1 });
  registered();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.cancellations.length, 1);
  assert.equal(s.calls.length, 0, 'independent invoke IPC must wait for cancel IPC');
  cancelled();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.calls.length, 1, 'one dispatch consumes the cancelled registration');
  assert.equal((s.calls[0][4] as AbortSignal).aborted, true);
  s.mounted.close();
});

test('failed pre-dispatch cancellation closes the receiver without new Core work', async () => {
  let registered!: () => void;
  const registration = new Promise<void>((resolve) => { registered = resolve; });
  const s = setup(async () => null, {
    register: () => registration,
    cancel: async () => { throw new Error('IPC unavailable'); },
  });
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  s.send({ type: 'cancel', id: 1 });
  registered();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.cancellations.length, 1);
  assert.equal(s.calls.length, 0);
  assert.deepEqual(s.closed, [attachment.uiSessionId]);
  assert.deepEqual(s.replies, [{ type: 'tobkiri-pack-closed' }]);
});

test('registration failure never sends cancellation or dispatches the operation', async () => {
  const s = setup(async () => ({ shouldNotRun: true }), {
    register: async () => { throw Object.assign(new Error('full'), { code: 'registration-full' }); },
  });
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.registrations.length, 1);
  assert.equal(s.cancellations.length, 0);
  assert.equal(s.calls.length, 0);
  assert.deepEqual(s.replies, [{ id: 1, error: { code: 'registration-full', message: 'Core refused the UI request' } }]);
  s.mounted.close();
});

test('replayed and over-capacity calls cannot execute additional effects', async () => {
  const s = setup(async () => new Promise(() => {}));
  for (let id = 1; id <= HOST_MAX_INFLIGHT; id++) s.send({ type: 'call', id, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  s.send({ type: 'call', id: HOST_MAX_INFLIGHT + 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.calls.length, HOST_MAX_INFLIGHT);
  assert.equal(s.replies.length, 2);
  s.send({ type: 'call', id: HOST_MAX_INFLIGHT + 2, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  assert.deepEqual(s.replies.at(-1), { type: 'tobkiri-pack-closed' });
  assert.deepEqual(s.closed, [attachment.uiSessionId]);
  assert.equal(s.mounted.frame.isConnected, false);
  assert.equal(s.calls.length, HOST_MAX_INFLIGHT);
  s.mounted.close();
});

test('successive oversized refusals close deterministically without dispatch or silent waits', () => {
  const s = setup();
  const args = { text: 'x'.repeat(HOST_MAX_BODY_BYTES) };
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args });
  assert.deepEqual(s.replies, [{ id: 1, error: { code: 'limit', message: 'UI request refused' } }]);
  s.send({ type: 'call', id: 2, shape: 'owner.pack.port@1.0', op: 'read', args });
  assert.deepEqual(s.replies.at(-1), { type: 'tobkiri-pack-closed' });
  assert.deepEqual(s.closed, [attachment.uiSessionId]);
  assert.equal(s.calls.length, 0);
  assert.equal(s.registrations.length, 0);
});

test('asset URLs cannot select a source path or forge a different scheme', () => {
  assert.equal(packUiUrl(attachment), `tobkiri-pack://${'a'.repeat(64)}/ui/index.html`);
  assert.equal(packUiUrl({ ...attachment, platform: 'windows' }), `http://tobkiri-pack.localhost/${'a'.repeat(64)}/ui/index.html`);
  for (const entry of ['../key', '/key', 'https://evil/ui', 'ui\\key']) {
    assert.throws(() => packUiUrl({ ...attachment, entry }));
  }
  assert.throws(() => packUiUrl({ ...attachment, packHash: 'http://evil' }));
  assert.equal(packUiUrl({ ...attachment, entry: 'ui/日本 name.html' }), `tobkiri-pack://${'a'.repeat(64)}/ui/%E6%97%A5%E6%9C%AC%20name.html`);
  assert.equal(packUiUrl({ ...attachment, entry: 'ui/%2e%2e.html' }), `tobkiri-pack://${'a'.repeat(64)}/ui/%252e%252e.html`);
});

test('stream requests preserve only scoped handle fields and the Core batch ceiling', async () => {
  const s = setup(async () => [{ text: 'one' }]);
  s.send({ type: 'stream-pull', id: 1, handle: 3, max: 32 });
  s.send({ type: 'stream-pull', id: 2, handle: 3, max: 33 });
  s.send({ type: 'stream-cancel', id: 3, handle: 3, ui_session_id: 'another_ui' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0][0], attachment.uiSessionId);
  assert.deepEqual(s.calls[0][2], { type: 'stream-pull', id: 1, handle: 3, max: 32 });
  assert.equal(s.replies.length, 3);
  s.send({ type: 'close' });
  assert.deepEqual(s.closed, [attachment.uiSessionId]);
  assert.equal(s.mounted.frame.isConnected, false);
});

test('actual SDK exchanges generic values and scoped streams over the isolated channel', async () => {
  const dom = new JSDOM('<main></main>', { url: 'tauri://localhost/' });
  const calls: unknown[] = [];
  let client: PackPortClient | undefined;
  let detached = false;
  const mounted = mountPackUi(dom.window.document.querySelector('main')!, attachment, {
    register: async () => {},
    invoke: async (ui, generation, request) => {
      assert.equal(ui, attachment.uiSessionId);
      assert.equal(generation, attachment.generation);
      calls.push(request);
      if ('type' in request) return request.type === 'stream-pull' ? [{ value: 'exact' }] : null;
      return request.op === 'stream' ? { kind: 'stream', handle: 17 } : { kind: 'value', value: { float: '-0e0' } };
    },
    cancel: async () => {},
    acknowledge: async (ui, generation) => { assert.equal(ui, attachment.uiSessionId); assert.equal(generation, attachment.generation); },
    close: async (ui) => { assert.equal(ui, attachment.uiSessionId); detached = true; },
  }, () => new NodeChannel() as unknown as MessageChannel);
  mounted.frame.contentWindow!.postMessage = ((_data, _target, ports) => {
    client = new PackPortClient((ports as Transferable[])[0]);
  }) as typeof window.postMessage;
  mounted.frame.dispatchEvent(new dom.window.Event('load'));
  try {
    assert.deepEqual(await client!.call('owner.pack.port@1.0', 'read', {}), { float: '-0e0' });
    const stream = await client!.call('owner.pack.port@1.0', 'stream', {});
    assert.deepEqual(await stream.pull(32), [{ value: 'exact' }]);
    await stream.cancel();
    assert.equal(calls.length, 4);
    client!.close();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(detached, true);
  } finally { client?.close(); mounted.close(); dom.window.close(); }
});

test('transport sizing accounts for JSON escaping before serialization and rejects unsupported JSON', () => {
  const hostile = { text: '\u0000\u0001"\\', astral: '🦦', safe: 42 };
  const measured = jsonEncodedByteLength(hostile);
  assert.equal(measured, Buffer.byteLength(JSON.stringify(hostile)));
  assert.equal(jsonEncodedByteLength({ value: 1.5 }), null);
  assert.equal(jsonEncodedByteLength({ value: Number.MAX_SAFE_INTEGER + 1 }), null);
  assert.equal(jsonEncodedByteLength({ value: 'x'.repeat(HOST_MAX_BODY_BYTES) }), null);
  assert.equal(escapedEnvelopeByteBound(HOST_MAX_BODY_BYTES), HOST_MAX_RESPONSE_TICKET_BYTES);
  assert.equal(escapedEnvelopeByteBound(HOST_MAX_BODY_BYTES + 1), null);
});

test('delayed, duplicate and stale acknowledgements do not release another ticket', () => {
  const queue = new UiTransportQueue();
  assert.equal(queue.reserve(1, 32), true);
  assert.equal(queue.size, 1);
  assert.equal(queue.acknowledge(2), false);
  assert.equal(queue.size, 1);
  assert.equal(queue.acknowledge(1), true);
  assert.equal(queue.acknowledge(1), false);
  assert.equal(queue.size, 0);
});

test('host-output ticket watermark resets when the process namespace changes', () => {
  const watermark = new HostOutputWatermark();
  const oldNamespace = `sha256:${'a'.repeat(64)}`;
  const newNamespace = `sha256:${'b'.repeat(64)}`;
  watermark.useNamespace(oldNamespace);
  assert.equal(watermark.accept(oldNamespace, 1), true);
  assert.equal(watermark.accept(oldNamespace, 1), false);
  assert.equal(watermark.accept(oldNamespace, 2), true);

  watermark.useNamespace(newNamespace);
  assert.equal(watermark.accept(newNamespace, 1), true);
  assert.equal(watermark.accept(oldNamespace, 3), false);
  assert.equal(watermark.accept(newNamespace, 1), false);
});

test('oversized Core result is refused before it enters the iframe message queue', async () => {
  const s = setup(async () => ({ body: 'x'.repeat(HOST_MAX_BODY_BYTES) }));
  s.send({ type: 'call', id: 1, shape: 'owner.pack.port@1.0', op: 'read', args: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(s.replies, [{ id: 1, error: { code: 'limit', message: 'Core response exceeded the UI limit' } }]);
  assert.deepEqual(s.acknowledged, [[attachment.uiSessionId, attachment.generation, 1]]);
  s.mounted.close();
});

test('receiver loss clears its bounded frontend queue and saturation rejects without retry buffering', () => {
  const queue = new UiTransportQueue();
  for (let id = 1; id <= HOST_MAX_INFLIGHT; id += 1) assert.equal(queue.reserve(id, HOST_MAX_BODY_BYTES), true);
  assert.equal(queue.reserve(HOST_MAX_INFLIGHT + 1, 1), false);
  assert.ok(queue.reservedBytes <= HOST_MAX_INFLIGHT * HOST_MAX_RESPONSE_TICKET_BYTES * 2);
  queue.clear(); // The matching native detach retires the receiver's tickets.
  assert.equal(queue.size, 0);
  assert.equal(queue.reservedBytes, 0);

  const oldNamespace = `sha256:${'a'.repeat(64)}`;
  for (const receiver of [
    `${oldNamespace}\u00001\u0000ui-a`,
    `${oldNamespace}\u00001\u0000ui-b`,
    `${oldNamespace}\u00001\u0000ui-c`,
    `${oldNamespace}\u00001\u0000ui-d`,
  ]) {
    assert.equal(queue.reserve(1, 32, receiver), true);
  }
  assert.equal(queue.reserve(1, 32, `sha256:${'b'.repeat(64)}\u00001\u0000ui-a`), false);
  queue.clearReceiver(`${oldNamespace}\u00001\u0000ui-b`);
  assert.equal(queue.size, HOST_MAX_INFLIGHT - 1);
  assert.equal(queue.acknowledge(1, `${oldNamespace}\u00001\u0000ui-b`), false);
  queue.clear();
  assert.equal(queue.size, 0);

  const oldProcess = `sha256:${'c'.repeat(64)}\u00001\u0000ui-a`;
  const newProcess = `sha256:${'d'.repeat(64)}\u00001\u0000ui-a`;
  assert.equal(queue.reserve(1, 32, oldProcess), true);
  assert.equal(queue.reserve(1, 32, newProcess), true);
  queue.clearReceiver(oldProcess);
  assert.equal(queue.size, 1);
  assert.equal(queue.acknowledge(1, newProcess), true);
});

test('terminal history caps both retained text and a single oversized event chunk', () => {
  const maximum = 262144;
  const next = appendBoundedHistory('old'.repeat(maximum), 'new'.repeat(maximum), maximum);
  assert.equal(next.length, maximum);
  assert.equal(next, 'new'.repeat(maximum).slice(-maximum));
});
