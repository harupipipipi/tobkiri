import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {RouteAnnouncer} from './RouteAnnouncer';

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

test('completed route changes announce, title, and focus a non-tabbable route target', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  try {
    await act(async () => {
      root.render(<RouteAnnouncer pathname="/packs" />);
    });
    const target = container.querySelector<HTMLElement>('[role="status"]');
    assert.ok(target);
    assert.equal(target.tabIndex, -1);
    assert.equal(document.title, 'Packs · Tobkiri Launcher');
    assert.equal(document.activeElement, target);

    await act(async () => {
      root.render(<RouteAnnouncer pathname="/ai-input" />);
    });
    assert.equal(document.title, 'AI Input · Tobkiri Launcher');
    assert.equal(document.activeElement, target);
    assert.match(target.textContent ?? '', /AI Input opened/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
    });
  }
});
