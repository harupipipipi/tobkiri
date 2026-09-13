import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {AdvancedSurfaceFrame} from './AdvancedSurfaceFrame';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';

test('initial loading does not announce empty results before a snapshot arrives', () => {
  for (const status of ['idle', 'loading', 'ready'] as const) {
    const html = renderToStaticMarkup(
      <AdvancedSurfaceFrame
        descriptor={LAUNCHER_ADVANCED_VIEWS.profileFiles}
        state={{status, stale: false, error: null}}
        onRetry={() => {}}
      >
        <p>No finite evidence entries are available</p>
      </AdvancedSurfaceFrame>,
    );
    assert.equal(html.includes('No finite evidence entries are available'), status === 'ready');
    assert.equal(html.includes('Loading the canonical v4 projection'), status !== 'ready');
  }
});

function createSurface(): {dom: JSDOM; container: HTMLElement; root: Root} {
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

test('stale surface frame keeps accepted evidence visible and exposes a retry control', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createSurface();
  let retries = 0;

  try {
    await act(async () => {
      root.render(
        <AdvancedSurfaceFrame
          descriptor={LAUNCHER_ADVANCED_VIEWS.graph}
          state={{
            status: 'stale',
            stale: true,
            error: {code: 'STALE', message: 'The accepted Profile snapshot is stale.'},
          }}
          onRetry={() => { retries += 1; }}
        >
          <p>Accepted Profile evidence</p>
        </AdvancedSurfaceFrame>,
      );
    });

    assert.match(container.textContent ?? '', /Accepted Profile evidence/);
    assert.match(container.textContent ?? '', /Showing the last accepted snapshot/);
    assert.match(container.textContent ?? '', /Actions are disabled until the authoritative surface is fresh/);
    assert.ok(container.querySelector('[role="alert"] [aria-hidden="true"]'));
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent?.includes('Retry'));
    assert.ok(retry);
    await act(async () => { retry.click(); });
    assert.equal(retries, 1);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});
