import {
  fetchFrontendContractOperation,
} from './api';
import {
  assertVerifiedRuntimeTarget,
  RUNTIME_PROFILE_CEREMONY_TARGETS as GENERATED_PROFILE_CEREMONY_TARGETS,
  RuntimeSurfaceError,
  RUNTIME_SURFACE_API_VERSION,
  validateRuntimeSurfaceEnvelope,
  type RuntimeSurfaceEnvelope,
  type RuntimeSurfaceTarget,
} from './runtimeSurface';

export type ProfileCeremonyStep = 'resolve' | 'review' | 'approve' | 'activate';

export interface ProfileCeremonyTargets {
  resolve?: RuntimeSurfaceTarget;
  review?: RuntimeSurfaceTarget;
  approve?: RuntimeSurfaceTarget;
  activate?: RuntimeSurfaceTarget;
}

/** Profile ceremony targets are generated from the verified frontend map. */
export const RUNTIME_PROFILE_CEREMONY_TARGETS: ProfileCeremonyTargets = GENERATED_PROFILE_CEREMONY_TARGETS;

export interface ProfileResolveInput {
  profile_id: string;
  expected_profile_revision: string;
  expected_plan_digest: string;
  desired_pack_ids: string[];
  profile_definition_digest?: string;
  profile_catalog_digest?: string;
  bundle_lock_digest?: string;
}

export interface ProfileReviewInput {
  candidate_id: string;
  candidate_digest: string;
}

export interface ProfileApproveInput {
  candidate_id: string;
  candidate_digest: string;
}

export interface ProfileActivateInput {
  approval_id: string;
  approval_digest: string;
}

export interface ProfileCatalogBinding {
  profile_definition_digest: string;
  profile_catalog_digest: string;
  bundle_lock_digest: string;
}

export interface ProfileResolveResult {
  state: 'resolved';
  candidate_id: string;
  candidate_digest: string;
  expires_in: number;
  review: {
    profile: unknown;
    profile_lock: unknown;
    resolved_plan: unknown;
    predecessor: unknown;
    catalog_binding?: ProfileCatalogBinding;
  };
  next_action: 'review';
  write_set: unknown[];
}

export interface ProfileReviewResult {
  state: 'reviewed';
  candidate_id: string;
  candidate_digest: string;
  next_action: 'approval';
  write_set: unknown[];
  review?: ProfileResolveResult['review'];
}

export interface ProfileApproveResult {
  state: 'approved';
  approval_id: string;
  approval_digest: string;
  expires_in: number;
  next_action: 'activation';
  write_set: unknown[];
  authority_approval: {
    approval_id: string;
    approval_digest: string;
    decision: string;
    security_epoch: number;
  };
}

export interface ProfileActivateResult {
  state: 'active';
  profile_id: string;
  activation_id: string;
  plan_digest: string;
  security_epoch: number;
  fencing_token: number;
  authoritative_snapshot: RuntimeSurfaceEnvelope<unknown>;
}

export interface ProfileCeremonyErrorData {
  runtime_surface_api_version?: string;
  state: 'error';
  code: 'PROFILE_NOT_ACTIVE' | 'STALE_REVISION' | 'DIGEST_MISMATCH' | 'UNAPPROVED' | 'TIMEOUT' | 'INVALID_REQUEST' | 'API_FAILURE';
  message: string;
  retryable: boolean;
  write_set: unknown[];
}

export interface ProfileCeremonyTransport {
  write<T>(
    target: RuntimeSurfaceTarget,
    payload: Record<string, unknown>,
    requestId?: string,
  ): Promise<T>;
}

const canonicalTransport: ProfileCeremonyTransport = {
  write: <T>(target: RuntimeSurfaceTarget, payload: Record<string, unknown>, requestId?: string) => (
    fetchFrontendContractOperation<T>(target.method, target.logical_target, payload, {requestId})
  ),
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new RuntimeSurfaceError('INVALID', `Profile ceremony response is missing ${field}.`);
  }
  return value;
}

