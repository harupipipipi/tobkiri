import assert from 'node:assert/strict';
import test from 'node:test';

import {useAppStore} from './store';

test('toast store deduplicates, replaces, updates, and bounds rapid events', () => {
  const previousState = useAppStore.getState();
  try {
    useAppStore.setState({toasts: []});
    const {addToast} = useAppStore.getState();
    addToast('Saved', 'success');
    addToast('Saved', 'success');
    assert.equal(useAppStore.getState().toasts.length, 1);

    addToast('Uploading', 'info', {replacementKey: 'upload'});
    const original = useAppStore.getState().toasts.at(-1);
    assert.ok(original);
    addToast('Upload failed', 'error', {replacementKey: 'upload'});
    const replacement = useAppStore.getState().toasts.at(-1);
    assert.equal(replacement?.id, original.id);
    assert.equal(replacement?.revision, 1);
    assert.equal(replacement?.message, 'Upload failed');

    if (replacement) {
      useAppStore.getState().updateToast(replacement.id, {message: 'Retrying', type: 'warning'});
    }
    assert.equal(useAppStore.getState().toasts.at(-1)?.message, 'Retrying');
    assert.equal(useAppStore.getState().toasts.at(-1)?.revision, 2);

    for (let index = 0; index < 7; index += 1) {
      addToast(`Event ${index}`, 'info');
    }
    const queued = useAppStore.getState().toasts;
    assert.equal(queued.length, 5);
    assert.deepEqual(queued.map((item) => item.message), [
      'Event 2',
      'Event 3',
      'Event 4',
      'Event 5',
      'Event 6',
    ]);
  } finally {
    useAppStore.setState(previousState, true);
  }
});

test('toast store enforces readable action lifetimes and ignores empty messages', () => {
  const previousState = useAppStore.getState();
  try {
    useAppStore.setState({toasts: []});
    const {addToast} = useAppStore.getState();
    addToast('   ', 'warning');
    assert.equal(useAppStore.getState().toasts.length, 0);
    addToast('Can be undone', 'success', {
      durationMs: 100,
      action: {label: 'Undo', onAction: () => undefined},
    });
    assert.equal(useAppStore.getState().toasts[0]?.durationMs, 15_000);
  } finally {
    useAppStore.setState(previousState, true);
  }
});
