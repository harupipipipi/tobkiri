import assert from 'node:assert/strict';
import test from 'node:test';
import {
  activateDefaultsWithRecovery,
  recoverDefaultsActivation,
} from './defaultsActivationRecovery';
import type {DefaultsSetupState} from './defaultsSetup';

function setupState(state: 'review_required' | 'active'): DefaultsSetupState {
  return {state} as DefaultsSetupState;
}

test('a lost activation response is resolved once from active Host state', async () => {
  let submitCount = 0;
  let fetchCount = 0;
  let reconcileCount = 0;
  const result = await activateDefaultsWithRecovery({
    submitActivation: async () => {
      submitCount += 1;
      throw new Error('response lost after commit');
    },
    fetchAuthoritativeSetup: async () => {
      fetchCount += 1;
      return setupState('active');
    },
    reconcileActiveRuntime: async () => {
      reconcileCount += 1;
    },
  });

  assert.equal(submitCount, 1);
  assert.equal(fetchCount, 1);
  assert.equal(reconcileCount, 1);
  assert.equal(result.state?.state, 'active');
  assert.equal(result.activationCommitted, true);
  assert.equal(result.error, null);
});

test('a post-commit health failure keeps activation committed and never replays POST', async () => {
  let submitCount = 0;
  const result = await activateDefaultsWithRecovery({
    submitActivation: async () => { submitCount += 1; },
    fetchAuthoritativeSetup: async () => setupState('active'),
    reconcileActiveRuntime: async () => { throw new Error('runtime health unavailable'); },
  });

  assert.equal(submitCount, 1);
  assert.equal(result.state?.state, 'active');
  assert.equal(result.activationCommitted, true);
  assert.match(String(result.error), /runtime health unavailable/);
});

test('every post-commit refresh failure remains recoverable without replay', async () => {
  const failures = ['health', 'pack-vm doctor', 'pack projection', 'mounted surface'];
  for (const failure of failures) {
    let submitCount = 0;
    const result = await activateDefaultsWithRecovery({
      submitActivation: async () => { submitCount += 1; },
      fetchAuthoritativeSetup: async () => setupState('active'),
      reconcileActiveRuntime: async () => { throw new Error(`${failure} refresh failed`); },
    });
    assert.equal(submitCount, 1, failure);
    assert.equal(result.state?.state, 'active', failure);
    assert.equal(result.activationCommitted, true, failure);
    assert.match(String(result.error), new RegExp(`${failure} refresh failed`), failure);
  }
});

test('restart recovery reads active authority and has no activation submit path', async () => {
  let fetchCount = 0;
  let reconcileCount = 0;
  const result = await recoverDefaultsActivation({
    fetchAuthoritativeSetup: async () => {
      fetchCount += 1;
      return setupState('active');
    },
    reconcileActiveRuntime: async () => { reconcileCount += 1; },
  });

  assert.equal(fetchCount, 1);
  assert.equal(reconcileCount, 1);
  assert.equal(result.state?.state, 'active');
  assert.equal(result.error, null);
});

test('a rejected replay is not attempted and a review response creates a fresh confirmation state', async () => {
  let submitCount = 0;
  let reconcileCount = 0;
  const result = await activateDefaultsWithRecovery({
    submitActivation: async () => {
      submitCount += 1;
      throw new Error('replay rejected');
    },
    fetchAuthoritativeSetup: async () => setupState('review_required'),
    reconcileActiveRuntime: async () => { reconcileCount += 1; },
  });

  assert.equal(submitCount, 1);
  assert.equal(reconcileCount, 0);
  assert.equal(result.state?.state, 'review_required');
  assert.equal(result.activationCommitted, false);
  assert.match(String(result.error), /replay rejected/);
});

test('verification retry converges after an authoritative GET failure without POST replay', async () => {
  let fetchCount = 0;
  const first = await recoverDefaultsActivation({
    fetchAuthoritativeSetup: async () => {
      fetchCount += 1;
      throw new Error('setup refresh failed');
    },
    reconcileActiveRuntime: async () => undefined,
  });
  assert.equal(first.state, null);
  assert.equal(first.activationCommitted, true);

  const second = await recoverDefaultsActivation({
    fetchAuthoritativeSetup: async () => {
      fetchCount += 1;
      return setupState('active');
    },
    reconcileActiveRuntime: async () => undefined,
  });
  assert.equal(fetchCount, 2);
  assert.equal(second.state?.state, 'active');
  assert.equal(second.error, null);
});

test('persistent integrity drift stays blocked until a later authoritative retry is healthy', async () => {
  let fetchCount = 0;
  let reconcileCount = 0;
  let corrected = false;

  const dependencies = {
    fetchAuthoritativeSetup: async () => {
      fetchCount += 1;
      return setupState('active');
    },
    reconcileActiveRuntime: async () => {
      reconcileCount += 1;
      if (!corrected) {
        throw new Error(
          'DIGEST_MISMATCH: the Profile, Pack v4 lock, and presentation catalog disagree',
        );
      }
    },
  };

  const blocked = await recoverDefaultsActivation(dependencies);
  assert.equal(blocked.state?.state, 'active');
  assert.equal(blocked.activationCommitted, true);
  assert.match(String(blocked.error), /DIGEST_MISMATCH/);
  assert.equal(fetchCount, 1);
  assert.equal(reconcileCount, 1);

  corrected = true;
  const recovered = await recoverDefaultsActivation(dependencies);
  assert.equal(recovered.state?.state, 'active');
  assert.equal(recovered.error, null);
  assert.equal(fetchCount, 2);
  assert.equal(reconcileCount, 2);
});
