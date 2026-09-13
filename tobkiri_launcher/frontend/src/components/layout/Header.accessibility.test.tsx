import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';
import {MemoryRouter} from 'react-router';

import {Header} from './Header';
import {useAppStore} from '@/src/store';

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

const GLOBAL_SURFACE_KEYS = [
  'window',
  'document',
  'navigator',
  'localStorage',
  'IS_REACT_ACT_ENVIRONMENT',
] as const;

type GlobalSurfaceKey = (typeof GLOBAL_SURFACE_KEYS)[number];
type GlobalSurfaceSnapshot = {
  [key in GlobalSurfaceKey]: PropertyDescriptor | undefined;
};

function captureGlobalSurface(): GlobalSurfaceSnapshot {
  return {
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
    document: Object.getOwnPropertyDescriptor(globalThis, 'document'),
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    IS_REACT_ACT_ENVIRONMENT: Object.getOwnPropertyDescriptor(globalThis, 'IS_REACT_ACT_ENVIRONMENT'),
  };
}

function restoreGlobalSurface(snapshot: GlobalSurfaceSnapshot): void {
  for (const key of GLOBAL_SURFACE_KEYS) {
    const descriptor = snapshot[key];
    if (descriptor) {
      Object.defineProperty(globalThis, key, descriptor);
    } else {
      Reflect.deleteProperty(globalThis, key);
    }
  }
}

type Surface = ReturnType<typeof createSurface>;

async function cleanupSurface(
  surface: Surface | undefined,
  snapshot: GlobalSurfaceSnapshot,
  release: () => void,
  restoreState?: () => void,
): Promise<void> {
  try {
    if (surface) {
      await act(async () => { surface.root.unmount(); });
    }
  } finally {
    try {
      restoreState?.();
    } finally {
      try {
        surface?.dom.window.close();
      } finally {
        try {
          restoreGlobalSurface(snapshot);
        } finally {
          release();
        }
      }
    }
  }
}

const nextTick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

async function waitForFocus(dom: JSDOM, target: HTMLElement): Promise<void> {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    if (dom.window.document.activeElement === target) return;
    await act(async () => { await nextTick(); });
  }
  assert.ok(dom.window.document.activeElement === target, 'expected the popover to move focus');
}

let domTestLock = Promise.resolve();

async function acquireDomTestLock(): Promise<() => void> {
  const previous = domTestLock;
  let release!: () => void;
  domTestLock = new Promise<void>((resolve) => {
    release = resolve;
  });
  await previous;
  return release;
}

test('Header avatar is an actionable Profile/Settings entry with focus, Escape, return focus, and tap behavior', {concurrency: false}, async () => {
  const release = await acquireDomTestLock();
  const globalSurfaceSnapshot = captureGlobalSurface();
  const previousState = useAppStore.getState();
  let surface: Surface | undefined;
  try {
    surface = createSurface();
    const {dom, container, root} = surface;
    useAppStore.setState({profile: {...previousState.profile, username: 'Test User'}});
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/']}>
          <Header />
        </MemoryRouter>,
      );
    });
    const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="Test User profile and settings"]');
    assert.ok(trigger);
    assert.match(trigger.className, /min-h-11/);
    trigger.focus();
    assert.ok(dom.window.document.activeElement === trigger);

    await act(async () => { trigger.click(); await nextTick(); });
    const dialog = dom.window.document.querySelector('[role="dialog"][aria-label="Profile menu"]');
    assert.ok(dialog);
    assert.ok(dialog.querySelector('a[href="/profile"]'));
    assert.ok(dialog.querySelector('a[href="/settings"]'));
    assert.equal(dialog.querySelector('[role="menuitem"]'), null);

    await act(async () => {
      dom.window.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Escape'}));
      await nextTick();
    });
    assert.equal(dom.window.document.querySelector('[role="dialog"][aria-label="Profile menu"]'), null);
    await act(async () => { await nextTick(); });
    assert.ok(dom.window.document.activeElement === trigger);

    await act(async () => { trigger.click(); await nextTick(); });
    const settings = dom.window.document.querySelector<HTMLAnchorElement>('[role="dialog"][aria-label="Profile menu"] a[href="/settings"]');
    assert.ok(settings);
    await act(async () => { settings.click(); await nextTick(); });
    assert.equal(dom.window.document.querySelector('[role="dialog"][aria-label="Profile menu"]'), null);
  } finally {
    await cleanupSurface(surface, globalSurfaceSnapshot, release, () => useAppStore.setState(previousState, true));
  }
});