function requiredNumber(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new RuntimeSurfaceError('INVALID', `Profile ceremony response is missing ${field}.`);
  }
  return value;
}

function requiredDigest(value: unknown, field: string): string {
  const digest = requiredString(value, field);
  if (!/^sha256:[0-9a-f]{64}$/.test(digest)) {
    throw new RuntimeSurfaceError('INVALID', `Profile ceremony response has an invalid ${field}.`);
  }
  return digest;
}

function requiredInteger(value: unknown, field: string): number {
  const number = requiredNumber(value, field);
  if (!Number.isInteger(number)) {
    throw new RuntimeSurfaceError('INVALID', `Profile ceremony response is missing ${field}.`);
  }
  return number;
}

function requiredWriteSet(value: unknown): unknown[] {
  if (!Array.isArray(value)) {
    throw new RuntimeSurfaceError('INVALID', 'Profile ceremony response has no write_set.');
  }
  return value;
}

function responseRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new RuntimeSurfaceError('INVALID', 'Profile ceremony response is not an object.');
  }
  if (value.state === 'error') {
    const code = value.code;
    const message = value.message;
    if (
      value.runtime_surface_api_version === RUNTIME_SURFACE_API_VERSION
      && typeof code === 'string'
      && typeof message === 'string'
      && typeof value.retryable === 'boolean'
      && Array.isArray(value.write_set)
    ) {
      const mapped = code === 'STALE_REVISION'
        ? 'STALE'
        : code === 'DIGEST_MISMATCH'
          ? 'DIGEST_MISMATCH'
          : code === 'PROFILE_NOT_ACTIVE'
            ? 'PROFILE_NOT_ACTIVE'
            : code === 'UNAPPROVED'
              ? 'APPROVAL_DENIED'
              : code === 'TIMEOUT'
                ? 'TIMEOUT'
                : code === 'INVALID_REQUEST'
                  ? 'INVALID'
                  : 'FAILED';
      throw new RuntimeSurfaceError(mapped, message);
    }
    throw new RuntimeSurfaceError('FAILED', 'The Profile ceremony failed closed.');
  }
  if (value.runtime_surface_api_version !== RUNTIME_SURFACE_API_VERSION) {
    throw new RuntimeSurfaceError('INVALID', 'Profile ceremony response has an invalid API version.');
  }
  return value;
}

function exactMutationPayload(step: ProfileCeremonyStep, value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new RuntimeSurfaceError('INVALID', `Profile ${step} request is not an object.`);
  }
  const hasCatalogBinding = step === 'resolve'
    && ['profile_definition_digest', 'profile_catalog_digest', 'bundle_lock_digest'].some(
      (key) => Object.prototype.hasOwnProperty.call(value, key),
    );
  const expectedKeys = step === 'resolve'
    ? hasCatalogBinding
      ? [
        'profile_id',
        'expected_profile_revision',
        'expected_plan_digest',
        'desired_pack_ids',
        'profile_definition_digest',
        'profile_catalog_digest',
        'bundle_lock_digest',
      ]
      : ['profile_id', 'expected_profile_revision', 'expected_plan_digest', 'desired_pack_ids']
    : step === 'activate'
      ? ['approval_id', 'approval_digest']
      : ['candidate_id', 'candidate_digest'];
  const keys = Object.keys(value).sort();
  if (keys.length !== expectedKeys.length || keys.some((key, index) => key !== [...expectedKeys].sort()[index])) {
    throw new RuntimeSurfaceError('INVALID', `Profile ${step} request contains an unknown field.`);
  }
  if (step === 'resolve') {
    if (!validRequestString(value.profile_id)
      || !isSha256(value.expected_profile_revision)
      || !isSha256(value.expected_plan_digest)
      || !Array.isArray(value.desired_pack_ids)
      || value.desired_pack_ids.length === 0
      || value.desired_pack_ids.some((item) => !validRequestString(item))
      || (hasCatalogBinding && (
        !isSha256(value.profile_definition_digest)
        || !isSha256(value.profile_catalog_digest)
        || !isSha256(value.bundle_lock_digest)
      ))
      || (!hasCatalogBinding && value.profile_id !== 'defaults')) {
      throw new RuntimeSurfaceError('INVALID', 'Profile resolve request is invalid.');
    }
  } else if (step === 'activate') {
    if (!validRequestString(value.approval_id) || !isSha256(value.approval_digest)) {
      throw new RuntimeSurfaceError('INVALID', 'Profile activation request is invalid.');
    }
  } else if (!validRequestString(value.candidate_id) || !isSha256(value.candidate_digest)) {
    throw new RuntimeSurfaceError('INVALID', `Profile ${step} request is invalid.`);
  }
  return value;
}

