import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {ApiContractError} from '@/src/lib/api';
import {ConfirmationPreDispatchError} from '@/src/lib/dialogConfirmation';
import {MutationResultUnknownError} from '@/src/lib/mutationJournal';
import {type DialogConfig, useAppStore} from '@/src/store';
import {DialogContainer} from './DialogContainer';

interface Surface {
  dom: JSDOM;
  container: HTMLElement;
  root: Root;
  trigger: HTMLButtonElement;
  copied: string[];
}

let activeSurface: Surface | null = null;
let previousState: ReturnType<typeof useAppStore.getState> | null = null;

function buttonWithText(container: ParentNode, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === text,
  );
  assert.ok(button, `button ${text} should be present`);
  return button;
}

async function renderDialog(config: DialogConfig): Promise<Surface> {
  previousState = useAppStore.getState();
  const dom = new JSDOM(
    '<!doctype html><html><body><button id="trigger">Open</button><div id="root"></div></body></html>',
    {url: 'http://localhost/packs'},
  );
  const copied: string[] = [];
  Object.defineProperty(dom.window.navigator, 'clipboard', {
    configurable: true,
    value: {writeText: async (value: string) => { copied.push(value); }},
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  const trigger = dom.window.document.querySelector<HTMLButtonElement>('#trigger');
  assert.ok(container);
  assert.ok(trigger);
  trigger.focus();
  useAppStore.setState({dialog: config});
  const root = createRoot(container);
  await act(async () => root.render(<DialogContainer />));
  await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  activeSurface = {dom, container, root, trigger, copied};
  return activeSurface;
}

function config(onConfirm: DialogConfig['onConfirm']): DialogConfig {
  return {
    title: 'Delete report?',
    message: 'This removes the selected report.',
    objectLabel: 'Quarterly report',
    actionLabel: 'Delete report',
    confirmText: 'Delete',
    cancelText: 'Keep report',
    onConfirm,
  };
}

afterEach(async () => {
  if (activeSurface) {
    await act(async () => activeSurface?.root.unmount());
    activeSurface.dom.window.close();
  }
  if (previousState) useAppStore.setState(previousState, true);
  activeSurface = null;
  previousState = null;
});

test('validation rejection is terminal and keeps object context without leaking details', async () => {
  const secret = 'secret-token-value';
  const surface = await renderDialog(config(async () => {
    throw new ApiContractError(`HTTP 422 invalid choice ${secret}`, {code: 'validation_failed'});
  }));

  await act(async () => buttonWithText(surface.container, 'Delete').click());

  const error = surface.container.querySelector<HTMLElement>('#dialog-error');
  assert.ok(error);
  assert.equal(document.activeElement, error);
  assert.equal(surface.container.querySelectorAll('[role="alert"]').length, 1);
  assert.equal(error.hasAttribute('aria-live'), false);
  assert.match(surface.container.textContent ?? '', /Affected: Quarterly report/);
  assert.match(surface.container.textContent ?? '', /review the selected item or entered choices/i);
  assert.equal(surface.container.textContent?.includes('Retry'), false);
  assert.doesNotMatch(surface.container.textContent ?? '', new RegExp(secret));
  const close = buttonWithText(surface.container, 'Close');
  await act(async () => window.dispatchEvent(new window.KeyboardEvent(
    'keydown',
    {key: 'Tab', shiftKey: true},
  )));
  assert.equal(document.activeElement, close);
});

test('recoverable network failure preserves context, retries once, then closes on success', async () => {
  let calls = 0;
  const surface = await renderDialog(config(async () => {
    calls += 1;
    if (calls === 1) {
      throw new ConfirmationPreDispatchError('Network unavailable before request dispatch');
    }
  }));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.match(surface.container.textContent ?? '', /selection is preserved/i);
  await act(async () => buttonWithText(surface.container, 'Retry').click());

  assert.equal(calls, 2);
  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
});

test('unknown after-commit result forbids blind retry and allows safe diagnostic copy', async () => {
  const surface = await renderDialog(config(async () => {
    throw new MutationResultUnknownError('report:delete:quarterly', crypto.randomUUID());
  }));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.match(surface.container.textContent ?? '', /will not be repeated automatically/i);
  assert.equal(surface.container.textContent?.includes('Retry'), false);
  const technical = surface.container.querySelector('code')?.textContent ?? '';
  assert.match(technical, /^MUTATION_UNKNOWN; diagnostic diag-/);
  await act(async () => buttonWithText(surface.container, 'Copy details').click());
  assert.match(surface.container.textContent ?? '', /Copied/);
  assert.deepEqual(surface.copied, [technical]);
});

test('unclassified network failure is treated as unknown and cannot be retried', async () => {
  const surface = await renderDialog(config(async () => {
    throw new TypeError('Failed to fetch');
  }));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.match(surface.container.textContent ?? '', /could not confirm whether the action completed/i);
  assert.equal(surface.container.textContent?.includes('Retry'), false);
});

test('conflict uses status refresh instead of retry and closes after authoritative refresh', async () => {
  let refreshCount = 0;
  const dialog = config(async () => {
    throw new ApiContractError('HTTP 409 approval already_revoked', {code: 'already_revoked'});
  });
  dialog.onConflict = async () => { refreshCount += 1; };
  const surface = await renderDialog(dialog);

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.equal(surface.container.textContent?.includes('Retry'), false);
  assert.ok(buttonWithText(surface.container, 'Close'));
  await act(async () => buttonWithText(surface.container, 'Refresh status').click());

  assert.equal(refreshCount, 1);
  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
});

test('expired authorization is terminal and Cancel after an error restores trigger focus', async () => {
  const surface = await renderDialog(config(async () => {
    throw new ApiContractError('Session is unavailable', null, 401);
  }));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.match(surface.container.textContent ?? '', /authorization is no longer valid/i);
  assert.equal(surface.container.textContent?.includes('Retry'), false);
  await act(async () => buttonWithText(surface.container, 'Close').click());

  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
  assert.equal(document.activeElement, surface.trigger);
});

test('Cancel after a recoverable error preserves state and restores trigger focus', async () => {
  const surface = await renderDialog(config(async () => {
    throw new ConfirmationPreDispatchError('temporary failure');
  }));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.ok(buttonWithText(surface.container, 'Retry'));
  await act(async () => buttonWithText(surface.container, 'Keep report').click());

  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
  assert.equal(document.activeElement, surface.trigger);
});

test('Escape and backdrop remain blocked only for non-cancellable pending work', async () => {
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const surface = await renderDialog(config(async () => pending));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  await act(async () => window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Escape'})));
  assert.ok(surface.container.querySelector('[role="alertdialog"]'));
  assert.equal(buttonWithText(surface.container, 'Keep report').disabled, true);

  release?.();
  await act(async () => pending);
});

test('read-only conflict refresh stays closable while its status lookup is pending', async () => {
  const statusLookup = new Promise<void>(() => {});
  const dialog = config(async () => {
    throw new ApiContractError('HTTP 409 already settled', {code: 'already_settled'});
  });
  dialog.onConflict = async () => statusLookup;
  const surface = await renderDialog(dialog);

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  await act(async () => buttonWithText(surface.container, 'Refresh status').click());
  assert.equal(buttonWithText(surface.container, 'Close').disabled, false);
  await act(async () => buttonWithText(surface.container, 'Close').click());
  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
});

test('failed conflict lookup retries only the read-only status action', async () => {
  let mutationCount = 0;
  let statusCount = 0;
  const dialog = config(async () => {
    mutationCount += 1;
    throw new ApiContractError('HTTP 409 already settled', {code: 'already_settled'});
  });
  dialog.onConflict = async () => {
    statusCount += 1;
    if (statusCount === 1) throw new TypeError('Failed to fetch status');
  };
  const surface = await renderDialog(dialog);

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  await act(async () => buttonWithText(surface.container, 'Refresh status').click());
  assert.ok(buttonWithText(surface.container, 'Retry status'));
  await act(async () => buttonWithText(surface.container, 'Retry status').click());

  assert.equal(mutationCount, 1);
  assert.equal(statusCount, 2);
  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
});

test('Escape cancels pending work when the caller supplies a cancellation contract', async () => {
  let cancelCount = 0;
  const pendingDialog = config(async () => new Promise<void>(() => {}));
  pendingDialog.pendingCancellation = {
    cancel: async () => { cancelCount += 1; },
  };
  const surface = await renderDialog(pendingDialog);

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  assert.equal(buttonWithText(surface.container, 'Keep report').disabled, false);
  await act(async () => window.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Escape'})));

  assert.equal(cancelCount, 1);
  assert.equal(surface.container.querySelector('[role="alertdialog"]'), null);
});

test('a replaced dialog ignores the stale pending confirmation rejection', async () => {
  let rejectFirst: ((error: Error) => void) | undefined;
  const firstAttempt = new Promise<void>((_resolve, reject) => { rejectFirst = reject; });
  const surface = await renderDialog(config(async () => firstAttempt));

  await act(async () => buttonWithText(surface.container, 'Delete').click());
  await act(async () => useAppStore.getState().showDialog({
    ...config(async () => {}),
    title: 'Archive notes?',
    objectLabel: 'Meeting notes',
    actionLabel: 'Archive notes',
    confirmText: 'Archive',
  }));
  rejectFirst?.(new Error('stale failure'));
  await act(async () => firstAttempt.catch(() => {}));

  assert.match(surface.container.textContent ?? '', /Archive notes/);
  assert.match(surface.container.textContent ?? '', /Affected: Meeting notes/);
  assert.equal(surface.container.querySelector('#dialog-error'), null);
  assert.ok(buttonWithText(surface.container, 'Archive'));
});
