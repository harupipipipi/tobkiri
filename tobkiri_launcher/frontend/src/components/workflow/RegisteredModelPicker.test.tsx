import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {RegisteredModelPicker} from './RegisteredModelPicker';
import type {WorkflowModelSnapshot} from '@/src/lib/workflowModelCatalog';

test('disabled inspection does not load active Profile models or accept an older pending read', async () => {
  const previous = {window: globalThis.window, document: globalThis.document, navigator: globalThis.navigator};
  const dom = new JSDOM('<div id="root"></div>', {url: 'https://launcher.test/'});
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root')!;
  const root = createRoot(container);
  let reads = 0;
  let resolve!: (snapshot: WorkflowModelSnapshot) => void;
  const load = () => {reads++; return new Promise<WorkflowModelSnapshot>(done => {resolve = done;});};
  const render = (disabled: boolean) => <RegisteredModelPicker value="" contextKey="profile-a" disabled={disabled} load={load} onChange={() => {throw new Error('No selection was authorized');}}/>;
  try {
    await act(async () => root.render(render(true)));
    assert.equal(reads, 0);
    await act(async () => root.render(render(false)));
    assert.equal(reads, 1);
    await act(async () => root.render(render(true)));
    await act(async () => resolve({revision: 1, models: [{profileId: 'old-model', modelId: 'old', displayName: 'Old active model', providerId: 'p'}]}));
    assert.doesNotMatch(container.textContent ?? '', /Old active model/);
    assert.equal(container.querySelector('select')?.disabled, true);
    await act(async () => root.render(render(false)));
    assert.equal(reads, 2);
    await act(async () => dom.window.dispatchEvent(new dom.window.Event('tobkiri-model-profiles-changed')));
    assert.equal(reads, 3, 'an inline model registration refreshes the current picker');
    await act(async () => root.render(render(true)));
    await act(async () => dom.window.dispatchEvent(new dom.window.Event('tobkiri-model-profiles-changed')));
    assert.equal(reads, 3, 'inspection must not refresh another active Profile registry');
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previous.window, configurable: true},
      document: {value: previous.document, configurable: true},
      navigator: {value: previous.navigator, configurable: true},
    });
  }
});