function validRequestString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

export function assertProfileCandidateMatches(
  expected: Pick<ProfileReviewInput, 'candidate_id' | 'candidate_digest'>,
  actual: unknown,
): asserts actual is Pick<ProfileReviewResult, 'candidate_id' | 'candidate_digest'> {
  if (!isRecord(actual)) {
    throw new RuntimeSurfaceError('INVALID', 'Profile review returned no candidate identity.');
  }
  const candidateId = requiredString(actual.candidate_id, 'candidate_id');
  const candidateDigest = requiredDigest(actual.candidate_digest, 'candidate_digest');
  if (candidateId !== expected.candidate_id || candidateDigest !== expected.candidate_digest) {
    throw new RuntimeSurfaceError(
      'DIGEST_MISMATCH',
      'Profile review returned a different candidate than the one displayed for approval.',
    );
  }
}

function targetFor(targets: ProfileCeremonyTargets, step: ProfileCeremonyStep): RuntimeSurfaceTarget {
  const target = targets[step];
  if (!target) {
    throw new RuntimeSurfaceError('UNAVAILABLE', 'Profile ceremony is not exposed by the generated Protocol v4 map yet.');
  }
  if (target.method !== 'POST') {
    throw new RuntimeSurfaceError('INVALID', 'Profile ceremony mutation target must be POST.');
  }
  assertVerifiedRuntimeTarget(target);
  return target;
}

export function validateProfileResolveResult(value: unknown): ProfileResolveResult {
  const result = responseRecord(value);
  if (result.state !== 'resolved' || !isRecord(result.review) || result.next_action !== 'review') {
    throw new RuntimeSurfaceError('INVALID', 'Profile resolve returned an invalid ceremony state.');
  }
  const reviewKeys = ['profile', 'profile_lock', 'resolved_plan', 'predecessor'];
  const allowedReviewKeys = [...reviewKeys, 'catalog_binding'];
  if (
    reviewKeys.some((key) => !Object.prototype.hasOwnProperty.call(result.review, key))
    || Object.keys(result.review).some((key) => !allowedReviewKeys.includes(key))
  ) {
    throw new RuntimeSurfaceError('INVALID', 'Profile resolve did not publish the exact candidate review records.');
  }
  let catalogBinding: ProfileCatalogBinding | undefined;
  if (Object.prototype.hasOwnProperty.call(result.review, 'catalog_binding')) {
    const candidate = result.review.catalog_binding;
    if (
      !isRecord(candidate)
      || Object.keys(candidate).length !== 3
      || !isSha256(candidate.profile_definition_digest)
      || !isSha256(candidate.profile_catalog_digest)
      || !isSha256(candidate.bundle_lock_digest)
    ) {
      throw new RuntimeSurfaceError('INVALID', 'Profile resolve returned an invalid catalog binding.');
    }
    catalogBinding = {
      profile_definition_digest: candidate.profile_definition_digest,
      profile_catalog_digest: candidate.profile_catalog_digest,
      bundle_lock_digest: candidate.bundle_lock_digest,
    };
  }
  return {
    state: 'resolved',
    candidate_id: requiredString(result.candidate_id, 'candidate_id'),
    candidate_digest: requiredDigest(result.candidate_digest, 'candidate_digest'),
    expires_in: requiredNumber(result.expires_in, 'expires_in'),
    review: {
      profile: result.review.profile,
      profile_lock: result.review.profile_lock,
      resolved_plan: result.review.resolved_plan,
      predecessor: result.review.predecessor,
      ...(catalogBinding ? {catalog_binding: catalogBinding} : {}),
    },
    next_action: 'review',
    write_set: requiredWriteSet(result.write_set),
  };
}

