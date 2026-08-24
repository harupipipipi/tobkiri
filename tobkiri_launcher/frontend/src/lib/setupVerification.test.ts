import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {
  ApiContractError,
  ApiRequestTimeoutError,
  PanelReauthorizationRequiredError,
} from './api';
import {parseDefaultsSetupState, type DefaultsSetupState} from './defaultsSetup';
import {
  failedSetupState,
  initialSetupVerificationState,
  readSetupVerificationRecord,
  resolveSetupVerificationState,
  SETUP_VERIFICATION_STORAGE_KEY,
  SetupVerificationSequence,
  verifiedSetupState,
  writeSetupVerificationRecord,
  type SetupVerificationState,
} from './setupVerification';

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

function fixture(state: DefaultsSetupState['state']): DefaultsSetupState {
  const payload = JSON.parse(readFileSync(new URL(
    '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
    import.meta.url,
  ), 'utf8'));
  payload.state = state;
  payload.denial_diagnostic = state === 'activation_denied'
    ? 'Profile revision is stale'
    : null;
  return parseDefaultsSetupState(payload);
}

const cached: SetupVerificationState = {
  kind: 'selected',
  source: 'cache',
  binding: {
    profileRevision: `sha256:${'a'.repeat(64)}`,
    planDigest: `sha256:${'b'.repeat(64)}`,
    securityEpoch: 7,
  },
  verifiedAt: 1,
};

test('only authoritative active and review-required responses select or clear setup', () => {
  const active = verifiedSetupState(fixture('active'), 100, cached);
  assert.equal(active.kind, 'selected');
  assert.equal(active.kind === 'selected' && active.source, 'backend');

  const missing = verifiedSetupState(fixture('review_required'), 101, cached);
  assert.equal(missing.kind, 'missing');

  const denied = verifiedSetupState(fixture('activation_denied'), 102, cached);
  assert.equal(denied.kind, 'error');
  assert.deepEqual(denied.kind === 'error' && denied.cached, cached.binding);

  const reconfirmation = structuredClone(fixture('review_required')) as DefaultsSetupState;
  Object.defineProperty(reconfirmation, 'denial_diagnostic', {
    configurable: true,
    enumerable: true,
    value: 'Profile reconfirmation is required',
  });
  const reconfirmationState = verifiedSetupState(reconfirmation, 103, cached);
  assert.equal(reconfirmationState.kind, 'error');
});

test('offline, slow runtime, 5xx, expired session, and malformed payloads remain distinct', () => {
  assert.deepEqual(
    failedSetupState(new TypeError('network failed'), cached).kind,
    'unavailable',
  );
  const timeout = failedSetupState(
    new ApiRequestTimeoutError('GET', '/api/setup/packs', 8_000),
    cached,
  );
  assert.equal(timeout.kind, 'unavailable');
  assert.equal(timeout.kind === 'unavailable' && timeout.reason, 'timeout');

  const unavailable = failedSetupState(
    new ApiContractError('runtime unavailable', null, 503),
    cached,
  );
  assert.equal(unavailable.kind, 'unavailable');
  assert.equal(unavailable.kind === 'unavailable' && unavailable.reason, 'runtime');

  assert.equal(
    failedSetupState(new ApiContractError('unauthorized', null, 401), cached).kind,
    'reauth_required',
  );
  assert.equal(
    failedSetupState(new PanelReauthorizationRequiredError(), cached).kind,
    'reauth_required',
  );
  assert.equal(failedSetupState(new Error('invalid response'), cached).kind, 'malformed');
});

test('the versioned receipt is revision-bound and never treats cached missing as authority', () => {
  const storage = new MemoryStorage();
  assert.equal(writeSetupVerificationRecord(cached, storage), true);
  const record = readSetupVerificationRecord(storage);
  assert.equal(record?.record_version, 'io.tobkiri.launcher.setup-verification.v1');
  assert.equal(record?.profile_revision, cached.binding.profileRevision);
  assert.equal(initialSetupVerificationState(true, storage).kind, 'selected');

  const missing = verifiedSetupState(fixture('review_required'), 200);
  assert.equal(missing.kind, 'missing');
  if (missing.kind !== 'missing') throw new Error('expected missing fixture');
  writeSetupVerificationRecord(missing, storage);
  assert.equal(initialSetupVerificationState(true, storage).kind, 'unknown');
});

test('malformed or unknown cached fields are removed instead of being trusted', () => {
  const storage = new MemoryStorage();
  storage.setItem(SETUP_VERIFICATION_STORAGE_KEY, JSON.stringify({
    record_version: 'io.tobkiri.launcher.setup-verification.v1',
    outcome: 'selected',
    verified_at: 1,
    profile_revision: `sha256:${'a'.repeat(64)}`,
    plan_digest: `sha256:${'b'.repeat(64)}`,
    security_epoch: 1,
    injected: true,
  }));
  assert.equal(readSetupVerificationRecord(storage), null);
  assert.equal(storage.getItem(SETUP_VERIFICATION_STORAGE_KEY), null);
});

test('concurrent verification responses apply only for the newest request token', () => {
  const sequence = new SetupVerificationSequence();
  const slow = sequence.begin();
  const retry = sequence.begin();
  assert.equal(sequence.isCurrent(slow), false);
  assert.equal(sequence.isCurrent(retry), true);
  sequence.cancel();
  assert.equal(sequence.isCurrent(retry), false);
});

// --- Runtime route gate vocabulary ----------------------------------


const healthy = {
  isSetupDone: true,
  runtimeReady: false,
  runtimeStatus: 'starting' as const,
  runtimeDisconnected: false,
  defaultsBootstrapRequired: false,
};

test('setup verification stays closed during cold start and panel-only readiness', () => {
  assert.equal(resolveSetupVerificationState(healthy), 'checking');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'panel_ready',
  }), 'checking');
});

test('only coherent runtime health opens runtime routes', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeReady: true,
    runtimeStatus: 'runtime_ready',
  }), 'verified');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeReady: true,
    runtimeStatus: 'starting',
  }), 'checking');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'runtime_ready',
  }), 'checking');
});

test('Host catalog verification opens Profile ceremony without launch readiness', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: false,
  }), 'verified');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: false,
  }), 'needs_setup');
});

test('Defaults bootstrap blocks the generic ceremony before existing-profile activation', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: true,
  }), 'needs_setup');

  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
    runtimeStatus: 'panel_ready',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
    defaultsBootstrapRequired: false,
  }), 'verified');
});

test('reconfirmation, errors, disconnects, and incomplete setup fail closed', () => {
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'profile_reconfirmation_required',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
  }), 'needs_reconfirm');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeStatus: 'error',
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
  }), 'denied');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    runtimeReady: true,
    runtimeStatus: 'runtime_ready',
    runtimeDisconnected: true,
    hostCatalogVerified: true,
    profileCeremonyAvailable: true,
  }), 'denied');
  assert.equal(resolveSetupVerificationState({
    ...healthy,
    isSetupDone: false,
  }), 'needs_setup');
});
