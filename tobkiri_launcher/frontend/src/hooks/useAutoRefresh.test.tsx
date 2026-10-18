import assert from 'node:assert/strict';
import test, {type TestContext} from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {useAutoRefresh} from './useAutoRefresh';

async function harness(
  context: TestContext,
  refresh: (signal: AbortSignal) => Promise<boolean>,
) {
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const dom = new JSDOM('<div id="root"></div>', {pretendToBeVisual: true});
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  context.mock.timers.enable({apis: ['setTimeout']});
  const root = createRoot(dom.window.document.getElementById('root')!);
  function Harness({paused = false}: {paused?: boolean}) {
    useAutoRefresh(refresh, {paused});
    return null;
  }
  const render = async (paused = false) => {
    await act(async () => root.render(<Harness paused={paused} />));
  };
  await render();
  context.after(async () => {
    await act(async () => root.unmount());
    context.mock.timers.reset();
    dom.window.close();
    for (const key of ['window', 'document', 'navigator', 'IS_REACT_ACT_ENVIRONMENT']) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  });
  return {
    dom,
    render,
    tick: async (milliseconds: number) => {
      await act(async () => context.mock.timers.tick(milliseconds));
    },
    event: async (target: EventTarget, name: string) => {
      await act(async () => target.dispatchEvent(new dom.window.Event(name)));
    },
  };
}

test('automatic reads back off after failures and return to the normal interval after recovery', async (context) => {
  let calls = 0;
  const h = await harness(context, async () => ++calls >= 3);
  assert.equal(calls, 1);
  await h.tick(4_999);
  assert.equal(calls, 1);
  await h.tick(1);
  assert.equal(calls, 2);
  await h.tick(9_999);
  assert.equal(calls, 2);
  await h.tick(1);
  assert.equal(calls, 3);
  await h.tick(29_999);
  assert.equal(calls, 3);
  await h.tick(1);
  assert.equal(calls, 4);
});

test('focus and online events cannot overlap an in-flight read', async (context) => {
  let calls = 0;
  let complete: (success: boolean) => void = () => undefined;
  const h = await harness(context, async () => {
    calls += 1;
    return new Promise<boolean>((resolve) => { complete = resolve; });
  });
  await h.event(h.dom.window, 'focus');
  await h.event(h.dom.window, 'online');
  await h.tick(60_000);
  assert.equal(calls, 1);
  await act(async () => complete(true));
  await h.tick(30_000);
  assert.equal(calls, 2);
  await act(async () => complete(true));
});

test('hidden and offline pages stop polling and resume when available', async (context) => {
  let calls = 0;
  const h = await harness(context, async () => { calls += 1; return true; });
  Object.defineProperty(h.dom.window.document, 'visibilityState', {value: 'hidden', configurable: true});
  await h.event(h.dom.window.document, 'visibilitychange');
  await h.tick(90_000);
  assert.equal(calls, 1);
  Object.defineProperty(h.dom.window.document, 'visibilityState', {value: 'visible', configurable: true});
  await h.event(h.dom.window.document, 'visibilitychange');
  assert.equal(calls, 2);
  Object.defineProperty(h.dom.window.navigator, 'onLine', {value: false, configurable: true});
  await h.event(h.dom.window, 'offline');
  await h.tick(90_000);
  assert.equal(calls, 2);
  Object.defineProperty(h.dom.window.navigator, 'onLine', {value: true, configurable: true});
  await h.event(h.dom.window, 'online');
  assert.equal(calls, 3);
});

test('pausing for a mutation invalidates the preceding read and reads again after the mutation', async (context) => {
  const signals: AbortSignal[] = [];
  let complete: (success: boolean) => void = () => undefined;
  const h = await harness(context, async (signal) => {
    signals.push(signal);
    return new Promise<boolean>((resolve) => { complete = resolve; });
  });
  await h.render(true);
  assert.equal(signals[0].aborted, true);
  await act(async () => complete(true));
  await h.tick(90_000);
  assert.equal(signals.length, 1);
  await h.render(false);
  assert.equal(signals.length, 2);
  assert.equal(signals[1].aborted, false);
  await act(async () => complete(true));
});