export function validateProfileReviewResult(
  value: unknown,
  expectedCandidate?: Pick<ProfileReviewInput, 'candidate_id' | 'candidate_digest'>,
): ProfileReviewResult {
  const result = responseRecord(value);
  if (result.state !== 'reviewed' || result.next_action !== 'approval') {
    throw new RuntimeSurfaceError('INVALID', 'Profile review returned an invalid ceremony state.');
  }
  const candidateId = requiredString(result.candidate_id, 'candidate_id');
  const candidateDigest = requiredDigest(result.candidate_digest, 'candidate_digest');
  if (expectedCandidate) {
    assertProfileCandidateMatches(expectedCandidate, {
      candidate_id: candidateId,
      candidate_digest: candidateDigest,
    });
  }
  return {
    state: 'reviewed',
    candidate_id: candidateId,
    candidate_digest: candidateDigest,
    next_action: 'approval',
    write_set: requiredWriteSet(result.write_set),
    ...(isRecord(result.review) ? {review: result.review as ProfileResolveResult['review']} : {}),
  };
}

export function validateProfileApproveResult(value: unknown): ProfileApproveResult {
  const result = responseRecord(value);
  if (result.state !== 'approved' || result.next_action !== 'activation') {
    throw new RuntimeSurfaceError('INVALID', 'Profile approval returned an invalid ceremony state.');
  }
  return {
    state: 'approved',
    approval_id: requiredString(result.approval_id, 'approval_id'),
    approval_digest: requiredDigest(result.approval_digest, 'approval_digest'),
    expires_in: requiredNumber(result.expires_in, 'expires_in'),
    next_action: 'activation',
    write_set: requiredWriteSet(result.write_set),
    authority_approval: (() => {
      if (!isRecord(result.authority_approval)) {
        throw new RuntimeSurfaceError('INVALID', 'Profile approval has no Authority Kernel record.');
      }
      return {
        approval_id: (() => {
          const approvalId = requiredString(result.authority_approval.approval_id, 'authority_approval.approval_id');
          if (approvalId !== result.approval_id) {
            throw new RuntimeSurfaceError('DIGEST_MISMATCH', 'Authority approval is bound to a different approval id.');
          }
          return approvalId;
        })(),
        approval_digest: (() => {
          const approvalDigest = requiredDigest(result.authority_approval.approval_digest, 'authority_approval.approval_digest');
          if (approvalDigest !== result.approval_digest) {
            throw new RuntimeSurfaceError('DIGEST_MISMATCH', 'Authority approval digest does not match the activation credential.');
          }
          return approvalDigest;
        })(),
        decision: (() => {
          const decision = requiredString(result.authority_approval.decision, 'authority_approval.decision');
          if (decision !== 'approved') {
            throw new RuntimeSurfaceError('APPROVAL_DENIED', 'Authority Kernel did not approve this Profile candidate.');
          }
          return decision;
        })(),
        security_epoch: requiredInteger(result.authority_approval.security_epoch, 'authority_approval.security_epoch'),
      };
    })(),
  };
}

