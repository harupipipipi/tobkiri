import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {PackDiagnostics} from './PackDiagnostics';

test('PackDiagnostics copies every visible blocking diagnostic field', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
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
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <PackDiagnostics diagnostics={[{
          code: 'production_backend_unavailable',
          severity: 'warning',
          message: 'The signed Host backend is unavailable.',
          owner_pack_id: 'launcher-host',
          contribution_id: 'conversation.complete',
          operation_id: 'conversation.turn',
        }]} />,
      );
    });
    const errorIcon = container.querySelector('[data-diagnostic-icon="error"]');
    const copy = container.querySelector<HTMLButtonElement>('button[aria-label="Copy Pack diagnostic"]');
    assert.ok(errorIcon);
    assert.ok(copy);
    assert.ok(copy.querySelector('svg.lucide-copy'));
    assert.notEqual(errorIcon, copy.querySelector('svg'));
    await act(async () => {
      copy.click();
      await Promise.resolve();
    });
    assert.equal(copied, [
      'production_backend_unavailable',
      'The signed Host backend is unavailable.',
      'Invocation remains unavailable until Tobkiri reports a healthy verified backend.',
      'Owner: launcher-host',
      'Contribution: conversation.complete',
      'Operation: conversation.turn',
    ].join('\n'));
  } finally {
    await act(async () => root?.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('PackDiagnostics does not add a copy action to an informational message', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  let root: Root | null = null;
  try {
    Object.defineProperties(globalThis, {
      window: {value: dom.window, configurable: true},
      document: {value: dom.window.document, configurable: true},
      navigator: {value: dom.window.navigator, configurable: true},
    });
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <PackDiagnostics diagnostics={[{
          code: 'catalog_refreshing',
          severity: 'info',
          message: 'The catalog is refreshing.',
        }]} />,
      );
    });
    assert.ok(container.querySelector('[data-diagnostic-icon="info"]'));
    assert.equal(container.querySelector('button[aria-label="Copy Pack diagnostic"]'), null);
  } finally {
    await act(async () => root?.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
