import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {ErrorBoundary} from './ErrorBoundary';

function ThrowingChild(): never {
  throw new Error('The authoritative panel failed to render.');
}

test('ErrorBoundary renders a semantic error icon beside its stable copy action', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousConsoleError = console.error;
  let root: Root | null = null;
  let copied = '';
  try {
    Object.defineProperties(globalThis, {
      window: {value: dom.window, configurable: true},
      document: {value: dom.window.document, configurable: true},
      navigator: {value: dom.window.navigator, configurable: true},
    });
    Object.defineProperty(dom.window.navigator, 'clipboard', {
      configurable: true,
      value: {writeText: async (text: string) => { copied = text; }},
    });
    console.error = () => undefined;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(<ErrorBoundary><ThrowingChild /></ErrorBoundary>);
    });
    assert.match(container.textContent ?? '', /The authoritative panel failed to render\./);
    const errorIcon = container.querySelector('[data-error-icon="rendering"]');
    assert.ok(errorIcon);
    const copy = container.querySelector<HTMLButtonElement>('button[aria-label="Copy rendering error"]');
    assert.ok(copy);
    assert.ok(copy.querySelector('svg.lucide-copy'));
    assert.notEqual(errorIcon, copy.querySelector('svg'));
    await act(async () => {
      copy.click();
      await Promise.resolve();
    });
    assert.equal(copied, 'The authoritative panel failed to render.');
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
