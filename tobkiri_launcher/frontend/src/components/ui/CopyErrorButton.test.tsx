import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {CopyErrorButton} from './CopyErrorButton';

test('copies the full error text and announces successful feedback', async () => {
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
      root?.render(<CopyErrorButton label="Copy startup error" text={'first line\nsecond line'} />);
    });
    const button = container.querySelector<HTMLButtonElement>('button');
    assert.ok(button);
    assert.equal(button.getAttribute('aria-label'), 'Copy startup error');
    assert.ok(button.querySelector('svg.lucide-copy'));

    await act(async () => {
      button.click();
      await Promise.resolve();
    });

    assert.equal(copied, 'first line\nsecond line');
    assert.equal(button.getAttribute('aria-label'), 'Error details copied to the clipboard.');
    assert.ok(button.querySelector('svg.lucide-copy'));
    assert.match(container.textContent ?? '', /Copied/);
  } finally {
    await act(async () => root?.unmount());
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('announces a manual-copy fallback when clipboard access is unavailable', async () => {
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
    Object.defineProperty(dom.window.document, 'execCommand', {
      configurable: true,
      value: () => false,
    });
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<CopyErrorButton text="unavailable diagnostic" />);
    });
    const button = container.querySelector<HTMLButtonElement>('button');
    assert.ok(button);

    await act(async () => {
      button.click();
      await Promise.resolve();
    });

    assert.equal(button.getAttribute('aria-label'), 'Could not copy error details. Select the text and copy it manually.');
    assert.ok(button.querySelector('svg.lucide-copy'));
    assert.match(container.textContent ?? '', /Copy failed/);
  } finally {
    await act(async () => root?.unmount());
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('text changes reset feedback and invalidate a late clipboard result', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  let root: Root | null = null;
  let finishFirstCopy: (() => void) | null = null;
  const copied: string[] = [];

  try {
    Object.defineProperties(globalThis, {
      window: {value: dom.window, configurable: true},
      document: {value: dom.window.document, configurable: true},
      navigator: {value: dom.window.navigator, configurable: true},
    });
    Object.defineProperty(dom.window.navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: (text: string) => {
          copied.push(text);
          if (text !== 'old diagnostic') return Promise.resolve();
          return new Promise<void>((resolve) => {
            finishFirstCopy = resolve;
          });
        },
      },
    });
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.querySelector<HTMLElement>('#root');
    assert.ok(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(<CopyErrorButton label="Copy current error" text="old diagnostic" />);
    });
    const button = container.querySelector<HTMLButtonElement>('button');
    assert.ok(button);

    await act(async () => {
      button.click();
      await Promise.resolve();
    });
    assert.ok(finishFirstCopy);

    await act(async () => {
      root?.render(<CopyErrorButton label="Copy current error" text="new diagnostic" />);
    });
    assert.equal(button.getAttribute('aria-label'), 'Copy current error');
    assert.doesNotMatch(container.textContent ?? '', /Copied/);

    await act(async () => {
      finishFirstCopy?.();
      await Promise.resolve();
    });
    assert.equal(button.getAttribute('aria-label'), 'Copy current error');
    assert.doesNotMatch(container.textContent ?? '', /Copied/);

    await act(async () => {
      button.click();
      await Promise.resolve();
    });
    assert.deepEqual(copied, ['old diagnostic', 'new diagnostic']);
    assert.match(container.textContent ?? '', /Copied/);
  } finally {
    await act(async () => root?.unmount());
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
