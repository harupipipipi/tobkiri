import assert from 'node:assert/strict';
import test from 'node:test';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { MemoryRouter } from 'react-router';
import { useAppStore } from '@/src/store';
import { Packs } from './Packs';

test('Pack status failure shows a bounded stage and successful retry clears it', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost:18765/panel/packs' });
  const priorState = useAppStore.getState();
  const names = ['window', 'document', 'navigator', 'localStorage', 'IS_REACT_ACT_ENVIRONMENT'];
  const descriptors = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  Object.defineProperties(globalThis, {
    window: { value: dom.window, configurable: true },
    document: { value: dom.window.document, configurable: true },
    navigator: { value: dom.window.navigator, configurable: true },
    localStorage: { value: dom.window.localStorage, configurable: true },
    IS_REACT_ACT_ENVIRONMENT: { value: true, configurable: true, writable: true },
  });
  let calls = 0;
  // This is a JSDOM component fixture, not a native bridge acceptance test.
  Object.defineProperty(dom.window, '__TAURI__', { value: { core: { invoke: async (command: string) => {
    assert.equal(command, 'signed_pack_admission_status');
    calls += 1;
    if (calls === 1) throw 'signed_pack_admission_status not allowed. secret-fixture /private/path';
    return { ready: false, onboarding_supported: true };
  } } } });
  useAppStore.setState({ packs: [], packsLoading: false, packsError: null, loadPacks: async () => {} });
  const container = dom.window.document.getElementById('root');
  assert.ok(container);
  const root = createRoot(container);
  try {
    await act(async () => { root.render(<MemoryRouter><Packs /></MemoryRouter>); });
    assert.equal(calls, 1);
    assert.match(container.querySelector('[role="alert"]')?.textContent ?? '', /PACK_STATUS_NATIVE_DENIED/);
    assert.doesNotMatch(container.textContent ?? '', /secret-fixture|private\/path/);
    const retry = [...container.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Check Pack service');
    assert.ok(retry);
    await act(async () => { retry.click(); });
    assert.equal(calls, 2);
    assert.doesNotMatch(container.textContent ?? '', /PACK_STATUS_NATIVE_DENIED|Host Pack service is unavailable/);
    assert.match(container.textContent ?? '', /Trust and add signed Pack/);
  } finally {
    await act(async () => { root.unmount(); });
    useAppStore.setState(priorState, true);
    dom.window.close();
    for (const name of names) {
      const descriptor = descriptors.get(name);
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});
