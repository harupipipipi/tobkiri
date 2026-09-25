import assert from 'node:assert/strict';
import test from 'node:test';
import {act, createElement} from 'react';
import {createRoot} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {JSDOM} from 'jsdom';

import {ErrorBoundary} from './ErrorBoundary';
import {
  createSafeCrashDiagnostic,
  saveRecoverableDraft,
} from '@/src/lib/crashRecovery';
import {useAppStore} from '@/src/store';

const rawSecret = 'provider-secret-never-render-this-fragment';
const rawPath = '/Users/private/project/unsafe.ts';

function crashedBoundary(language: 'en' | 'ja', crashCount = 1): ErrorBoundary {
  useAppStore.setState((state) => ({
    profile: {...state.profile, language},
  }));
  const boundary = new ErrorBoundary({children: createElement('div', null, 'healthy')});
  boundary.state = {
    hasError: true,
    diagnostic: createSafeCrashDiagnostic(
      new Error(`private prompt ${rawSecret} at ${rawPath}`),
      '\n    at PrivatePanel (/Users/private/project/unsafe.ts:1:2)',
      '/panel/ai-input?token=secret',
    ),
    diagnosticStatus: 'not_saved',
    copyStatus: 'idle',
    draft: {
      schema: 'tobkiri.launcher.crash_drafts.v1',
      capturedAt: '2026-08-24T00:00:00.000Z',
      drafts: [{
        id: 'operation:aiInput:test',
        label: 'AI Input',
        route: '/panel/ai-input',
        updatedAt: '2026-08-24T00:00:00.000Z',
        fields: {prompt: 'draft content must stay hidden'},
      }],
    },
    crashCount,
  };
  return boundary;
}

test('render crash shows truthful safe copy and distinct recovery actions without raw text', () => {
  const html = renderToStaticMarkup(crashedBoundary('en', 2).render());
  assert.match(html, /This surface stopped rendering/);
  assert.match(html, /does not promise that other unsaved work|recoverable local draft/);
  assert.match(html, /could not be saved on this device/);
  assert.match(html, /Recovery failed again/);
  assert.match(html, /Retry this surface/);
  assert.match(html, /Return Home/);
  assert.match(html, /Reset local UI state/);
  assert.match(html, /Reload the full page/);
  assert.match(html, /Export recoverable drafts/);
  assert.match(html, /Copy diagnostic/);
  assert.match(html, /role="alert"/);
  assert.match(html, /role="status"/);
  assert.match(html, /tabindex="-1"/);
  assert.doesNotMatch(html, new RegExp(rawSecret));
  assert.doesNotMatch(html, /private prompt|Users\/private|draft content must stay hidden/);
});

test('recovery copy is localized in Japanese', () => {
  const html = renderToStaticMarkup(crashedBoundary('ja').render());
  assert.match(html, /\u3053\u306e\u753b\u9762\u306e\u8868\u793a\u304c\u505c\u6b62\u3057\u307e\u3057\u305f/);
  assert.match(html, /\u3053\u306e\u753b\u9762\u3092\u518d\u8a66\u884c/);
  assert.match(html, /\u30db\u30fc\u30e0\u306b\u623b\u308b/);
  assert.doesNotMatch(html, /This surface stopped rendering/);
});

test('the recovery heading receives focus after the fallback replaces the surface', () => {
  const boundary = crashedBoundary('en');
  let focused = false;
  const target = boundary as unknown as {headingRef: {current: {focus: () => void} | null}};
  target.headingRef.current = {focus: () => { focused = true; }};
  boundary.componentDidUpdate(
    boundary.props,
    {...boundary.state, hasError: false},
  );
  assert.equal(focused, true);
});

test('derived crash state does not perform storage side effects during render', () => {
  let writes = 0;
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {getItem: () => null, setItem: () => { writes += 1; }, removeItem: () => {}},
  });
  try {
    const derived = ErrorBoundary.getDerivedStateFromError(new Error(rawSecret));
    assert.equal(derived.hasError, true);
    assert.equal(writes, 0);
  } finally {
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous);
    else Reflect.deleteProperty(globalThis, 'sessionStorage');
  }
});

test('a real render crash preserves an unsaved draft, focuses the heading, and keeps actions keyboard-native', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const previousSessionStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="root"></div></body></html>',
    {url: 'http://localhost/panel/ai-input'},
  );
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root = createRoot(container);
  const originalConsoleError = console.error;
  console.error = () => {};
  const CrashingSurface = () => {
    saveRecoverableDraft({
      id: 'operation:aiInput:test',
      label: 'AI Input test',
      route: '/panel/ai-input',
      fields: {prompt: 'unsaved crash draft'},
    });
    throw new Error(`unsafe ${rawSecret}`);
  };
  try {
    await act(async () => {
      root.render(
        <ErrorBoundary><CrashingSurface /></ErrorBoundary>,
      );
    });
    const heading = container.querySelector<HTMLHeadingElement>('#viewer-recovery-heading');
    assert.ok(heading);
    assert.equal(dom.window.document.activeElement, heading);
    assert.match(container.textContent ?? '', /1 recoverable local draft/);
    assert.doesNotMatch(container.textContent ?? '', /unsaved crash draft|provider-secret-never/);
    const buttons = [...container.querySelectorAll<HTMLButtonElement>('button')];
    assert.equal(buttons.length >= 6, true);
    assert.equal(buttons.every((button) => button.type === 'button'), true);
  } finally {
    console.error = originalConsoleError;
    await act(async () => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
    if (previousLocalStorage) Object.defineProperty(globalThis, 'localStorage', previousLocalStorage);
    else Reflect.deleteProperty(globalThis, 'localStorage');
    if (previousSessionStorage) Object.defineProperty(globalThis, 'sessionStorage', previousSessionStorage);
    else Reflect.deleteProperty(globalThis, 'sessionStorage');
  }
});
