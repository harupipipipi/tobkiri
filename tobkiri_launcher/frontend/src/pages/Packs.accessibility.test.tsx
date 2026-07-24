import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter, Route, Routes} from 'react-router';

import {type Pack, useAppStore} from '@/src/store';
import {Packs} from './Packs';

const samplePack: Pack = {
  id: 'research-pack',
  name: 'Research Pack',
  version: '1.2.3',
  type: 'community',
  enabled: false,
  description: 'Research tools',
  approvalStatus: 'approved',
  approvalReason: null,
  approved: true,
  hashValid: true,
  criticalChanged: false,
  approvalIssues: [],
  capabilities: [],
  flows: [],
  dependencies: [],
};

test('Packs provides independent semantic detail and switch actions', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/packs',
  });
  const previousState = useAppStore.getState();
  let toggleCount = 0;
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  useAppStore.setState({
    packs: [samplePack],
    isLoading: false,
    loadPacks: async () => {},
    togglePack: async () => {
      toggleCount += 1;
      return true;
    },
  });
  const container = document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root: Root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/packs']}>
        <Routes>
          <Route path="/packs" element={<Packs />} />
          <Route path="/packs/:id" element={<p>Pack detail reached</p>} />
        </Routes>
      </MemoryRouter>,
    );
  });

  try {
    const detailLink = container.querySelector<HTMLAnchorElement>('a[href="/packs/research-pack"]');
    const packSwitch = container.querySelector<HTMLButtonElement>('[role="switch"]');
    assert.ok(detailLink);
    assert.ok(packSwitch);
    assert.equal(detailLink.contains(packSwitch), false);
    assert.equal(detailLink.getAttribute('aria-label'), 'Open Research Pack details');
    assert.match(detailLink.className, /focus-visible:ring-2/);
    assert.match(detailLink.className, /min-h-11/);
    assert.match(packSwitch.className, /after:-inset-2\.5/);

    detailLink.focus();
    assert.equal(document.activeElement, detailLink);
    detailLink.dispatchEvent(new window.KeyboardEvent('keydown', {key: ' ', bubbles: true}));
    assert.doesNotMatch(container.textContent ?? '', /Pack detail reached/);
    assert.equal(toggleCount, 0);

    await act(async () => {
      packSwitch.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      packSwitch.click();
    });
    assert.equal(toggleCount, 1);
    assert.doesNotMatch(container.textContent ?? '', /Pack detail reached/);

    await act(async () => {
      packSwitch.dispatchEvent(new window.KeyboardEvent('keydown', {key: ' ', bubbles: true}));
      packSwitch.click();
    });
    assert.equal(toggleCount, 2);
    assert.doesNotMatch(container.textContent ?? '', /Pack detail reached/);

    await act(async () => detailLink.click());
    assert.match(container.textContent ?? '', /Pack detail reached/);
    assert.equal(toggleCount, 2);
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});
