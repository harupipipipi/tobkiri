import assert from 'node:assert/strict';
import test from 'node:test';

import {runtimeBannerIconKind, shouldShowRuntimeErrorCopy} from './Layout';

test('runtime copy action is reserved for actionable danger diagnostics', () => {
  assert.equal(shouldShowRuntimeErrorCopy({tone: 'warning', detail: 'Preparing'}), false);
  assert.equal(shouldShowRuntimeErrorCopy({tone: 'warning', detail: 'Profile reconfirmation required'}), false);
  assert.equal(shouldShowRuntimeErrorCopy({tone: 'danger', detail: '   '}), false);
  assert.equal(shouldShowRuntimeErrorCopy({tone: 'danger', detail: 'Runtime connection failed'}), true);
});

test('runtime banner preserves progress and separates warning and error icons', () => {
  assert.equal(runtimeBannerIconKind('warning', 'starting'), 'progress');
  assert.equal(
    runtimeBannerIconKind('warning', 'profile_reconfirmation_required'),
    'warning',
  );
  assert.equal(runtimeBannerIconKind('danger', 'error'), 'error');
});
