import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {useAppStore, type Toast, type ToastType} from '@/src/store';
import {ToastContainer} from './ToastContainer';

function toast(id: string, type: ToastType, options: Partial<Toast> = {}): Toast {
  return {
    id,
    message: `${type} message`,
    type,
    dedupeKey: id,
    durationMs: 10_000,
    persistent: true,
    revision: 0,
    ...options,
  };
}

function createSurface(): {
  dom: JSDOM;
  container: HTMLElement;
  root: Root;
  previouslyFocused: HTMLButtonElement;
} {
  const dom = new JSDOM(
    '<!doctype html><html><body><button id="before" onclick="return false">Before</button><div id="root"></div></body></html>',
    {url: 'http://localhost/panel/'},
  );
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  const previouslyFocused = dom.window.document.querySelector<HTMLButtonElement>('#before');
  assert.ok(container);
  assert.ok(previouslyFocused);
  previouslyFocused.focus();
  return {dom, container, root: createRoot(container), previouslyFocused};
}

async function wait(ms: number): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

test('each severity has one non-nested announcement and appearing toasts do not steal focus', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const surface = createSurface();
  useAppStore.setState({
    toasts: [
      toast('success', 'success'),
      toast('info', 'info'),
      toast('warning', 'warning'),
      toast('error', 'error'),
    ],
  });
  try {
    await act(async () => surface.root.render(<ToastContainer />));
    const queue = surface.container.querySelector('section[aria-label="Notifications"]');
    assert.ok(queue);
    assert.equal(queue.hasAttribute('role'), false);
    assert.equal(queue.hasAttribute('aria-live'), false);
    const announcements = [...surface.container.querySelectorAll('[aria-live]')];
    assert.equal(announcements.length, 4);
    assert.equal(surface.container.querySelectorAll('[role="status"]').length, 3);
    assert.equal(surface.container.querySelectorAll('[role="alert"]').length, 1);
    assert.equal(surface.container.querySelector('[role="alert"]')?.getAttribute('aria-live'), 'assertive');
    assert.ok(announcements.every((node) => node.querySelector('[aria-live]') === null));
    assert.match(surface.container.textContent ?? '', /Success: success message/);
    assert.match(surface.container.textContent ?? '', /Information: info message/);
    assert.match(surface.container.textContent ?? '', /Warning: warning message/);
    assert.match(surface.container.textContent ?? '', /Error: error message/);
    assert.equal(surface.dom.window.document.activeElement, surface.previouslyFocused);
    assert.match(
      surface.container.querySelector<HTMLElement>('[data-toast-id="success"]')?.className ?? '',
      /motion-reduce/,
    );
  } finally {
    act(() => surface.root.unmount());
    useAppStore.setState(previousState, true);
    surface.dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('timeout stays paused while hovered or focused and resumes afterward', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const surface = createSurface();
  useAppStore.setState({
    toasts: [toast('timed', 'success', {durationMs: 90, persistent: false})],
  });
  try {
    await act(async () => surface.root.render(<ToastContainer />));
    const card = surface.container.querySelector<HTMLElement>('[data-toast-id="timed"]');
    assert.ok(card);
    act(() => {
      card.dispatchEvent(new surface.dom.window.MouseEvent('mouseover', {bubbles: true}));
    });
    await wait(130);
    assert.ok(surface.container.querySelector('[data-toast-id="timed"]'));
    assert.equal(card.dataset.toastPaused, 'true');
    const dismiss = card.querySelector<HTMLButtonElement>('button');
    assert.ok(dismiss);
    act(() => dismiss.focus());
    act(() => {
      card.dispatchEvent(new surface.dom.window.MouseEvent('mouseout', {bubbles: true}));
    });
    await wait(130);
    assert.ok(surface.container.querySelector('[data-toast-id="timed"]'));
    act(() => surface.previouslyFocused.focus());
    await wait(120);
    assert.equal(surface.container.querySelector('[data-toast-id="timed"]'), null);
  } finally {
    act(() => surface.root.unmount());
    useAppStore.setState(previousState, true);
    surface.dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('keyboard-reachable actions run and dismissal remains explicit', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const surface = createSurface();
  let actionCalls = 0;
  useAppStore.setState({
    toasts: [
      toast('action', 'warning', {
        action: {label: 'Retry', onAction: () => { actionCalls += 1; }},
      }),
      toast('dismiss', 'error'),
    ],
  });
  try {
    await act(async () => surface.root.render(<ToastContainer />));
    const retry = [...surface.container.querySelectorAll('button')]
      .find((button) => button.textContent === 'Retry');
    assert.ok(retry);
    act(() => retry.focus());
    assert.equal(surface.dom.window.document.activeElement, retry);
    await act(async () => {
      retry.click();
      await Promise.resolve();
    });
    assert.equal(actionCalls, 1);
    assert.equal(surface.container.querySelector('[data-toast-id="action"]'), null);
    const dismiss = surface.container.querySelector<HTMLButtonElement>(
      '[aria-label="Dismiss error notification"]',
    );
    assert.ok(dismiss);
    await act(async () => dismiss.click());
    assert.equal(surface.container.querySelector('[data-toast-id="dismiss"]'), null);
  } finally {
    act(() => surface.root.unmount());
    useAppStore.setState(previousState, true);
    surface.dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('failed actions remain paused, announced, and retryable', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const surface = createSurface();
  let actionCalls = 0;
  useAppStore.setState({
    toasts: [
      toast('retry', 'warning', {
        persistent: false,
        durationMs: 80,
        action: {
          label: 'Upload',
          onAction: () => {
            actionCalls += 1;
            if (actionCalls === 1) throw new Error('sensitive backend detail');
          },
        },
      }),
    ],
  });
  try {
    await act(async () => surface.root.render(<ToastContainer />));
    const upload = [...surface.container.querySelectorAll('button')]
      .find((button) => button.textContent === 'Upload');
    assert.ok(upload);
    await act(async () => upload.click());
    const card = surface.container.querySelector<HTMLElement>('[data-toast-id="retry"]');
    assert.ok(card);
    assert.equal(card.dataset.toastPaused, 'true');
    assert.match(card.textContent ?? '', /Action failed\. Try again or dismiss\./);
    assert.doesNotMatch(card.textContent ?? '', /sensitive backend detail/);
    const retry = [...card.querySelectorAll('button')]
      .find((button) => button.textContent === 'Retry Upload');
    assert.ok(retry);
    await wait(120);
    assert.ok(surface.container.querySelector('[data-toast-id="retry"]'));
    await act(async () => retry.click());
    assert.equal(actionCalls, 2);
    assert.equal(surface.container.querySelector('[data-toast-id="retry"]'), null);
  } finally {
    act(() => surface.root.unmount());
    useAppStore.setState(previousState, true);
    surface.dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
