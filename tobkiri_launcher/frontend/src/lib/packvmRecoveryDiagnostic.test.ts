import assert from 'node:assert/strict';
import test from 'node:test';
import {
  classifyPackVMRecoveryCode,
  formatPackVMRecoveryError,
} from './packvmLifecycle';

test('PackVM recovery preserves a typed integrity diagnostic without leaking backend detail', () => {
  const error = Object.assign(new Error(
    'private host path /Users/haru/.cache/profile.lock disagrees with catalog',
  ), {
    data: {code: 'DIGEST_MISMATCH', message: 'private backend detail'},
  });

  assert.equal(classifyPackVMRecoveryCode(error), 'DIGEST_MISMATCH');
  assert.equal(
    formatPackVMRecoveryError(error),
    'DIGEST_MISMATCH: Integrity verification failed: the Profile, Pack v4 lock, and presentation catalog do not agree.',
  );
  assert.doesNotMatch(formatPackVMRecoveryError(error), /Users|private backend/);
});

test('PackVM recovery classifies bounded timeout and stale responses', () => {
  assert.equal(
    classifyPackVMRecoveryCode({name: 'ApiRequestTimeoutError'}),
    'TIMEOUT',
  );
  assert.equal(
    classifyPackVMRecoveryCode({data: {code: 'STALE_REVISION'}}),
    'STALE_REVISION',
  );
});

test('a blocked catalog is availability failure, not a lock digest mismatch', () => {
  const error = 'PackVM catalog access is blocked until healthy attestation.';
  assert.equal(classifyPackVMRecoveryCode(error), 'API_FAILURE');
  assert.equal(
    formatPackVMRecoveryError(error),
    'API_FAILURE: PackVM reconciliation could not be verified; retry only after the Host is healthy.',
  );
});