test('mobile navigation exposes ordinary named links, moves focus, and closes on Escape or selection', {concurrency: false}, async () => {
  const release = await acquireDomTestLock();
  const globalSurfaceSnapshot = captureGlobalSurface();
  const previousState = useAppStore.getState();
  let surface: Surface | undefined;
  try {
    surface = createSurface();
    const {dom, container, root} = surface;
    useAppStore.setState({devtoolsEnabled: true});
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/graphs']}>
          <Header />
        </MemoryRouter>,
      );
    });
    const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="Open navigation"]');
    assert.ok(trigger);
    assert.equal(trigger.getAttribute('aria-haspopup'), 'dialog');
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');

    await act(async () => { trigger.click(); await nextTick(); });
    const navigationDialog = dom.window.document.querySelector<HTMLElement>('[role="dialog"][aria-label="Mobile navigation"]');
    assert.ok(navigationDialog);
    assert.equal(dom.window.document.querySelector('[role="menu"][aria-label="Mobile navigation"]'), null);
    assert.equal(navigationDialog.querySelector('[role="menuitem"]'), null);
    const navigation = navigationDialog.querySelector<HTMLElement>('nav[aria-label="Mobile navigation"]');
    assert.ok(navigation);
    const devtoolsGroup = navigation.querySelector<HTMLElement>(
      'section[aria-labelledby="mobile-nav-group-devtools"]',
    );
    assert.ok(devtoolsGroup);
    assert.equal(devtoolsGroup.querySelectorAll('a').length, 7);
    assert.equal(
      devtoolsGroup.querySelector('a[href="/graphs"]')?.getAttribute('aria-current'),
      'page',
    );
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    assert.equal(trigger.getAttribute('aria-controls'), navigationDialog.id);
    const firstLink = navigation.querySelector<HTMLAnchorElement>('a[href]');
    assert.ok(firstLink);
    assert.equal(firstLink.getAttribute('role'), null);
    await waitForFocus(dom, firstLink);

    await act(async () => {
      dom.window.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Escape'}));
      await nextTick();
    });
    assert.equal(dom.window.document.querySelector('[role="dialog"][aria-label="Mobile navigation"]'), null);
    assert.ok(dom.window.document.activeElement === trigger);

    await act(async () => { trigger.click(); await nextTick(); });
    const packsLink = dom.window.document.querySelector<HTMLAnchorElement>('[role="dialog"][aria-label="Mobile navigation"] a[href="/packs"]');
    assert.ok(packsLink);
    packsLink.focus();
    assert.equal(dom.window.document.activeElement, packsLink);
    await act(async () => { packsLink.click(); await nextTick(); });
    assert.equal(dom.window.document.querySelector('[role="dialog"][aria-label="Mobile navigation"]'), null);
  } finally {
    await cleanupSurface(
      surface,
      globalSurfaceSnapshot,
      release,
      () => useAppStore.setState(previousState, true),
    );
  }
});

test('reconfirmation status is explicit and actionable on the shared desktop/mobile header', {concurrency: false}, async () => {
  const release = await acquireDomTestLock();
  const globalSurfaceSnapshot = captureGlobalSurface();
  const previousState = useAppStore.getState();
  let surface: Surface | undefined;
  try {
    surface = createSurface();
    const {container, root} = surface;
    useAppStore.setState({
      runtimeReady: false,
      runtimeStatus: 'profile_reconfirmation_required',
      runtimeError: 'internal diagnostic is intentionally not rendered',
      runtimeDisconnected: false,
    });
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/packs']}>
          <Header />
        </MemoryRouter>,
      );
    });
    const action = container.querySelector<HTMLAnchorElement>('a[href="/setup"]');
    assert.ok(action);
    assert.match(action.textContent ?? '', /Profile reconfirmation required/);
    assert.match(action.getAttribute('aria-label') ?? '', /Open Setup/);
    assert.doesNotMatch(container.textContent ?? '', /Warming up/);
    assert.doesNotMatch(container.textContent ?? '', /internal diagnostic/);
    action.focus();
    assert.equal(container.ownerDocument.activeElement, action);
  } finally {
    await cleanupSurface(surface, globalSurfaceSnapshot, release, () => useAppStore.setState(previousState, true));
  }
});