export function validateProfileActivateResult(value: unknown): ProfileActivateResult {
  const result = responseRecord(value);
  if (result.state !== 'active') {
    throw new RuntimeSurfaceError('INVALID', 'Profile activation returned an invalid ceremony state.');
  }
  const profileId = requiredString(result.profile_id, 'profile_id');
  const planDigest = requiredDigest(result.plan_digest, 'plan_digest');
  const activationId = requiredString(result.activation_id, 'activation_id');
  const securityEpoch = requiredInteger(result.security_epoch, 'security_epoch');
  const fencingToken = requiredInteger(result.fencing_token, 'fencing_token');
  const authoritativeSnapshot = validateRuntimeSurfaceEnvelope(
    'profile',
    result.authoritative_snapshot,
  );
  const authoritativeData = isRecord(authoritativeSnapshot.data)
    ? authoritativeSnapshot.data
    : null;
  const authoritativeActivationRecord = authoritativeData && isRecord(authoritativeData.activation_record)
    ? authoritativeData.activation_record
    : null;
  if (
    authoritativeSnapshot.profile_id !== profileId
    || authoritativeSnapshot.plan_digest !== planDigest
    || !authoritativeActivationRecord
    || authoritativeActivationRecord.activation_id !== activationId
    || authoritativeActivationRecord.security_epoch !== securityEpoch
    || authoritativeActivationRecord.fencing_token !== fencingToken
  ) {
    throw new RuntimeSurfaceError(
      'DIGEST_MISMATCH',
      'Profile activation metadata does not match the authoritative activation record.',
    );
  }
  return {
    state: 'active',
    profile_id: profileId,
    activation_id: activationId,
    plan_digest: planDigest,
    security_epoch: securityEpoch,
    fencing_token: fencingToken,
    authoritative_snapshot: authoritativeSnapshot,
  };
}

export interface ProfileCeremonyClient {
  resolve(input: ProfileResolveInput, requestId?: string): Promise<ProfileResolveResult>;
  review(input: ProfileReviewInput, requestId?: string): Promise<ProfileReviewResult>;
  approve(input: ProfileApproveInput, requestId?: string): Promise<ProfileApproveResult>;
  activate(input: ProfileActivateInput, requestId?: string): Promise<ProfileActivateResult>;
}

export function createProfileCeremonyClient(
  targets: ProfileCeremonyTargets = RUNTIME_PROFILE_CEREMONY_TARGETS,
  transport: ProfileCeremonyTransport = canonicalTransport,
): ProfileCeremonyClient {
  const write = async <T>(step: ProfileCeremonyStep, payload: Record<string, unknown>, validate: (value: unknown) => T, requestId?: string): Promise<T> => {
    const result = await transport.write<unknown>(targetFor(targets, step), payload, requestId);
    return validate(result);
  };
  return {
    resolve: (input, requestId) => write('resolve', exactMutationPayload('resolve', input), validateProfileResolveResult, requestId),
    review: (input, requestId) => {
      const payload = exactMutationPayload('review', input);
      return transport.write<unknown>(targetFor(targets, 'review'), payload, requestId)
        .then((result) => validateProfileReviewResult(result, input));
    },
    approve: (input, requestId) => write('approve', exactMutationPayload('approve', input), validateProfileApproveResult, requestId),
    activate: (input, requestId) => write('activate', exactMutationPayload('activate', input), validateProfileActivateResult, requestId),
  };
}

export const defaultProfileCeremonyClient = createProfileCeremonyClient();

export interface ProfileCeremonySnapshot {
  profile_id: string;
  profile_revision: string;
  plan_digest: string;
}

export function snapshotForProfileCeremony<T>(
  envelope: RuntimeSurfaceEnvelope<T> | null,
): ProfileCeremonySnapshot | null {
  if (!envelope || envelope.state !== 'ready') return null;
  return {
    profile_id: envelope.profile_id,
    profile_revision: envelope.profile_revision,
    plan_digest: envelope.plan_digest,
  };
}
