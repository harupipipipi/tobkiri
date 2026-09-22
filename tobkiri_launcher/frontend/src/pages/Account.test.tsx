import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';
import {MemoryRouter} from 'react-router';

import {useAppStore} from '@/src/store';
import {Account} from './Account';

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

test('Account localizes its descriptor and primary Launcher-profile controls in Japanese', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  try {
    useAppStore.setState({
      profile: {...previousState.profile, language: 'ja'},
      packs: [],
      packsLoading: false,
      loadPacks: async () => {},
      loadFrontendCatalog: async () => {},
    });
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={['/account']}>
          <Account />
        </MemoryRouter>,
      );
    });
    const html = container.innerHTML;

    assert.match(html, /<h1[^>]*>個人プロフィール<\/h1>/);
    assert.match(html, /個人プロフィール/);
    assert.match(html, /このデバイス/);
    assert.match(html, /aria-label="アバターを選択"/);
    assert.match(html, /職種または役割/);
    assert.doesNotMatch(html, /ランタイム証跡/);
    assert.doesNotMatch(html, /Pack closure/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    useAppStore.setState(previousState, true);
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
