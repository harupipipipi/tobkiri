import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

import {JSDOM} from 'jsdom';

const splashPath = fileURLToPath(new URL('../../src-tauri/splash/index.html', import.meta.url));
const splashHtml = readFileSync(splashPath, 'utf8');

test('hidden startup actions remain absent from layout before an error', () => {
  assert.match(
    splashHtml,
    /\.startup-error-icon\[hidden\], \.copy-error\[hidden\] \{ display: none; \}/,
  );
});

async function renderSplash({listen, invoke}) {
  const dom = new JSDOM(splashHtml, {
    beforeParse(window) {
      window.setTimeout = () => 0;
      window.__TAURI__ = {
        event: {listen},
        core: {invoke},
      };
    },
    runScripts: 'dangerously',
  });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return dom;
}

test('transient progress failure clears stale error copy when progress recovers', async () => {
  const dom = await renderSplash({
    listen: async () => { throw new Error('transient listener failure'); },
    invoke: async () => 'Checking Python environment...',
  });
  try {
    const progress = dom.window.document.querySelector('#progress');
    const errorIcon = dom.window.document.querySelector('#startup-error-icon');
    const copy = dom.window.document.querySelector('#copy-error');
    const feedback = dom.window.document.querySelector('#copy-feedback');
    assert.equal(progress?.textContent, 'Checking Python environment...');
    assert.equal(progress?.getAttribute('role'), 'status');
    assert.equal(progress?.getAttribute('aria-live'), 'polite');
    assert.equal(errorIcon?.hidden, true);
    assert.equal(copy?.hidden, true);
    assert.equal(feedback?.textContent, '');
  } finally {
    dom.window.close();
  }
});

test('terminal startup error exposes the stable double-square copy action', async () => {
  let progressListener;
  let copied = '';
  const dom = await renderSplash({
    listen: async (_event, listener) => { progressListener = listener; },
    invoke: () => new Promise(() => undefined),
  });
  try {
    Object.defineProperty(dom.window.navigator, 'clipboard', {
      configurable: true,
      value: {writeText: async (text) => { copied = text; }},
    });
    progressListener?.({payload: 'Error: Python setup failed'});
    const progress = dom.window.document.querySelector('#progress');
    const errorIcon = dom.window.document.querySelector('#startup-error-icon');
    const copy = dom.window.document.querySelector('#copy-error');
    assert.equal(progress?.getAttribute('role'), 'alert');
    assert.equal(errorIcon?.hidden, false);
    assert.equal(errorIcon?.querySelectorAll('circle, path').length, 3);
    assert.equal(copy?.hidden, false);
    assert.equal(copy?.querySelectorAll('rect, path').length, 2);
    copy?.click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(copied, 'Error: Python setup failed');
    assert.equal(copy?.querySelectorAll('rect, path').length, 2);
  } finally {
    dom.window.close();
  }
});
