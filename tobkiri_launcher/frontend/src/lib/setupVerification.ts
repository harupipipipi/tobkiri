import {
  ApiContractError,
  ApiRequestTimeoutError,
  PanelReauthorizationRequiredError,
} from './api';
import type {RuntimeStatus} from './apiTypes';
import {recordClientDiagnostic} from './clientDiagnostics';
import type {DefaultsSetupState} from './defaultsSetup';
import {
  getBrowserStorage,
  readSafeStorageValue,
  removeSafeStorageValue,
  type SafeStorage,
  writeSafeStorageValue,
} from './safeStorage';

export const SETUP_VERIFICATION_STORAGE_KEY =
  'tobkiri-launcher-setup-verification-v1';
export const SETUP_VERIFICATION_TIMEOUT_MS = 8_000;
const RECORD_VERSION = 'io.tobkiri.launcher.setup-verification.v1';
const DIGEST = /^sha256:[0-9a-f]{64}$/;

export interface SetupVerificationBinding {
  readonly profileRevision: string;
  readonly planDigest: string;
  readonly securityEpoch: number;
}

export interface SetupVerificationRecord {
  readonly record_version: typeof RECORD_VERSION;
  readonly outcome: 'selected' | 'missing';
  readonly verified_at: number;
  readonly profile_revision: string;
  readonly plan_digest: string;
  readonly security_epoch: number;
}

interface RecoverableState {
  readonly cached: SetupVerificationBinding | null;
  readonly diagnosticReference: string;
}

export type SetupVerificationState =
  | {readonly kind: 'unknown'; readonly cached: SetupVerificationBinding | null}
  | {readonly kind: 'loading'; readonly cached: SetupVerificationBinding | null}
  | {
      readonly kind: 'selected';
      readonly source: 'backend' | 'cache';
      readonly binding: SetupVerificationBinding;
      readonly verifiedAt: number;
    }
  | {
      readonly kind: 'missing';
      readonly binding: SetupVerificationBinding;
      readonly verifiedAt: number;
    }
  | ({readonly kind: 'reauth_required'} & RecoverableState)
  | ({readonly kind: 'unavailable'; readonly reason: 'offline' | 'runtime' | 'timeout'}
      & RecoverableState)
  | ({readonly kind: 'malformed'} & RecoverableState)
  | ({readonly kind: 'error'} & RecoverableState);

function isBinding(value: SetupVerificationBinding): boolean {
  return DIGEST.test(value.profileRevision)
    && DIGEST.test(value.planDigest)
    && Number.isSafeInteger(value.securityEpoch)
    && value.securityEpoch > 0;
}

function bindingFromRecord(record: SetupVerificationRecord): SetupVerificationBinding {
  return {
    profileRevision: record.profile_revision,
    planDigest: record.plan_digest,
    securityEpoch: record.security_epoch,
  };
}

function parseRecord(value: unknown): SetupVerificationRecord | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  const expected = [
    'outcome',
    'plan_digest',
    'profile_revision',
    'record_version',
    'security_epoch',
    'verified_at',
  ].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    return null;
  }
  const candidate = record as unknown as SetupVerificationRecord;
  const binding = bindingFromRecord(candidate);
  if (
    candidate.record_version !== RECORD_VERSION
    || (candidate.outcome !== 'selected' && candidate.outcome !== 'missing')
    || !Number.isSafeInteger(candidate.verified_at)
    || candidate.verified_at < 1
    || !isBinding(binding)
  ) return null;
  return candidate;
}

/** Read the last schema-validated verification without treating it as live authority. */
export function readSetupVerificationRecord(
  storage: SafeStorage | null = getBrowserStorage('local'),
): SetupVerificationRecord | null {
  const serialized = readSafeStorageValue(storage, SETUP_VERIFICATION_STORAGE_KEY);
  if (!serialized) return null;
  try {
    const parsed = parseRecord(JSON.parse(serialized));
    if (parsed) return parsed;
  } catch (error) {
    recordClientDiagnostic({
      code: 'setup.verification.cache_malformed',
      operation: 'setup.verification.cache.read',
      error,
    });
  }
  removeSafeStorageValue(storage, SETUP_VERIFICATION_STORAGE_KEY);
  return null;
}

/** Persist a bounded verification receipt; it is continuity evidence, never authority. */
export function writeSetupVerificationRecord(
  state: Extract<SetupVerificationState, {kind: 'selected' | 'missing'}>,
  storage: SafeStorage | null = getBrowserStorage('local'),
): boolean {
  const record: SetupVerificationRecord = {
    record_version: RECORD_VERSION,
    outcome: state.kind,
    verified_at: state.verifiedAt,
    profile_revision: state.binding.profileRevision,
    plan_digest: state.binding.planDigest,
    security_epoch: state.binding.securityEpoch,
  };
  return writeSafeStorageValue(
    storage,
    SETUP_VERIFICATION_STORAGE_KEY,
    JSON.stringify(record),
  );
}

