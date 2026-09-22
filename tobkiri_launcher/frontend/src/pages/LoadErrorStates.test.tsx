import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter, Route, Routes} from 'react-router';

import type {Pack} from '@/src/store';
import {useAppStore} from '@/src/store';
import {setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';
import {PackDetail} from './PackDetail';
import {Packs} from './Packs';

const cachedPack: Pack = {
  id: 'cached', name: 'Cached Pack', version: '1.0.0', type: 'community', enabled: true,
  installed: true,
  description: 'Previously loaded', approvalStatus: 'approved', approvalReason: null,
  artifactDigest: 'sha256:cached-artifact', profileId: 'profile-a', workspaceId: 'workspace-a',
  profileRevision: 'sha256:profile-a', planDigest: 'sha256:plan-a', catalogRevision: 'catalog-a',
  approved: true, hashValid: true, criticalChanged: false, approvalIssues: [],
  capabilities: [], flows: [], dependencies: [],
};

async function renderPage(element: ReactNode) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', {url: 'http://localhost/'});
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    sessionStorage: dom.window.sessionStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  let root!: ReturnType<typeof createRoot>;
  await act(async () => {
    root = createRoot(document.getElementById('root')!);
    root.render(element);
    await new Promise(resolve => setTimeout(resolve, 0));
  });
  return {dom, root};
}

test('Packs retains cached data and marks it stale after a refresh failure', async () => {
  setRuntimeDispatchStatus('runtime_ready');
  useAppStore.setState({packs: [cachedPack], apiError: null, isLoading: false});
  const dispatchError = "('tobkiri.host.pack-control.v4', 'catalog.read')";
  globalThis.fetch = (async () => new Response(JSON.stringify({success: false, data: null, error: dispatchError}), {status: 409})) as typeof fetch;
  const {dom, root} = await renderPage(<MemoryRouter><Packs /></MemoryRouter>);
  const previousNavigator = globalThis.navigator;
  let copied = '';

  try {
    Object.defineProperty(globalThis, 'navigator', {
      value: dom.window.navigator,
      configurable: true,
    });
    Object.defineProperty(dom.window.navigator, 'clipboard', {
      configurable: true,
      value: {writeText: async (text: string) => { copied = text; }},
    });
    assert.match(document.body.textContent ?? '', /Packs could not be loaded/);
    assert.match(document.body.textContent ?? '', /tobkiri\.host\.pack-control\.v4/);
    assert.match(document.body.textContent ?? '', /catalog\.read/);
    assert.match(document.body.textContent ?? '', /Showing the last successfully loaded data/);
    assert.match(document.body.textContent ?? '', /Cached Pack/);
    const errorIcon = document.querySelector<SVGElement>('[data-error-icon="load"]');
    assert.ok(errorIcon);
    assert.ok(errorIcon.classList.contains('lucide-circle-alert'));
    const copyButton = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Copy Packs could not be loaded error"]',
    );
    assert.ok(copyButton);
    assert.ok(copyButton.querySelector('svg.lucide-copy'));
    assert.equal(copyButton.querySelector('svg.lucide-circle-alert'), null);
    await act(async () => {
      copyButton.click();
      await Promise.resolve();
    });
    assert.equal(copied, [
      'Packs could not be loaded',
      dispatchError,
      'Showing the last successfully loaded data.',
    ].join('\n'));
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'navigator', {
      value: previousNavigator,
      configurable: true,
    });
  }
});

test('PackDetail does not mislabel a failed catalog request as an unknown id', async () => {
  setRuntimeDispatchStatus('runtime_ready');
  useAppStore.setState({packs: [], apiError: null, isLoading: false});
  globalThis.fetch = (async () => new Response(JSON.stringify({success: false, data: null, error: 'Service unavailable'}), {status: 503})) as typeof fetch;
  const {dom, root} = await renderPage(
    <MemoryRouter initialEntries={['/packs/missing']}>
      <Routes><Route path="/packs/:id" element={<PackDetail />} /></Routes>
    </MemoryRouter>,
  );
  assert.match(document.body.textContent ?? '', /Pack details could not be loaded/);
  assert.doesNotMatch(document.body.textContent ?? '', /Pack not found/);
  await act(async () => root.unmount());
  dom.window.close();
});
