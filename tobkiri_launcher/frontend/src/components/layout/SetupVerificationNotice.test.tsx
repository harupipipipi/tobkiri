import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {SetupVerificationNotice} from './SetupVerificationNotice';
import type {SetupVerificationState} from '@/src/lib/setupVerification';

const state: SetupVerificationState = {
  kind: 'reauth_required',
  cached: {
    profileRevision: `sha256:${'a'.repeat(64)}`,
    planDigest: `sha256:${'b'.repeat(64)}`,
    securityEpoch: 1,
  },
  diagnosticReference: 'diag-test',
};

test('recoverable notice exposes keyboard buttons and safe diagnostics', async () => {
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/'});
  Object.defineProperty(globalThis, 'window', {configurable: true, value: dom.window});
  Object.defineProperty(globalThis, 'document', {configurable: true, value: dom.window.document});
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: dom.window.navigator});
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', {
    configurable: true,
    value: true,
  });
  let retries = 0;
  let reauthorizations = 0;
  const container = document.getElementById('root');
  if (!container) throw new Error('missing test root');
  const root = createRoot(container);
  await act(async () => root.render(<SetupVerificationNotice
    state={state}
    onRetry={() => { retries += 1; }}
    onReauthorize={() => { reauthorizations += 1; }}
  />));

  const buttons = [...container.querySelectorAll('button')];
  assert.deepEqual(buttons.map((button) => button.textContent?.trim()), [
    'Reauthorize',
    'Retry',
    'Diagnostics',
  ]);
  await act(async () => buttons[0]?.dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true})));
  await act(async () => buttons[1]?.dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true})));
  await act(async () => buttons[2]?.dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true})));
  assert.equal(reauthorizations, 1);
  assert.equal(retries, 1);
  assert.match(container.textContent ?? '', /diag-test/);
  assert.match(container.textContent ?? '', /sha256:a{64}/);
  assert.equal(buttons[2]?.getAttribute('aria-expanded'), 'true');
  await act(async () => root.unmount());
  dom.window.close();
});

test('offline notice preserves setup and offers retry plus diagnostics', async () => {
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/'});
  Object.defineProperty(globalThis, 'window', {configurable: true, value: dom.window});
  Object.defineProperty(globalThis, 'document', {configurable: true, value: dom.window.document});
  const container = document.getElementById('root');
  if (!container) throw new Error('missing test root');
  const root = createRoot(container);
  await act(async () => root.render(<SetupVerificationNotice
    state={{kind: 'unavailable', reason: 'offline', cached: null, diagnosticReference: 'diag-offline'}}
    onRetry={() => undefined}
    onReauthorize={() => undefined}
  />));
  assert.match(container.textContent ?? '', /offline/);
  assert.match(container.textContent ?? '', /completed setup is preserved/);
  assert.doesNotMatch(container.textContent ?? '', /Reauthorize/);
  await act(async () => root.unmount());
  dom.window.close();
});