/** Seed the shell from a selected receipt while a fresh authority check is pending. */
export function initialSetupVerificationState(
  setupCompleted: boolean,
  storage: SafeStorage | null = getBrowserStorage('local'),
): SetupVerificationState {
  const record = readSetupVerificationRecord(storage);
  if (setupCompleted && record?.outcome === 'selected') {
    return {
      kind: 'selected',
      source: 'cache',
      binding: bindingFromRecord(record),
      verifiedAt: record.verified_at,
    };
  }
  return {
    kind: 'unknown',
    cached: record?.outcome === 'selected' ? bindingFromRecord(record) : null,
  };
}

function bindingFromSetup(state: DefaultsSetupState): SetupVerificationBinding {
  const confirmation = state.recommended_default_profile.confirmation;
  return {
    profileRevision: confirmation.profile_revision,
    planDigest: confirmation.plan_digest,
    securityEpoch: confirmation.security_epoch,
  };
}

/** Convert one fully parsed Host response into a finite verification outcome. */
export function verifiedSetupState(
  state: DefaultsSetupState,
  verifiedAt = Date.now(),
  previous: SetupVerificationState = {kind: 'unknown', cached: null},
): SetupVerificationState {
  const binding = bindingFromSetup(state);
  if (state.state === 'active') {
    return {kind: 'selected', source: 'backend', binding, verifiedAt};
  }
  if (state.state === 'review_required' && state.denial_diagnostic === null) {
    return {kind: 'missing', binding, verifiedAt};
  }
  const diagnostic = recordClientDiagnostic({
    code: 'setup.verification.authority_denied',
    operation: 'GET /api/setup/packs',
    error: new Error(state.denial_diagnostic ?? 'Authority denied'),
  });
  return {
    kind: 'error',
    cached: cachedBinding(previous),
    diagnosticReference: diagnostic.reference,
  };
}

function cachedBinding(state: SetupVerificationState): SetupVerificationBinding | null {
  if (state.kind === 'selected') return state.binding;
  if (state.kind === 'missing') return null;
  return state.cached;
}

/** Classify a failed check without changing completed setup state. */
export function failedSetupState(
  error: unknown,
  previous: SetupVerificationState,
): SetupVerificationState {
  const cached = cachedBinding(previous);
  const diagnostic = recordClientDiagnostic({
    code: 'setup.verification.request_failed',
    operation: 'GET /api/setup/packs',
    error,
  });
  const recovery = {cached, diagnosticReference: diagnostic.reference};
  if (
    error instanceof PanelReauthorizationRequiredError
    || (error instanceof ApiContractError && error.status === 401)
  ) {
    return {kind: 'reauth_required', ...recovery};
  }
  if (error instanceof ApiRequestTimeoutError || (error instanceof Error
    && (error.name === 'AbortError' || /timed out/i.test(error.message)))) {
    return {kind: 'unavailable', reason: 'timeout', ...recovery};
  }
  if (error instanceof TypeError) {
    return {kind: 'unavailable', reason: 'offline', ...recovery};
  }
  if (error instanceof ApiContractError && error.status >= 500) {
    return {kind: 'unavailable', reason: 'runtime', ...recovery};
  }
  if (error instanceof ApiContractError) {
    return {kind: 'error', ...recovery};
  }
  return {kind: 'malformed', ...recovery};
}

/** Return a loading state while retaining only a previously selected binding. */
export function loadingSetupState(previous: SetupVerificationState): SetupVerificationState {
  return {kind: 'loading', cached: cachedBinding(previous)};
}

/** Monotonic token source that rejects stale concurrent verification responses. */
export class SetupVerificationSequence {
  private generation = 0;

  begin(): number {
    this.generation += 1;
    return this.generation;
  }

  isCurrent(token: number): boolean {
    return token === this.generation;
  }

  cancel(): void {
    this.generation += 1;
  }
}


// --- Runtime route gate vocabulary -----------------------------------------
// The persistent verification record above and the runtime health gate below
// answer different questions: the record tracks Host-owned Defaults Profile
// verification, while the gate resolves which runtime routes may render from
// live health signals. Both stay intentionally separate.

export type SetupVerificationRouteState =
  | 'checking'
  | 'verified'
  | 'needs_setup'
  | 'needs_reconfirm'
  | 'denied';

export interface SetupVerificationInput {
  isSetupDone: boolean;
  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeDisconnected: boolean;
  defaultsBootstrapRequired: boolean;
  hostCatalogVerified?: boolean;
  profileCeremonyAvailable?: boolean;
}

/**
 * Resolve the only states that the panel may use to expose runtime routes.
 *
 * A ready flag is not sufficient by itself: it must agree with the typed
 * health status and a disconnected runtime must fail closed even when a
 * previous healthy value is still in memory.
 */
export function resolveSetupVerificationState(
  input: SetupVerificationInput,
): SetupVerificationRouteState {
  if (input.runtimeDisconnected) return 'denied';
  if (input.runtimeStatus === 'error') return 'denied';
  if (input.defaultsBootstrapRequired) return 'needs_setup';
  if (input.runtimeStatus === 'profile_reconfirmation_required') return 'needs_reconfirm';
  if (input.hostCatalogVerified && input.profileCeremonyAvailable) {
    return 'verified';
  }
  if (!input.isSetupDone) return 'needs_setup';
  if (input.runtimeReady && input.runtimeStatus === 'runtime_ready') return 'verified';
  return 'checking';
}
