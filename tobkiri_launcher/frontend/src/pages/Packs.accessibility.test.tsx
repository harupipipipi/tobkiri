import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter, Route, Routes} from 'react-router';

import type {PackControlBinding} from '@/src/lib/apiTypes';
import {type Pack, useAppStore} from '@/src/store';
import {Packs} from './Packs';

const samplePack: Pack = {
  id: 'research-pack',
  name: 'Research Pack',
  version: '1.2.3',
  type: 'community',
  installed: true,
  enabled: false,
  description: 'Research tools',
  artifactDigest: 'sha256:research-artifact',
  profileId: 'profile-a',
  workspaceId: 'workspace-a',
  profileRevision: 'sha256:profile-a',
  planDigest: 'sha256:plan-a',
  catalogRevision: 'catalog-a',
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

const availablePack: Pack = {
  ...samplePack,
  id: 'available-pack',
  name: 'Available Pack',
  installed: false,
  enabled: false,
  approvalStatus: 'unknown',
  approved: false,
};

const activePackBinding: PackControlBinding = {
  profile_id: samplePack.profileId,
  workspace_id: samplePack.workspaceId,
  profile_revision: samplePack.profileRevision,
  plan_digest: samplePack.planDigest,
  catalog_revision: samplePack.catalogRevision,
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
    packCatalogBinding: activePackBinding,
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

test('Packs requires installation before approval or enablement', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/packs',
  });
  const previousState = useAppStore.getState();
  let installCount = 0;
  let approveCount = 0;
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  useAppStore.setState({
    packs: [availablePack],
    packCatalogBinding: activePackBinding,
    isLoading: false,
    loadPacks: async () => {},
    installPack: async () => {
      installCount += 1;
    },
    approvePack: async () => {
      approveCount += 1;
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
        </Routes>
      </MemoryRouter>,
    );
  });

  try {
    const installButton = [...container.querySelectorAll('button')].find(
      (button) => button.textContent?.trim() === 'Install',
    );
    assert.ok(installButton);
    assert.match(container.textContent ?? '', /Available/);
    assert.doesNotMatch(container.textContent ?? '', /Approve/);
    assert.equal(container.querySelector('[role="switch"]'), null);

    await act(async () => installButton.click());
    assert.equal(installCount, 1);
    assert.equal(approveCount, 0);
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('Packs exposes required Profile Packs without revoke or toggle actions', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/packs',
  });
  const previousState = useAppStore.getState();
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  useAppStore.setState({
    packs: [{...samplePack, required: true}],
    packCatalogBinding: activePackBinding,
    isLoading: false,
    loadPacks: async () => {},
  });
  const container = document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root: Root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/packs']}>
        <Routes>
          <Route path="/packs" element={<Packs />} />
        </Routes>
      </MemoryRouter>,
    );
  });

  try {
    assert.match(container.textContent ?? '', /Required by active execution Profile · profile-a/);
    assert.match(container.textContent ?? '', /Host-global artifact inventory/);
    assert.match(container.textContent ?? '', /Host-global artifact inventory and install state/);
    assert.equal(container.querySelector('[role="switch"]'), null);
    assert.equal(container.querySelector('[aria-label^="Revoke approval"]'), null);
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});

test('Packs locks Profile-scoped state when the catalog binding does not match a row', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/packs',
  });
  const previousState = useAppStore.getState();
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  useAppStore.setState({
    packs: [{...samplePack, required: true}],
    packCatalogBinding: {...activePackBinding, profile_id: 'profile-b'},
    isLoading: false,
    loadPacks: async () => {},
  });
  const container = document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root: Root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/packs']}>
        <Routes>
          <Route path="/packs" element={<Packs />} />
        </Routes>
      </MemoryRouter>,
    );
  });

  try {
    assert.match(container.textContent ?? '', /Profile state unavailable/);
    assert.match(container.textContent ?? '', /Profile-scoped requirement unavailable/);
    assert.equal(container.querySelector('[role="switch"]'), null);
    assert.equal(container.querySelector('[aria-label^="Revoke approval"]'), null);
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
  }
});
