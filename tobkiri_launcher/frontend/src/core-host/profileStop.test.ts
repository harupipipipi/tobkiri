import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { ProfileStopController } from './profileStop.ts';

const flush = () => new Promise((resolve) => setImmediate(resolve));

const deferred = () => {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

const setup = (invokeStop: () => Promise<unknown> = () => Promise.resolve()) => {
  const dom = new JSDOM(
    '<p id="status" role="status"></p><button id="stop" type="button">Stop</button><main id="pack-ui"><iframe></iframe></main>',
    { url: 'tauri://localhost/' },
  );
  const status = dom.window.document.getElementById('status') as HTMLElement;
  const stopButton = dom.window.document.getElementById('stop') as HTMLButtonElement;
  const packUi = dom.window.document.getElementById('pack-ui') as HTMLElement;
  const calls = { advances: 0, prepares: 0, invokes: 0 };
  const order: string[] = [];
  const hooks: { onPrepare?: () => void } = {};
  let generation = 0;
  let framePresentAtInvoke: boolean | null = null;
  const controller = new ProfileStopController(status, stopButton, {
    getGeneration: () => generation,
    advanceGeneration: () => {
      calls.advances += 1;
      order.push('advance');
      return ++generation;
    },
    prepareStop: () => {
      calls.prepares += 1;
      order.push('prepare');
      packUi.replaceChildren();
      hooks.onPrepare?.();
    },
    invokeStop: () => {
      calls.invokes += 1;
      order.push('invoke');
      framePresentAtInvoke = packUi.querySelector('iframe') !== null;
      return invokeStop();
    },
  });
  // Mirrors main.ts: one Stop entry shared by click and once:true pagehide.
  stopButton.addEventListener('click', controller.stop);
  dom.window.addEventListener('pagehide', controller.stop, { once: true });
  return {
    controller,
    status,
    stopButton,
    calls,
    order,
    hooks,
    get framePresentAtInvoke() {
      return framePresentAtInvoke;
    },
    click: () => stopButton.dispatchEvent(new dom.window.MouseEvent('click')),
    pagehide: () => dom.window.dispatchEvent(new dom.window.Event('pagehide')),
    // Mirrors main.ts: Start advances the generation, then rearms the latch.
    startNewContext: () => {
      generation += 1;
      controller.resetForStart();
    },
  };
};

test('accepted stop latches synchronously and publishes deferred success', async () => {
  const pending = deferred();
  const s = setup(() => pending.promise);
  s.status.textContent = 'Profile running.';
  assert.equal(s.stopButton.disabled, false);

  let reentered = false;
  s.hooks.onPrepare = () => {
    if (reentered) return;
    reentered = true;
    s.click();
    s.controller.stop();
  };
  s.click();
  assert.equal(s.status.textContent, 'Stopping Profile…');
  assert.equal(s.stopButton.disabled, true);
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  assert.deepEqual(s.order, ['advance', 'prepare', 'invoke']);
  assert.equal(s.framePresentAtInvoke, false);

  s.click();
  s.controller.stop();
  s.pagehide();
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  assert.equal(s.status.textContent, 'Stopping Profile…');

  pending.resolve();
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Profile stopped.');
  assert.equal(s.stopButton.disabled, true);

  s.click();
  s.controller.stop();
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  assert.equal(s.status.textContent, 'Profile stopped.');
});

test('backend rejection shows the fixed failure label and stays latched', async () => {
  const pending = deferred();
  const s = setup(() => pending.promise);
  s.status.textContent = 'Profile running.';
  s.click();
  assert.equal(s.status.textContent, 'Stopping Profile…');

  let messageRead = false;
  const failure = new Error('PRIVATE-STOP-DETAIL-9f27');
  Object.defineProperty(failure, 'message', {
    get: () => {
      messageRead = true;
      return 'PRIVATE-STOP-DETAIL-9f27';
    },
  });
  pending.reject(failure);
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Profile stop failed.');
  assert.equal(messageRead, false);
  assert.ok(!String(s.status.textContent ?? '').includes('PRIVATE-STOP-DETAIL'));
  assert.equal(s.stopButton.disabled, true);

  s.click();
  s.controller.stop();
  s.pagehide();
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  assert.equal(s.status.textContent, 'Profile stop failed.');
});

test('a synchronous invoke throw uses the same fixed failure path once', () => {
  const s = setup(() => {
    throw new Error('PRIVATE-SYNC-THROW');
  });
  s.click();
  assert.equal(s.status.textContent, 'Profile stop failed.');
  assert.equal(s.stopButton.disabled, true);
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  s.controller.stop();
  assert.equal(s.calls.invokes, 1);
});

test('pagehide alone issues one stop for the displayed context', async () => {
  const pending = deferred();
  const s = setup(() => pending.promise);
  s.status.textContent = 'Profile running.';
  s.pagehide();
  assert.equal(s.status.textContent, 'Stopping Profile…');
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  s.pagehide();
  s.controller.stop();
  s.click();
  assert.deepEqual(s.calls, { advances: 1, prepares: 1, invokes: 1 });
  pending.resolve();
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Profile stopped.');
});

test('a stale resolve cannot overwrite a newer start context', async () => {
  const first = deferred();
  const s = setup(() => first.promise);
  s.click();
  assert.equal(s.status.textContent, 'Stopping Profile…');

  s.startNewContext();
  s.status.textContent = 'Profile running.';
  assert.equal(s.stopButton.disabled, false);

  first.resolve();
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Profile running.');
  assert.equal(s.stopButton.disabled, false);
  assert.equal(s.calls.invokes, 1);
});

test('a stale reject cannot overwrite a newer start context', async () => {
  const first = deferred();
  const s = setup(() => first.promise);
  s.click();
  s.startNewContext();
  s.status.textContent = 'Profile running.';

  first.reject(new Error('PRIVATE-STALE-STOP'));
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Profile running.');
  assert.equal(s.stopButton.disabled, false);
  assert.equal(s.calls.invokes, 1);
});

test('an old settle during a newer deferred stop keeps the pending state', async () => {
  const stops = [deferred(), deferred()];
  let which = 0;
  const s = setup(() => stops[which++].promise);
  s.click();
  s.startNewContext();
  s.status.textContent = 'Profile running.';
  s.click();
  assert.equal(s.status.textContent, 'Stopping Profile…');
  assert.equal(s.stopButton.disabled, true);
  assert.deepEqual(s.calls, { advances: 2, prepares: 2, invokes: 2 });

  stops[0].resolve();
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Stopping Profile…');
  assert.equal(s.stopButton.disabled, true);

  s.click();
  s.controller.stop();
  assert.equal(s.calls.invokes, 2);

  stops[1].resolve();
  await flush();
  await flush();
  assert.equal(s.status.textContent, 'Profile stopped.');
  s.click();
  assert.equal(s.calls.invokes, 2);
});
