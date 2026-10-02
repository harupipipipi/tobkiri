import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clearClientDiagnostics,
  listClientDiagnostics,
} from './clientDiagnostics';
import {formatUserFacingError} from './userFacingError';

test('user-facing errors expose a typed code and diagnostic reference, not raw exception text', () => {
  clearClientDiagnostics();
  const message = formatUserFacingError(
    new Error('failed at /Users/haru/private/workspace/secret.txt'),
    'The operation could not be completed.',
    'test.operation',
  );

  assert.match(message, /The operation could not be completed\./);
  assert.match(message, /UNEXPECTED_ERROR/);
  assert.match(message, /diagnostic diag-/);
  assert.doesNotMatch(message, /private\/workspace/);
  assert.deepEqual(listClientDiagnostics()[0]?.operation, 'test.operation');
  clearClientDiagnostics();
});
