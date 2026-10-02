import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';
import {MemoryRouter} from 'react-router';

import {Sidebar} from './Sidebar';
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

const nextTick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test('bottom-left avatar opens Profile/Settings with native links and returns focus on Escape', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createSurface();
  useAppStore.setState({profile: {...previousState.profile, username: 'Sidebar User'}});
  try {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/']}>
          <Sidebar />
        </MemoryRouter>,
      );
    });
    const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="Sidebar User profile and settings"]');
    assert.ok(trigger);
    assert.match(trigger.className, /min-h-11/);
    trigger.focus();
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
    assert.equal(dom.window.document.activeElement, trigger);
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('desktop navigation exposes one labelled Devtools group with selected-state semantics', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createSurface();
  useAppStore.setState({devtoolsEnabled: true, isSidebarOpen: true});
  try {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/graphs']}>
          <Sidebar />
        </MemoryRouter>,
      );
    });
    const devtoolsGroup = container.querySelector<HTMLElement>(
      'li[aria-labelledby="sidebar-group-devtools"]',
    );
    assert.ok(devtoolsGroup);
    assert.equal(
      devtoolsGroup.querySelector('#sidebar-group-devtools')?.textContent,
      'Devtools',
    );
    assert.equal(devtoolsGroup.querySelectorAll('a').length, 7);
    const selected = devtoolsGroup.querySelector<HTMLAnchorElement>(
      'a[href="/graphs"]',
    );
    assert.ok(selected);
    assert.equal(selected.getAttribute('aria-current'), 'page');
    selected.focus();
    assert.equal(dom.window.document.activeElement, selected);

    await act(async () => {
      useAppStore.getState().setDevtoolsEnabled(false);
    });
    assert.equal(
      container.querySelector('li[aria-labelledby="sidebar-group-devtools"]'),
      null,
    );
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});
