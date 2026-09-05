import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';

import {RouteBoundary} from './RouteBoundary';

function ThrowingRoute(): never {
  throw new Error('The route failed to load.');
}

test('RouteBoundary renders a semantic error icon separate from the copy action', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousConsoleError = console.error;
  let root: Root | null = null;

  try {
    Object.defineProperties(globalThis, {
      window: {value: dom.window, configurable: true},
      document: {value: dom.window.document, configurable: true},
      navigator: {value: dom.window.navigator, configurable: true},
    });
    console.error = () => undefined;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<MemoryRouter><RouteBoundary><ThrowingRoute /></RouteBoundary></MemoryRouter>);
    });

    const errorIcon = container.querySelector('[data-error-icon="route-load"]');
    const copy = container.querySelector<HTMLButtonElement>('button[aria-label="Copy page load error"]');
    assert.ok(errorIcon);
    assert.ok(copy);
    assert.ok(copy.querySelector('svg.lucide-copy'));
    assert.notEqual(errorIcon, copy.querySelector('svg'));
  } finally {
    await act(async () => root?.unmount());
    console.error = previousConsoleError;
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
