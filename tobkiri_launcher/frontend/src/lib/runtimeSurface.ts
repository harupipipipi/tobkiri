import {
  ApiRequestTimeoutError,
  fetchFrontendContractOperation,
  invokeFrontendCapability,
  type FrontendContractMethod,
} from './api';
import {
  generatedTargetFor,
  VERIFIED_GENERATED_RUNTIME_TARGETS,
  type VerifiedGeneratedTarget,
} from './generatedFrontendContractMap';

/** Canonical Launcher projection envelope exposed by the generated v4 map. */
export const RUNTIME_SURFACE_API_VERSION =
  'io.tobkiri.launcher.runtime-surface.v4' as const;

export const CANONICAL_RUNTIME_SURFACES = [
  'profile',
  'profiles',
  'settings',
  'packs',
  'contracts',
  'operations',
  'principals',
] as const;

export type RuntimeSurfaceId = typeof CANONICAL_RUNTIME_SURFACES[number];

export type RuntimeSurfaceState = 'ready' | 'stale' | 'blocked';

export interface RuntimeSurfaceRecordRef {
  digest: string;
  source_ref: string;
}

export interface RuntimeActivationRecord {
  activation_api_version: 'io.tobkiri.activation-record.v2';
  profile_id: string;
  profile_revision: string;
  activation_id: string;
  state: 'active';
  state_generation: number;
  catalog_revision: string;
  bundle_digest: string;
  lock_digest: string;
  plan_digest: string;
  closure_digest: string;
  profile_authority_snapshot_digest: string;
  security_epoch: number;
  fencing_token: number;
  created_at: string;
  committed_at?: string;
}

/** Digest/source evidence refs for the captured canonical records. */
export interface RuntimeSurfaceRecords {
  profile_lock: RuntimeSurfaceRecordRef;
  resolved_plan: RuntimeSurfaceRecordRef;
  activation_record: RuntimeSurfaceRecordRef;
  authority_snapshot: RuntimeSurfaceRecordRef;
}

export interface RuntimeProfileSettingsProjection {
  scope: 'runtime_profile';
  mutable_via_profile_activation: true;
  profile_id: string;
  profile_revision: string;
  catalog_revision: string;
  plan_digest: string;
  lock_digest: string;
  security_epoch: number;
}

export interface RuntimeSurfaceEnvelope<T> {
  runtime_surface_api_version: typeof RUNTIME_SURFACE_API_VERSION;
  surface: RuntimeSurfaceId;
  state: RuntimeSurfaceState;
  profile_id: string;
  profile_revision: string;
  catalog_revision: string;
  plan_digest: string;
  records: RuntimeSurfaceRecords;
  data: T;
}

export interface RuntimeProfileCatalogBaseBinding {
  pack_id: string;
  definition_revision: string | null;
  definition_digest: string | null;
  artifact_digest: string | null;
}

export interface RuntimeProfileCatalogShellBinding {
  provider_id: string;
  pack_id: string | null;
  definition_revision: string | null;
  definition_digest: string | null;
  artifact_digest: string | null;
}

export interface RuntimeProfileCatalogApplicationBinding {
  pack_id: string;
  artifact_digest: string | null;
  artifact_ref: string | null;
}

export interface RuntimeProfileCatalogDefinition {
  digest: string;
  ref: string;
  catalog_revision: string | null;
  source_path: string;
  provenance: Record<string, unknown>;
}

export interface RuntimeProfileCatalogPackClosureEntry {
  pack_id: string;
  role: string;
  version: string;
  artifact_digest: string;
  artifact_ref: string;
}

export interface RuntimeProfileCatalogRecords {
  profile_revision: string | null;
  profile_lock_digest: string | null;
  plan_digest: string | null;
}

export interface RuntimeProfileCatalogAuthoritySnapshot {
  state: 'active' | 'captured_on_resolve';
  digest: string | null;
  ref: string | null;
  definition_references: string[];
}

export interface RuntimeProfileCatalogCandidate {
  state: 'not_staged' | string;
  candidate_id: string | null;
  candidate_digest: string | null;
  expires_at: string | null;
}

export interface RuntimeProfileCatalogEntry {
  profile_id: string;
  display_name: string;
  active: boolean;
  lifecycle_state: 'active' | 'available';
  available: boolean;
  diagnostics: Array<{code: string; subject: string}>;
  definition: RuntimeProfileCatalogDefinition;
  bindings: {
    base: RuntimeProfileCatalogBaseBinding;
    shell: RuntimeProfileCatalogShellBinding;
    application: RuntimeProfileCatalogApplicationBinding | null;
  };
  pack_closure: RuntimeProfileCatalogPackClosureEntry[];
  records: RuntimeProfileCatalogRecords;
  authority_snapshot: RuntimeProfileCatalogAuthoritySnapshot;
  candidate: RuntimeProfileCatalogCandidate;
}

export interface RuntimeProfileCatalogProjection {
  catalog_api_version: 'io.tobkiri.profile-catalog-presentation.v4';
  catalog_digest: string;
  bundle_lock_digest: string;
  catalog_ref: string;
  active_profile_id: string;
  count: number;
  profiles: RuntimeProfileCatalogEntry[];
}

export interface RuntimeSurfaceTarget {
  method: FrontendContractMethod;
  logical_target: string;
  contract_id: string;
  operation_id: string;
  contribution_id: string;
  provider_id: string;
  function_id: string;
  allowed_payload_keys: string[];
  map_artifact_digest: string;
  source_ref: string;
  read_guards?: boolean;
}

export interface RuntimePlanBinding {
  binding_id: string;
  source_principal_id: string;
  target_principal_id: string;
  target_contract_id: string;
  operation_id: string;
  owner_pack_id: string;
  edge_digest: string;
  authority_reference: string;
}

export interface RuntimeRequestedEdge {
  caller_function_id: string;
  target_provider_id: string;
  contract_id: string;
  operation_id: string;
  requested_scope_template: Record<string, unknown>;
  authority_reference?: string | null;
}

export interface RuntimeJsonSchema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, RuntimeJsonSchema>;
  required?: string[];
  enum?: unknown[];
  items?: RuntimeJsonSchema;
  default?: unknown;
}

/** The formal v4 action for invoking a declared Contract operation. */
export const RUNTIME_CONTRACT_INVOKE_ACTION = 'contract_invoke' as const;
export type RuntimeOperationAction = typeof RUNTIME_CONTRACT_INVOKE_ACTION;

/** Exact operation metadata published by the v4 operation catalog. */
export interface RuntimeOperationDescriptor {
  action: RuntimeOperationAction;
  operation_id: string;
  contract_id: string;
  owner_pack_id: string;
  contribution_id: string;
  target_provider_id: string;
  artifact_digest: string;
  invocation_contribution_id: string | null;
  invocation_owner_pack_id: string | null;
  invocation_catalog_hash: string | null;
  invocation_reason: string | null;
  invokable: boolean;
  catalog_digest: string;
  function_id: string;
  function_principal_id: string;
  caller_function_id: string;
  authority_reference: string;
  schema: Record<string, unknown>;
  label?: string;
  provider_id?: string;
  input_schema?: RuntimeJsonSchema;
  provider_semantics?: Record<string, unknown> | null;
  route: RuntimeOperationRoute;
}

export interface RuntimeOperationRoute {
  contract_id: string;
  operation_id: string;
  function_id: string;
  provider_pack_id: string;
}

export interface RuntimePackDescriptor {
  pack_id: string;
  role: string;
  kind: string;
  version: string;
  display_name: string;
  artifact_digest: string;
  artifact_ref: string;
  installed: boolean;
  enabled: boolean;
  approved: boolean;
  required: boolean;
  invokable_operations: string[];
  reason?: string | null;
}

/** Exact route metadata published by the v4 contract map projection. */
export interface RuntimeRouteDescriptor {
  route_id: string;
  method: string;
  logical_target: string;
  contract_id: string;
  operation_id: string;
  provider_id: string;
  function_id: string;
  frontend_map_digest: string;
  contribution_id: string;
  presentation: string;
  owner_pack_id: string;
  manifest_digest: string;
  function_principal_id: string;
  allowed_payload_keys: string[];
  security: {
    transport: string;
    panel_authentication_required: boolean;
    broker_authority_required: boolean;
    csrf_required: boolean;
    request_id_required: boolean;
    replay_protection_required: boolean;
  };
}

export interface RuntimeFlowDescriptor {
  flow_id: string;
  label?: string;
  state: string;
  operation_ids: string[];
}

export interface RuntimeArtifactEntry {
  entry_id: string;
  owner_pack_id: string;
  path: string;
  kind: string;
  artifact_digest: string;
}

/**
 * These are the logical targets declared by the digest-pinned frontend map;
 * the physical request still goes through /api/contracts/defaultspack/.
 */
function generatedRuntimeTarget(
  method: FrontendContractMethod,
  logicalTarget: string,
  readGuards?: boolean,
): RuntimeSurfaceTarget {
  const target = generatedTargetFor(
    VERIFIED_GENERATED_RUNTIME_TARGETS,
    method,
    logicalTarget,
  );
  return {...target, ...(readGuards === undefined ? {} : {read_guards: readGuards})};
}

export const RUNTIME_SURFACE_TARGETS: Partial<Record<RuntimeSurfaceId, RuntimeSurfaceTarget>> = {
  profile: generatedRuntimeTarget('GET', '/api/runtime-surface/profile', true),
  profiles: generatedRuntimeTarget('GET', '/api/runtime-surface/profiles', false),
  settings: generatedRuntimeTarget('GET', '/api/runtime-surface/settings', false),
  packs: generatedRuntimeTarget('GET', '/api/runtime-surface/topology/packs', true),
  contracts: generatedRuntimeTarget('GET', '/api/runtime-surface/topology/contracts', true),
  operations: generatedRuntimeTarget('GET', '/api/runtime-surface/topology/operations', true),
  principals: generatedRuntimeTarget('GET', '/api/runtime-surface/topology/principals', true),
};

export const RUNTIME_PROFILE_CEREMONY_TARGETS = {
  resolve: generatedRuntimeTarget('POST', '/api/runtime-surface/profile-change/resolve'),
  review: generatedRuntimeTarget('POST', '/api/runtime-surface/profile-change/review'),
  approve: generatedRuntimeTarget('POST', '/api/runtime-surface/profile-change/approve'),
  activate: generatedRuntimeTarget('POST', '/api/runtime-surface/profile-change/activate'),
};

export interface RuntimeSurfaceTransport {
  read<T>(target: RuntimeSurfaceTarget, input: {
    expected_profile_revision?: string;
    expected_plan_digest?: string;
  }): Promise<T>;
}

const canonicalTransport: RuntimeSurfaceTransport = {
  read: <T>(target: RuntimeSurfaceTarget, input: {
    expected_profile_revision?: string;
    expected_plan_digest?: string;
  }) => (
    assertVerifiedRuntimeTarget(target),
    assertTargetPayload(target, input),
    fetchFrontendContractOperation<T>(target.method, target.logical_target, input)
  ),
};

export type RuntimeSurfaceErrorCode =
  | 'UNAVAILABLE'
  | 'PROFILE_NOT_ACTIVE'
  | 'TIMEOUT'
  | 'STALE'
  | 'DIGEST_MISMATCH'
  | 'APPROVAL_DENIED'
  | 'INVALID'
  | 'FAILED';

export class RuntimeSurfaceError extends Error {
  readonly code: RuntimeSurfaceErrorCode;

  constructor(code: RuntimeSurfaceErrorCode, message: string) {
    super(message);
    this.name = 'RuntimeSurfaceError';
    this.code = code;
  }
}

export function runtimeSurfaceErrorMessage(code: RuntimeSurfaceErrorCode): string {
  switch (code) {
    case 'UNAVAILABLE':
      return 'This surface is not exposed by the generated Protocol v4 map yet.';
    case 'TIMEOUT':
      return 'The Protocol v4 request timed out. No new data was accepted.';
    case 'PROFILE_NOT_ACTIVE':
      return 'The active Profile is unavailable. The UI remains fail-closed.';
    case 'STALE':
    case 'DIGEST_MISMATCH':
      return 'The Profile, Plan, or Catalog digest changed. The accepted snapshot is stale and actions are locked.';
    case 'APPROVAL_DENIED':
      return 'Host approval denied this operation. The UI remains fail-closed.';
    case 'INVALID':
      return 'The runtime surface returned an invalid canonical v4 projection.';
    case 'FAILED':
      return 'The runtime surface could not be loaded. Try again when the runtime is ready.';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isSha256Digest(value: unknown): value is string {
  return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => validString(item));
}

function isDateTime(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && Number.isFinite(Date.parse(value));
}

function parseRuntimeSurfaceRecordRef(value: unknown): RuntimeSurfaceRecordRef | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes('digest') || !keys.includes('source_ref')) {
    return null;
  }
  const digest = value.digest;
  const sourceRef = value.source_ref;
  if (!isSha256Digest(digest) || !validString(sourceRef)) return null;
  if (
    /^(?:file|https?):/i.test(sourceRef)
    || sourceRef.startsWith('/')
    || /^[A-Za-z]:[\\/]/.test(sourceRef)
    || sourceRef.includes('\\')
    || sourceRef.includes('\0')
    || !/^[a-z][a-z0-9+.-]*:\/\//i.test(sourceRef)
  ) {
    return null;
  }
  return {digest, source_ref: sourceRef};
}

function parseRuntimeSurfaceRecords(value: unknown): RuntimeSurfaceRecords | null {
  if (!isRecord(value)) return null;
  const recordKeys = [
    'profile_lock',
    'resolved_plan',
    'activation_record',
    'authority_snapshot',
  ];
  if (
    Object.keys(value).length !== recordKeys.length
    || recordKeys.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
  ) {
    return null;
  }
  const profileLock = parseRuntimeSurfaceRecordRef(value.profile_lock);
  const resolvedPlan = parseRuntimeSurfaceRecordRef(value.resolved_plan);
  const activationRecord = parseRuntimeSurfaceRecordRef(value.activation_record);
  const authoritySnapshot = parseRuntimeSurfaceRecordRef(value.authority_snapshot);
  if (!profileLock || !resolvedPlan || !activationRecord || !authoritySnapshot) return null;
  return {
    profile_lock: profileLock,
    resolved_plan: resolvedPlan,
    activation_record: activationRecord,
    authority_snapshot: authoritySnapshot,
  };
}

function parseRuntimeActivationRecord(
  value: unknown,
  profileId: string,
  profileRevision: string,
  catalogRevision: string,
  profileLockDigest: string,
  planDigest: string,
  authoritySnapshotDigest: string,
  expectedBundleDigest: unknown,
  expectedClosureDigest: unknown,
  expectedSecurityEpoch: unknown,
  expectedFencingToken: unknown,
): RuntimeActivationRecord | null {
  if (!isRecord(value)) return null;
  const activationApiVersion = value.activation_api_version;
  const recordProfileId = value.profile_id;
  const recordProfileRevision = value.profile_revision;
  const activationId = value.activation_id;
  const state = value.state;
  const stateGeneration = value.state_generation;
  const recordCatalogRevision = value.catalog_revision;
  const bundleDigest = value.bundle_digest;
  const lockDigest = value.lock_digest;
  const recordPlanDigest = value.plan_digest;
  const closureDigest = value.closure_digest;
  const recordAuthoritySnapshotDigest = value.profile_authority_snapshot_digest;
  const securityEpoch = value.security_epoch;
  const fencingToken = value.fencing_token;
  const createdAt = value.created_at;
  const committedAt = value.committed_at;
  const requiredKeys = [
    'activation_api_version',
    'profile_id',
    'profile_revision',
    'activation_id',
    'state',
    'state_generation',
    'catalog_revision',
    'bundle_digest',
    'lock_digest',
    'plan_digest',
    'closure_digest',
    'profile_authority_snapshot_digest',
    'security_epoch',
    'fencing_token',
    'created_at',
  ];
  const hasCommittedAt = Object.prototype.hasOwnProperty.call(value, 'committed_at');
  if (
    Object.keys(value).length !== requiredKeys.length + (hasCommittedAt ? 1 : 0)
    || requiredKeys.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
    || activationApiVersion !== 'io.tobkiri.activation-record.v2'
    || recordProfileId !== profileId
    || recordProfileRevision !== profileRevision
    || !validString(activationId)
    || !/^activation:[a-z0-9][a-z0-9._-]{7,127}$/.test(activationId)
    || state !== 'active'
    || typeof stateGeneration !== 'number'
    || !Number.isInteger(stateGeneration)
    || stateGeneration < 0
    || recordCatalogRevision !== catalogRevision
    || !isSha256Digest(bundleDigest)
    || bundleDigest !== expectedBundleDigest
    || lockDigest !== profileLockDigest
    || recordPlanDigest !== planDigest
    || !isSha256Digest(closureDigest)
    || closureDigest !== expectedClosureDigest
    || recordAuthoritySnapshotDigest !== authoritySnapshotDigest
    || typeof securityEpoch !== 'number'
    || !Number.isInteger(securityEpoch)
    || securityEpoch < 0
    || securityEpoch !== expectedSecurityEpoch
    || typeof fencingToken !== 'number'
    || !Number.isInteger(fencingToken)
    || fencingToken < 0
    || fencingToken !== expectedFencingToken
    || !isDateTime(createdAt)
  ) {
    return null;
  }
  let committedAtValue: string | undefined;
  if (hasCommittedAt) {
    if (!isDateTime(committedAt)) return null;
    committedAtValue = committedAt;
  }
  return {
    activation_api_version: activationApiVersion,
    profile_id: recordProfileId,
    profile_revision: recordProfileRevision,
    activation_id: activationId,
    state: 'active',
    state_generation: stateGeneration,
    catalog_revision: recordCatalogRevision,
    bundle_digest: bundleDigest,
    lock_digest: lockDigest,
    plan_digest: recordPlanDigest,
    closure_digest: closureDigest,
    profile_authority_snapshot_digest: recordAuthoritySnapshotDigest,
    security_epoch: securityEpoch,
    fencing_token: fencingToken,
    created_at: createdAt,
    ...(committedAtValue === undefined ? {} : {committed_at: committedAtValue}),
  };
}

function parseRuntimeOperationRoute(
  value: unknown,
  operation: Pick<RuntimeOperationDescriptor, 'contract_id' | 'operation_id' | 'function_id' | 'owner_pack_id'>,
): RuntimeOperationRoute | null {
  if (!isRecord(value)) return null;
  const keys = ['contract_id', 'operation_id', 'function_id', 'provider_pack_id'];
  if (
    Object.keys(value).length !== keys.length
    || keys.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
    || !validString(value.contract_id)
    || !validString(value.operation_id)
    || !validString(value.function_id)
    || !validString(value.provider_pack_id)
    || value.contract_id !== operation.contract_id
    || value.operation_id !== operation.operation_id
    || value.function_id !== operation.function_id
    || value.provider_pack_id !== operation.owner_pack_id
  ) {
    return null;
  }
  return {
    contract_id: value.contract_id,
    operation_id: value.operation_id,
    function_id: value.function_id,
    provider_pack_id: value.provider_pack_id,
  };
}

export function assertVerifiedRuntimeTarget(target: RuntimeSurfaceTarget): void {
  let expected: VerifiedGeneratedTarget;
  try {
    expected = generatedTargetFor(
      VERIFIED_GENERATED_RUNTIME_TARGETS,
      target.method,
      target.logical_target,
    );
  } catch {
    throw new RuntimeSurfaceError(
      'UNAVAILABLE',
      'The requested runtime surface target is not declared by the verified frontend Contract Map.',
    );
  }
  if (
    target.contract_id !== expected.contract_id
    || target.operation_id !== expected.operation_id
    || target.contribution_id !== expected.contribution_id
    || target.provider_id !== expected.provider_id
    || target.function_id !== expected.function_id
    || target.map_artifact_digest !== expected.map_artifact_digest
    || target.source_ref !== expected.source_ref
    || target.allowed_payload_keys.length !== expected.allowed_payload_keys.length
    || target.allowed_payload_keys.some((key, index) => key !== expected.allowed_payload_keys[index])
  ) {
    throw new RuntimeSurfaceError(
      'DIGEST_MISMATCH',
      'The runtime surface target does not match the verified frontend Contract Map.',
    );
  }
}

function assertTargetPayload(
  target: RuntimeSurfaceTarget,
  input: Record<string, unknown>,
): void {
  if (Object.keys(input).some((key) => !target.allowed_payload_keys.includes(key))) {
    throw new RuntimeSurfaceError(
      'INVALID',
      'The runtime surface request contains a key not allowed by its generated Contract Map target.',
    );
  }
}

export function validateRuntimeSurfaceEnvelope<T>(
  expectedSurface: RuntimeSurfaceId,
  value: unknown,
): RuntimeSurfaceEnvelope<T> {
  if (!isRecord(value)) {
    throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
  }
  if (value.runtime_surface_api_version === RUNTIME_SURFACE_API_VERSION && value.state === 'error') {
    const errorKeys = ['runtime_surface_api_version', 'state', 'code', 'message', 'retryable', 'write_set'];
    if (
      Object.keys(value).length !== errorKeys.length
      || errorKeys.some((key) => !Object.prototype.hasOwnProperty.call(value, key))
      || typeof value.code !== 'string'
      || typeof value.message !== 'string'
      || typeof value.retryable !== 'boolean'
      || !Array.isArray(value.write_set)
    ) {
      throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
    }
    const code = value.code;
    const message = typeof value.message === 'string' ? value.message : 'Canonical runtime surface failed closed.';
    const mapped = code === 'PROFILE_NOT_ACTIVE'
      ? 'PROFILE_NOT_ACTIVE'
      : code === 'STALE_REVISION'
        ? 'STALE'
        : code === 'DIGEST_MISMATCH'
          ? 'DIGEST_MISMATCH'
          : code === 'UNAPPROVED'
            ? 'APPROVAL_DENIED'
            : code === 'TIMEOUT'
              ? 'TIMEOUT'
              : code === 'INVALID_REQUEST'
                ? 'INVALID'
                : 'FAILED';
    throw new RuntimeSurfaceError(mapped, message);
  }
  if (
    Object.keys(value).length !== 9
    || ![
      'runtime_surface_api_version',
      'surface',
      'state',
      'profile_id',
      'profile_revision',
      'plan_digest',
      'catalog_revision',
      'records',
      'data',
    ].every((key) => Object.prototype.hasOwnProperty.call(value, key))
    || value.runtime_surface_api_version !== RUNTIME_SURFACE_API_VERSION
    || value.surface !== expectedSurface
    || (value.state !== 'ready' && value.state !== 'stale' && value.state !== 'blocked')
  ) {
    throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
  }
  const profileId = value.profile_id;
  const profileRevision = value.profile_revision;
  const catalogRevision = value.catalog_revision;
  const planDigest = value.plan_digest;
  const acceptedRecords = parseRuntimeSurfaceRecords(value.records);
  if (
    typeof profileId !== 'string'
    || !profileId
    || typeof profileRevision !== 'string'
    || !profileRevision
    || typeof catalogRevision !== 'string'
    || !catalogRevision
    || typeof planDigest !== 'string'
    || !planDigest
    || !isSha256Digest(profileRevision)
    || !isSha256Digest(planDigest)
    || !isSha256Digest(catalogRevision)
    || !acceptedRecords
  ) {
    throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
  }
  if (expectedSurface === 'profile') {
    const profileData = isRecord(value.data) ? value.data : null;
    const profileSummary = profileData && isRecord(profileData.profile)
      ? profileData.profile
      : null;
    const resolvedPlan = profileData && isRecord(profileData.resolved_plan)
      ? profileData.resolved_plan
      : null;
    const profileLock = profileData && isRecord(profileData.profile_lock)
      ? profileData.profile_lock
      : null;
    const authoritySnapshot = profileData && isRecord(profileData.authority_snapshot)
      ? profileData.authority_snapshot
      : null;
    const activationRecord = profileData
      ? parseRuntimeActivationRecord(
        profileData.activation_record,
        profileId,
        profileRevision,
        catalogRevision,
        acceptedRecords.profile_lock.digest,
        planDigest,
        acceptedRecords.authority_snapshot.digest,
        resolvedPlan?.bundle_digest,
        resolvedPlan?.closure_digest,
        authoritySnapshot?.security_epoch,
        authoritySnapshot?.fencing_token,
      )
      : null;
    if (
      !profileSummary
      || profileSummary.profile_id !== profileId
      || profileSummary.profile_revision !== profileRevision
      || profileSummary.catalog_revision !== catalogRevision
      || !resolvedPlan
      || resolvedPlan.plan_digest !== planDigest
      || acceptedRecords.resolved_plan.digest !== planDigest
      || !isSha256Digest(resolvedPlan.bundle_digest)
      || !isSha256Digest(resolvedPlan.closure_digest)
      || !profileLock
      || profileLock.lock_digest !== acceptedRecords.profile_lock.digest
      || profileLock.plan_digest !== planDigest
      || profileLock.bundle_digest !== resolvedPlan.bundle_digest
      || profileLock.closure_digest !== resolvedPlan.closure_digest
      || profileLock.security_epoch !== authoritySnapshot?.security_epoch
      || profileLock.profile_authority_snapshot_digest !== acceptedRecords.authority_snapshot.digest
      || !authoritySnapshot
      || authoritySnapshot.profile_authority_snapshot_digest !== acceptedRecords.authority_snapshot.digest
      || !activationRecord
    ) {
      throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
    }
  }
  if (expectedSurface === 'profiles') {
    const profileCatalog = extractExactProfileCatalog(value.data);
    const activeEntry = profileCatalog?.profiles.find((entry) => entry.active) ?? null;
    if (
      !profileCatalog
      || profileCatalog.active_profile_id !== profileId
      || (profileCatalog.profiles.length > 0 && (
        !activeEntry
        || activeEntry.profile_id !== profileId
        || activeEntry.records.profile_revision !== profileRevision
        || activeEntry.records.profile_lock_digest !== acceptedRecords.profile_lock.digest
        || activeEntry.records.plan_digest !== planDigest
        || activeEntry.authority_snapshot.digest !== acceptedRecords.authority_snapshot.digest
      ))
    ) {
      throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
    }
  }
  if (expectedSurface === 'settings') {
    const runtimeSettings = extractRuntimeProfileSettings(value.data);
    if (
      runtimeSettings === null
      || runtimeSettings.profile_id !== profileId
      || runtimeSettings.profile_revision !== profileRevision
      || runtimeSettings.catalog_revision !== catalogRevision
      || runtimeSettings.plan_digest !== planDigest
      || runtimeSettings.lock_digest !== acceptedRecords.profile_lock.digest
    ) {
      throw new RuntimeSurfaceError('INVALID', runtimeSurfaceErrorMessage('INVALID'));
    }
  }
  if (value.state === 'stale') {
    throw new RuntimeSurfaceError('STALE', runtimeSurfaceErrorMessage('STALE'));
  }
  if (value.state === 'blocked') {
    throw new RuntimeSurfaceError('APPROVAL_DENIED', runtimeSurfaceErrorMessage('APPROVAL_DENIED'));
  }
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: expectedSurface,
    state: 'ready',
    profile_id: profileId,
    profile_revision: profileRevision,
    catalog_revision: catalogRevision,
    plan_digest: planDigest,
    records: acceptedRecords,
    data: value.data as T,
  };
}

function validString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isOptionalString(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || validString(value);
}

/** Read only the runtime scope; Launcher user preferences are intentionally ignored. */
export function extractRuntimeProfileSettings(value: unknown): RuntimeProfileSettingsProjection | null {
  if (!isRecord(value) || !isRecord(value.runtime_profile_settings)) return null;
  const settings = value.runtime_profile_settings;
  const expectedKeys = [
    'scope',
    'mutable_via_profile_activation',
    'profile_id',
    'profile_revision',
    'catalog_revision',
    'plan_digest',
    'lock_digest',
    'security_epoch',
  ];
  if (
    Object.keys(settings).length !== expectedKeys.length
    || expectedKeys.some((key) => !Object.prototype.hasOwnProperty.call(settings, key))
    || settings.scope !== 'runtime_profile'
    || settings.mutable_via_profile_activation !== true
    || !validString(settings.profile_id)
    || !isSha256Digest(settings.profile_revision)
    || !isSha256Digest(settings.catalog_revision)
    || !isSha256Digest(settings.plan_digest)
    || !isSha256Digest(settings.lock_digest)
    || typeof settings.security_epoch !== 'number'
    || !Number.isInteger(settings.security_epoch)
    || settings.security_epoch < 0
  ) {
    return null;
  }
  return {
    scope: 'runtime_profile',
    mutable_via_profile_activation: true,
    profile_id: settings.profile_id,
    profile_revision: settings.profile_revision,
    catalog_revision: settings.catalog_revision,
    plan_digest: settings.plan_digest,
    lock_digest: settings.lock_digest,
    security_epoch: settings.security_epoch,
  };
}

function exactObject(value: unknown, keys: string[]): value is Record<string, unknown> {
  return isRecord(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function nullableDigest(value: unknown): string | null {
  return value === null ? null : isSha256Digest(value) ? value : null;
}

function canonicalReference(value: unknown): value is string {
  return validString(value)
    && !/^(?:file|https?):/i.test(value)
    && !value.startsWith('/')
    && !/^[A-Za-z]:[\\/]/.test(value)
    && !value.includes('\\')
    && !value.includes('\0')
    && !value.includes('..')
    && /^[a-z][a-z0-9+.-]*:\/\/[^\s]+$/i.test(value);
}

function relativeSourcePath(value: unknown): value is string {
  return validString(value)
    && !value.startsWith('/')
    && !/^[A-Za-z]:[\\/]/.test(value)
    && !value.includes('\\')
    && !value.includes('\0')
    && !value.split('/').some((part) => part === '.' || part === '..');
}

function nullableDateTime(value: unknown): string | null {
  return value === null ? null : isDateTime(value) ? value : null;
}

function profileCatalogBinding(value: unknown): RuntimeProfileCatalogBaseBinding | null {
  if (!exactObject(value, ['pack_id', 'definition_revision', 'definition_digest', 'artifact_digest'])) {
    return null;
  }
  const definitionRevision = nullableDigest(value.definition_revision);
  const definitionDigest = nullableDigest(value.definition_digest);
  const artifactDigest = nullableDigest(value.artifact_digest);
  if (
    !validString(value.pack_id)
    || (value.definition_revision !== null && definitionRevision === null)
    || (value.definition_digest !== null && definitionDigest === null)
    || (value.artifact_digest !== null && artifactDigest === null)
  ) {
    return null;
  }
  return {
    pack_id: value.pack_id,
    definition_revision: definitionRevision,
    definition_digest: definitionDigest,
    artifact_digest: artifactDigest,
  };
}

function profileCatalogShellBinding(value: unknown): RuntimeProfileCatalogShellBinding | null {
  if (!exactObject(value, ['provider_id', 'pack_id', 'definition_revision', 'definition_digest', 'artifact_digest'])) {
    return null;
  }
  const definitionRevision = nullableDigest(value.definition_revision);
  const definitionDigest = nullableDigest(value.definition_digest);
  const artifactDigest = nullableDigest(value.artifact_digest);
  if (
    !validString(value.provider_id)
    || (value.pack_id !== null && !validString(value.pack_id))
    || (value.definition_revision !== null && definitionRevision === null)
    || (value.definition_digest !== null && definitionDigest === null)
    || (value.artifact_digest !== null && artifactDigest === null)
  ) {
    return null;
  }
  return {
    provider_id: value.provider_id,
    pack_id: value.pack_id as string | null,
    definition_revision: definitionRevision,
    definition_digest: definitionDigest,
    artifact_digest: artifactDigest,
  };
}

function profileCatalogApplicationBinding(value: unknown): RuntimeProfileCatalogApplicationBinding | null {
  if (!exactObject(value, ['pack_id', 'artifact_digest', 'artifact_ref'])) return null;
  const artifactDigest = nullableDigest(value.artifact_digest);
  if (
    !validString(value.pack_id)
    || (value.artifact_digest !== null && artifactDigest === null)
    || (value.artifact_ref !== null && !canonicalReference(value.artifact_ref))
  ) {
    return null;
  }
  return {
    pack_id: value.pack_id,
    artifact_digest: artifactDigest,
    artifact_ref: value.artifact_ref as string | null,
  };
}

function parseProfileCatalogEntry(value: unknown): RuntimeProfileCatalogEntry | null {
  if (!exactObject(value, [
    'profile_id',
    'display_name',
    'active',
    'lifecycle_state',
    'available',
    'diagnostics',
    'definition',
    'bindings',
    'pack_closure',
    'records',
    'authority_snapshot',
    'candidate',
  ])) {
    return null;
  }
  if (
    !validString(value.profile_id)
    || !validString(value.display_name)
    || typeof value.active !== 'boolean'
    || (value.lifecycle_state !== 'active' && value.lifecycle_state !== 'available')
    || typeof value.available !== 'boolean'
    || !Array.isArray(value.diagnostics)
    || !exactObject(value.definition, ['digest', 'ref', 'catalog_revision', 'source_path', 'provenance'])
    || !exactObject(value.bindings, ['base', 'shell', 'application'])
    || !Array.isArray(value.pack_closure)
    || !exactObject(value.records, ['profile_revision', 'profile_lock_digest', 'plan_digest'])
    || !exactObject(value.authority_snapshot, ['state', 'digest', 'ref', 'definition_references'])
    || !exactObject(value.candidate, ['state', 'candidate_id', 'candidate_digest', 'expires_at'])
  ) {
    return null;
  }
  const diagnostics: Array<{code: string; subject: string}> = [];
  for (const item of value.diagnostics) {
    if (!exactObject(item, ['code', 'subject']) || !validString(item.code) || !validString(item.subject)) {
      return null;
    }
    diagnostics.push({code: item.code, subject: item.subject});
  }
  const catalogRevision = nullableDigest(value.definition.catalog_revision);
  if (
    !isSha256Digest(value.definition.digest)
    || !canonicalReference(value.definition.ref)
    || value.definition.ref !== `profile-v4://${value.profile_id}/${value.definition.digest}`
    || (value.definition.catalog_revision !== null && catalogRevision === null)
    || !relativeSourcePath(value.definition.source_path)
    || !isRecord(value.definition.provenance)
  ) {
    return null;
  }
  const base = profileCatalogBinding(value.bindings.base);
  const shell = profileCatalogShellBinding(value.bindings.shell);
  const application = value.bindings.application === null
    ? null
    : profileCatalogApplicationBinding(value.bindings.application);
  if (!base || !shell || (value.bindings.application !== null && !application)) return null;

  const profileRevision = nullableDigest(value.records.profile_revision);
  const profileLockDigest = nullableDigest(value.records.profile_lock_digest);
  const planDigest = nullableDigest(value.records.plan_digest);
  if (
    (value.records.profile_revision !== null && profileRevision === null)
    || (value.records.profile_lock_digest !== null && profileLockDigest === null)
    || (value.records.plan_digest !== null && planDigest === null)
  ) {
    return null;
  }

  const closure: RuntimeProfileCatalogPackClosureEntry[] = [];
  const closureIds = new Set<string>();
  for (const item of value.pack_closure) {
    if (
      !exactObject(item, ['pack_id', 'role', 'version', 'artifact_digest', 'artifact_ref'])
      || !validString(item.pack_id)
      || closureIds.has(item.pack_id)
      || !validString(item.role)
      || !validString(item.version)
      || !isSha256Digest(item.artifact_digest)
      || !canonicalReference(item.artifact_ref)
      || item.artifact_ref !== `pack-v4://${item.pack_id}@${item.artifact_digest}`
    ) {
      return null;
    }
    closureIds.add(item.pack_id);
    closure.push({
      pack_id: item.pack_id,
      role: item.role,
      version: item.version,
      artifact_digest: item.artifact_digest,
      artifact_ref: item.artifact_ref,
    });
  }

  const authoritySnapshot = value.authority_snapshot;
  const authorityDigest = nullableDigest(authoritySnapshot.digest);
  if (
    (authoritySnapshot.state !== 'active' && authoritySnapshot.state !== 'captured_on_resolve')
    || (authoritySnapshot.digest !== null && authorityDigest === null)
    || (authoritySnapshot.ref !== null && !canonicalReference(authoritySnapshot.ref))
    || !Array.isArray(authoritySnapshot.definition_references)
    || !isStringArray(authoritySnapshot.definition_references)
  ) {
    return null;
  }

  const closureById = new Map(closure.map((item) => [item.pack_id, item]));
  for (const packId of [base.pack_id, shell.pack_id, application?.pack_id]) {
    if (!packId) continue;
    const closureEntry = closureById.get(packId);
    const bindingDigest = packId === base.pack_id
      ? base.artifact_digest
      : packId === shell.pack_id
        ? shell.artifact_digest
        : application?.artifact_digest ?? null;
    if (bindingDigest !== null && closureEntry && bindingDigest !== closureEntry.artifact_digest) {
      return null;
    }
  }
  if (application && application.artifact_ref !== null && application.artifact_digest !== null
    && application.artifact_ref !== `pack-v4://${application.pack_id}@${application.artifact_digest}`) {
    return null;
  }
  if (application && ((application.artifact_ref === null) !== (application.artifact_digest === null))) {
    return null;
  }
  if (authoritySnapshot.digest !== null && authoritySnapshot.ref !== null
    && authoritySnapshot.ref !== `authority-snapshot-v4://${value.profile_id}/${authoritySnapshot.digest}`) {
    return null;
  }

  const candidateId = value.candidate.candidate_id;
  const candidateDigest = nullableDigest(value.candidate.candidate_digest);
  const expiresAt = nullableDateTime(value.candidate.expires_at);
  if (
    !validString(value.candidate.state)
    || (candidateId !== null && !validString(candidateId))
    || (value.candidate.candidate_digest !== null && candidateDigest === null)
    || (value.candidate.expires_at !== null && expiresAt === null)
  ) {
    return null;
  }
  if (value.available !== (diagnostics.length === 0)) return null;
  if (value.active !== (value.lifecycle_state === 'active')) return null;
  if (value.active && (
    value.records.profile_revision === null
    || value.records.profile_lock_digest === null
    || value.records.plan_digest === null
    || authoritySnapshot.state !== 'active'
    || authoritySnapshot.digest === null
    || authoritySnapshot.ref === null
  )) {
    return null;
  }
  if (!value.active && (
    value.records.profile_revision !== null
    || value.records.profile_lock_digest !== null
    || value.records.plan_digest !== null
    || authoritySnapshot.state !== 'captured_on_resolve'
    || authoritySnapshot.digest !== null
    || authoritySnapshot.ref !== null
  )) {
    return null;
  }
  return {
    profile_id: value.profile_id,
    display_name: value.display_name,
    active: value.active,
    lifecycle_state: value.lifecycle_state,
    available: value.available,
    diagnostics,
    definition: {
      digest: value.definition.digest,
      ref: value.definition.ref,
      catalog_revision: catalogRevision,
      source_path: value.definition.source_path,
      provenance: value.definition.provenance,
    },
    bindings: {base, shell, application},
    pack_closure: closure,
    records: {
      profile_revision: profileRevision,
      profile_lock_digest: profileLockDigest,
      plan_digest: planDigest,
    },
    authority_snapshot: {
      state: authoritySnapshot.state,
      digest: authorityDigest,
      ref: authoritySnapshot.ref as string | null,
      definition_references: [...authoritySnapshot.definition_references],
    },
    candidate: {
      state: value.candidate.state,
      candidate_id: candidateId as string | null,
      candidate_digest: candidateDigest,
      expires_at: expiresAt,
    },
  };
}

export function extractExactProfileCatalog(value: unknown): RuntimeProfileCatalogProjection | null {
  if (!exactObject(value, [
    'catalog_api_version',
    'catalog_digest',
    'bundle_lock_digest',
    'catalog_ref',
    'active_profile_id',
    'count',
    'profiles',
  ])) {
    return null;
  }
  if (
    value.catalog_api_version !== 'io.tobkiri.profile-catalog-presentation.v4'
    || !isSha256Digest(value.catalog_digest)
    || !isSha256Digest(value.bundle_lock_digest)
    || !canonicalReference(value.catalog_ref)
    || value.catalog_ref !== `profile-catalog-v4://bundle/${value.catalog_digest}`
    || !validString(value.active_profile_id)
    || typeof value.count !== 'number'
    || !Number.isSafeInteger(value.count)
    || value.count < 0
    || !Array.isArray(value.profiles)
    || value.count !== value.profiles.length
  ) {
    return null;
  }
  const profiles: RuntimeProfileCatalogEntry[] = [];
  const profileIds = new Set<string>();
  let activeCount = 0;
  for (const item of value.profiles) {
    const entry = parseProfileCatalogEntry(item);
    if (!entry || profileIds.has(entry.profile_id)) return null;
    profileIds.add(entry.profile_id);
    if (entry.active) activeCount += 1;
    profiles.push(entry);
  }
  if (profiles.length > 0 && (activeCount !== 1 || !profiles.some((item) => (
    item.active && item.profile_id === value.active_profile_id
  )))) {
    return null;
  }
  if (profiles.length === 0 && activeCount !== 0) return null;
  return {
    catalog_api_version: value.catalog_api_version,
    catalog_digest: value.catalog_digest,
    bundle_lock_digest: value.bundle_lock_digest,
    catalog_ref: value.catalog_ref,
    active_profile_id: value.active_profile_id,
    count: value.count,
    profiles,
  };
}

/** Derive the exact requested Pack ids from one verified Profile definition. */
export function extractExactProfileCatalogSelectablePackIds(value: unknown): string[] | null {
  const entry = parseProfileCatalogEntry(value);
  if (!entry || !entry.available) return null;
  const excludedRoles = new Set(['base', 'shell', 'application', 'dependency']);
  return entry.pack_closure
    .filter((item) => !excludedRoles.has(item.role))
    .map((item) => item.pack_id);
}

/**
 * Return bindings only when the projection contains the complete canonical
 * identity. This keeps Graph and Profile Wiring from synthesizing edges.
 */
export function extractExactPlanBindings(value: unknown): RuntimePlanBinding[] | null {
  if (!isRecord(value)) return null;
  const wiring = isRecord(value.resolved_wiring) ? value.resolved_wiring : null;
  const bindingValue = wiring?.bindings;
  if (!Array.isArray(bindingValue)) return null;
  const bindings: RuntimePlanBinding[] = [];
  for (const candidate of bindingValue) {
    if (!isRecord(candidate)) return null;
    if (
      !validString(candidate.binding_id)
      || !validString(candidate.source_principal_id)
      || !validString(candidate.target_principal_id)
      || !validString(candidate.target_contract_id)
      || !validString(candidate.operation_id)
      || !validString(candidate.owner_pack_id)
      || !validString(candidate.edge_digest)
      || !validString(candidate.authority_reference)
    ) {
      return null;
    }
    bindings.push({
      binding_id: candidate.binding_id,
      source_principal_id: candidate.source_principal_id,
      target_principal_id: candidate.target_principal_id,
      target_contract_id: candidate.target_contract_id,
      operation_id: candidate.operation_id,
      owner_pack_id: candidate.owner_pack_id,
      edge_digest: candidate.edge_digest,
      authority_reference: candidate.authority_reference,
    });
  }
  return bindings;
}

export function extractExactRequestedEdges(value: unknown): RuntimeRequestedEdge[] | null {
  if (!isRecord(value)) return null;
  const wiring = isRecord(value.resolved_wiring) ? value.resolved_wiring : null;
  const edgeValue = wiring?.requested_edges;
  if (!Array.isArray(edgeValue)) return null;
  const edges: RuntimeRequestedEdge[] = [];
  for (const candidate of edgeValue) {
    const authorityReference = isRecord(candidate) ? candidate.authority_reference : undefined;
    if (
      !isRecord(candidate)
      || !validString(candidate.caller_function_id)
      || !validString(candidate.target_provider_id)
      || !validString(candidate.contract_id)
      || !validString(candidate.operation_id)
      || !isRecord(candidate.requested_scope_template)
      || !isOptionalString(authorityReference)
    ) {
      return null;
    }
    edges.push({
      caller_function_id: candidate.caller_function_id,
      target_provider_id: candidate.target_provider_id,
      contract_id: candidate.contract_id,
      operation_id: candidate.operation_id,
      requested_scope_template: candidate.requested_scope_template,
      authority_reference: authorityReference,
    });
  }
  return edges;
}

/** Return only provider Pack ids from the canonical Profile document. */
export function extractExactProfileSelectablePackIds(value: unknown): string[] | null {
  if (!isRecord(value) || !isRecord(value.profile_document) || !Array.isArray(value.profile_document.packs)) {
    return null;
  }
  const ids: string[] = [];
  for (const item of value.profile_document.packs) {
    if (
      !isRecord(item)
      || !validString(item.pack_id)
      || !['backend', 'contribution', 'provider', 'application'].includes(String(item.role))
      || (item.artifact_digest !== null && !/^sha256:[0-9a-f]{64}$/.test(String(item.artifact_digest)))
      || ids.includes(item.pack_id)
    ) {
      return null;
    }
    if (item.role !== 'application') ids.push(item.pack_id);
  }
  return ids;
}

function extractExactArray(value: unknown, key: string): Record<string, unknown>[] {
  if (!isRecord(value) || !Array.isArray(value[key])) return [];
  return value[key].filter(isRecord);
}

/** Normalize complete Pack lifecycle rows from the exact Packs projection. */
export function extractExactPackDescriptors(value: unknown): RuntimePackDescriptor[] {
  const rows = extractExactArray(value, 'packs');
  const packs = rows.flatMap((candidate) => {
    const invokableOperations = candidate.invokable_operations;
    const reason = candidate.reason;
    if (
      !validString(candidate.pack_id)
      || !validString(candidate.role)
      || !validString(candidate.kind)
      || !validString(candidate.version)
      || !validString(candidate.display_name)
      || !isSha256Digest(candidate.artifact_digest)
      || candidate.artifact_ref !== `pack-v4://${candidate.pack_id}@${candidate.artifact_digest}`
      || typeof candidate.installed !== 'boolean'
      || typeof candidate.enabled !== 'boolean'
      || typeof candidate.approved !== 'boolean'
      || typeof candidate.required !== 'boolean'
      || !isStringArray(invokableOperations)
      || (reason !== undefined && reason !== null && !validString(reason))
    ) {
      return [];
    }
    let normalizedReason: string | null | undefined;
    if (reason === undefined) {
      normalizedReason = undefined;
    } else if (reason === null) {
      normalizedReason = null;
    } else if (validString(reason)) {
      normalizedReason = reason;
    } else {
      return [];
    }
    return [{
      pack_id: candidate.pack_id,
      role: candidate.role,
      kind: candidate.kind,
      version: candidate.version,
      display_name: candidate.display_name,
      artifact_digest: candidate.artifact_digest,
      artifact_ref: candidate.artifact_ref,
      installed: candidate.installed,
      enabled: candidate.enabled,
      approved: candidate.approved,
      required: candidate.required,
      invokable_operations: invokableOperations,
      ...(normalizedReason === undefined ? {} : {reason: normalizedReason}),
    }];
  });
  if (
    packs.length !== rows.length
    || new Set(packs.map((pack) => pack.pack_id)).size !== packs.length
  ) {
    return [];
  }
  return packs;
}

/**
 * Normalize only complete operation rows. Partial rows are omitted instead of
 * being guessed into a Flow or AI Input item.
 */
export function extractExactOperationDescriptors(
  value: unknown,
): RuntimeOperationDescriptor[] {
  return extractExactArray(value, 'operations').flatMap((candidate) => {
    const schema = candidate.schema;
    if (
      (candidate.action !== undefined && candidate.action !== RUNTIME_CONTRACT_INVOKE_ACTION)
      || !validString(candidate.owner_pack_id)
      || !validString(candidate.contribution_id)
      || !validString(candidate.target_provider_id)
      || !isSha256Digest(candidate.artifact_digest)
      || !validString(candidate.operation_id)
      || !validString(candidate.contract_id)
      || !validString(candidate.function_id)
      || !validString(candidate.function_principal_id)
      || !validString(candidate.caller_function_id)
      || !validString(candidate.authority_reference)
      || !isRecord(schema)
      || !isSha256Digest(candidate.catalog_digest)
      || typeof candidate.invokable !== 'boolean'
    ) {
      return [];
    }
    const invocationContributionValue = candidate.invocation_contribution_id;
    const invocationOwnerPackValue = candidate.invocation_owner_pack_id;
    const invocationCatalogHashValue = candidate.invocation_catalog_hash;
    const invocationReasonValue = candidate.invocation_reason;
    const invocationContributionId = invocationContributionValue === null
      ? null
      : validString(invocationContributionValue)
        ? invocationContributionValue
        : null;
    const invocationOwnerPackId = invocationOwnerPackValue === null
      ? null
      : validString(invocationOwnerPackValue)
        ? invocationOwnerPackValue
        : null;
    const invocationCatalogHash = invocationCatalogHashValue === null
      ? null
      : isSha256Digest(invocationCatalogHashValue)
        ? invocationCatalogHashValue
        : null;
    const invocationReason = invocationReasonValue === null
      ? null
      : validString(invocationReasonValue)
        ? invocationReasonValue
        : null;
    if (
      invocationContributionValue === undefined
      || invocationOwnerPackValue === undefined
      || invocationCatalogHashValue === undefined
      || invocationReasonValue === undefined
      || (invocationContributionValue !== null && !validString(invocationContributionValue))
      || (invocationOwnerPackValue !== null && !validString(invocationOwnerPackValue))
      || (invocationCatalogHashValue !== null && !isSha256Digest(invocationCatalogHashValue))
      || (invocationReasonValue !== null && !validString(invocationReasonValue))
    ) {
      return [];
    }
    const operationIdentity: Pick<
      RuntimeOperationDescriptor,
      'contract_id' | 'operation_id' | 'function_id' | 'owner_pack_id'
    > = {
      contract_id: candidate.contract_id,
      operation_id: candidate.operation_id,
      function_id: candidate.function_id,
      owner_pack_id: candidate.owner_pack_id,
    };
    const route = parseRuntimeOperationRoute(candidate.route, operationIdentity);
    if (!route) return [];
    const inputSchema = isRuntimeJsonSchema(schema.input_schema) ? schema.input_schema : undefined;
    const providerSemantics = candidate.provider_semantics;
    const providerId = candidate.provider_id;
    const label = candidate.label;
    return [{
      action: RUNTIME_CONTRACT_INVOKE_ACTION,
      operation_id: candidate.operation_id,
      contract_id: candidate.contract_id,
      owner_pack_id: candidate.owner_pack_id,
      contribution_id: candidate.contribution_id,
      target_provider_id: candidate.target_provider_id,
      artifact_digest: candidate.artifact_digest,
      invocation_contribution_id: invocationContributionId,
      invocation_owner_pack_id: invocationOwnerPackId,
      invocation_catalog_hash: invocationCatalogHash,
      invocation_reason: invocationReason,
      invokable: candidate.invokable,
      catalog_digest: candidate.catalog_digest,
      function_id: candidate.function_id,
      function_principal_id: candidate.function_principal_id,
      caller_function_id: candidate.caller_function_id,
      authority_reference: candidate.authority_reference,
      schema,
      ...(validString(label) ? {label} : {}),
      ...(validString(providerId) ? {provider_id: providerId} : {}),
      ...(inputSchema ? {input_schema: inputSchema} : {}),
      ...(isRecord(providerSemantics) ? {provider_semantics: providerSemantics} : {}),
      route,
    }];
  });
}

/** Read invokable operation keys only from the authoritative Packs projection. */
export function extractAuthoritativeInvokableOperationKeys(value: unknown): Set<string> | null {
  const rows = extractExactArray(value, 'packs');
  const packs = extractExactPackDescriptors(value);
  if (rows.length === 0 || packs.length !== rows.length || packs.some((pack) => !pack.enabled || !pack.approved)) {
    return null;
  }
  const keys = new Set<string>();
  for (const pack of packs) {
    for (const item of pack.invokable_operations) keys.add(item);
  }
  return keys;
}

function isRuntimeJsonSchema(value: unknown): value is RuntimeJsonSchema {
  if (!isRecord(value)) return false;
  if (value.type !== undefined && typeof value.type !== 'string') return false;
  if (value.title !== undefined && typeof value.title !== 'string') return false;
  if (value.description !== undefined && typeof value.description !== 'string') return false;
  if (value.enum !== undefined && !Array.isArray(value.enum)) return false;
  if (value.required !== undefined && (
    !Array.isArray(value.required)
    || value.required.some((item) => typeof item !== 'string')
  )) return false;
  if (value.properties !== undefined) {
    if (!isRecord(value.properties)) return false;
    if (Object.values(value.properties).some((item) => !isRuntimeJsonSchema(item))) return false;
  }
  if (value.items !== undefined && !isRuntimeJsonSchema(value.items)) return false;
  return true;
}

/** Normalize complete route rows; no route is composed from an operation id. */
export function extractExactRouteDescriptors(value: unknown): RuntimeRouteDescriptor[] {
  return extractExactArray(value, 'routes').flatMap((candidate) => {
    const security = candidate.security;
    const allowedPayloadKeys = candidate.allowed_payload_keys;
    const expectedRouteKeys = [
      'route_id',
      'method',
      'logical_target',
      'contract_id',
      'operation_id',
      'contribution_id',
      'presentation',
      'owner_pack_id',
      'provider_id',
      'function_id',
      'function_principal_id',
      'manifest_digest',
      'frontend_map_digest',
      'allowed_payload_keys',
      'security',
    ];
    const expectedSecurityKeys = [
      'transport',
      'panel_authentication_required',
      'broker_authority_required',
      'csrf_required',
      'request_id_required',
      'replay_protection_required',
    ];
    if (
      Object.keys(candidate).length !== expectedRouteKeys.length
      || expectedRouteKeys.some((key) => !Object.prototype.hasOwnProperty.call(candidate, key))
      || !validString(candidate.route_id)
      || (candidate.method !== 'GET' && candidate.method !== 'POST')
      || !validString(candidate.logical_target)
      || !validString(candidate.contract_id)
      || !validString(candidate.operation_id)
      || !validString(candidate.provider_id)
      || !validString(candidate.function_id)
      || !isSha256Digest(candidate.frontend_map_digest)
      || !validString(candidate.contribution_id)
      || !validString(candidate.presentation)
      || !validString(candidate.owner_pack_id)
      || !isSha256Digest(candidate.manifest_digest)
      || !validString(candidate.function_principal_id)
      || !isStringArray(allowedPayloadKeys)
      || !isRecord(security)
      || security.transport !== 'canonical_contract'
      || typeof security.panel_authentication_required !== 'boolean'
      || typeof security.broker_authority_required !== 'boolean'
      || typeof security.csrf_required !== 'boolean'
      || typeof security.request_id_required !== 'boolean'
      || typeof security.replay_protection_required !== 'boolean'
      || Object.keys(security).length !== expectedSecurityKeys.length
      || expectedSecurityKeys.some((key) => !Object.prototype.hasOwnProperty.call(security, key))
      || security.panel_authentication_required !== true
      || security.broker_authority_required !== true
      || security.csrf_required !== (candidate.method === 'POST')
      || security.request_id_required !== (candidate.method === 'POST')
      || security.replay_protection_required !== (candidate.method === 'POST')
    ) {
      return [];
    }
    return [{
      route_id: candidate.route_id,
      method: candidate.method,
      logical_target: candidate.logical_target,
      contract_id: candidate.contract_id,
      operation_id: candidate.operation_id,
      provider_id: candidate.provider_id,
      function_id: candidate.function_id,
      frontend_map_digest: candidate.frontend_map_digest,
      contribution_id: candidate.contribution_id,
      presentation: candidate.presentation,
      owner_pack_id: candidate.owner_pack_id,
      manifest_digest: candidate.manifest_digest,
      function_principal_id: candidate.function_principal_id,
      allowed_payload_keys: allowedPayloadKeys,
      security: {
        transport: security.transport,
        panel_authentication_required: security.panel_authentication_required,
        broker_authority_required: security.broker_authority_required,
        csrf_required: security.csrf_required,
        request_id_required: security.request_id_required,
        replay_protection_required: security.replay_protection_required,
      },
    }];
  });
}

/** Flow rows must be declared composition records, never Pack-name matches. */
export function extractExactFlowDescriptors(value: unknown): RuntimeFlowDescriptor[] {
  return extractExactArray(value, 'flows').flatMap((candidate) => {
    const operationIds = candidate.operation_ids;
    if (
      !validString(candidate.flow_id)
      || !validString(candidate.state)
      || !isStringArray(operationIds)
    ) {
      return [];
    }
    return [{
      flow_id: candidate.flow_id,
      state: candidate.state,
      operation_ids: operationIds,
      ...(validString(candidate.label) ? {label: candidate.label} : {}),
    }];
  });
}

function isSafeRelativeArtifactPath(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.startsWith('/') || value.startsWith('file:')) return false;
  if (/^[A-Za-z]:[\\/]/.test(value) || value.includes('\\') || value.includes('\0')) return false;
  const segments = value.split('/');
  return segments.every((segment) => segment.length > 0 && segment !== '.' && segment !== '..');
}

/** Record only finite manifest artifact evidence, never a host file path. */
export function extractFiniteArtifactEntries(value: unknown): RuntimeArtifactEntry[] | null {
  if (!isRecord(value)) return null;
  const candidates = Array.isArray(value.artifact_entries)
    ? value.artifact_entries
    : Array.isArray(value.pack_closure)
      ? value.pack_closure.flatMap((pack) => isRecord(pack) && Array.isArray(pack.artifacts) ? pack.artifacts : [])
      : null;
  if (!candidates) return null;
  const entries: RuntimeArtifactEntry[] = [];
  for (const candidate of candidates) {
    if (
      !isRecord(candidate)
      || !validString(candidate.entry_id)
      || !validString(candidate.owner_pack_id)
      || !isSafeRelativeArtifactPath(candidate.path)
      || !validString(candidate.kind)
      || 'host_path' in candidate
      || !isSha256Digest(candidate.artifact_digest)
    ) {
      return null;
    }
    entries.push({
      entry_id: candidate.entry_id,
      owner_pack_id: candidate.owner_pack_id,
      path: candidate.path,
      kind: candidate.kind,
      artifact_digest: candidate.artifact_digest,
    });
  }
  return entries;
}

function runtimeOperationMatchesSnapshot(
  envelope: RuntimeSurfaceEnvelope<unknown>,
  operation: RuntimeOperationDescriptor,
): boolean {
  const acceptedOperations = extractExactOperationDescriptors(envelope.data);
  const operationInputSchema = operation.input_schema
    ?? (isRecord(operation.schema) && isRuntimeJsonSchema(operation.schema.input_schema)
      ? operation.schema.input_schema
      : undefined);
  return acceptedOperations.some((candidate) => (
    candidate.action === operation.action
    && candidate.operation_id === operation.operation_id
    && candidate.contract_id === operation.contract_id
    && candidate.owner_pack_id === operation.owner_pack_id
    && candidate.contribution_id === operation.contribution_id
    && candidate.target_provider_id === operation.target_provider_id
    && candidate.artifact_digest === operation.artifact_digest
    && candidate.invocation_contribution_id === operation.invocation_contribution_id
    && candidate.invocation_owner_pack_id === operation.invocation_owner_pack_id
    && candidate.invocation_catalog_hash === operation.invocation_catalog_hash
    && candidate.invocation_reason === operation.invocation_reason
    && candidate.invokable === operation.invokable
    && candidate.catalog_digest === operation.catalog_digest
    && candidate.function_id === operation.function_id
    && candidate.function_principal_id === operation.function_principal_id
    && candidate.caller_function_id === operation.caller_function_id
    && candidate.authority_reference === operation.authority_reference
    && candidate.route.contract_id === operation.route.contract_id
    && candidate.route.operation_id === operation.route.operation_id
    && candidate.route.function_id === operation.route.function_id
    && candidate.route.provider_pack_id === operation.route.provider_pack_id
    && JSON.stringify(candidate.schema) === JSON.stringify(operation.schema)
    && JSON.stringify(candidate.input_schema) === JSON.stringify(operationInputSchema)
  ));
}

export interface RuntimeOperationInvocation {
  envelope: RuntimeSurfaceEnvelope<unknown>;
  operation: RuntimeOperationDescriptor;
  payload: Record<string, unknown>;
  requestId?: string;
}

/**
 * Invoke one catalog-declared operation through the existing Broker-backed
 * capability path. Authority fields and client approval flags are impossible
 * to add to this request shape.
 */
export function invokeRuntimeOperation({
  envelope,
  operation,
  payload,
  requestId,
}: RuntimeOperationInvocation): Promise<unknown> {
  if (!isRecord(payload)) {
    return Promise.reject(new RuntimeSurfaceError(
      'INVALID',
      'Operation input must be a JSON object declared by the accepted operation schema.',
    ));
  }
  if (envelope.state !== 'ready') {
    const code = envelope.state === 'blocked' ? 'APPROVAL_DENIED' : 'STALE';
    return Promise.reject(new RuntimeSurfaceError(code, runtimeSurfaceErrorMessage(code)));
  }
  const route = parseRuntimeOperationRoute(operation.route, operation);
  const operationInputSchema = operation.input_schema
    ?? (isRecord(operation.schema) && isRuntimeJsonSchema(operation.schema.input_schema)
      ? operation.schema.input_schema
      : undefined);
  if (
    operation.action !== RUNTIME_CONTRACT_INVOKE_ACTION
    || !validString(operation.operation_id)
    || !validString(operation.contract_id)
    || !validString(operation.owner_pack_id)
    || !validString(operation.contribution_id)
    || !validString(operation.target_provider_id)
    || !isSha256Digest(operation.artifact_digest)
    || !validString(operation.function_id)
    || !validString(operation.function_principal_id)
    || !validString(operation.caller_function_id)
    || !validString(operation.authority_reference)
    || !isRecord(operation.schema)
    || !isSha256Digest(operation.catalog_digest)
    || (operation.invocation_contribution_id !== null
      && !validString(operation.invocation_contribution_id))
    || (operation.invocation_owner_pack_id !== null
      && !validString(operation.invocation_owner_pack_id))
    || (operation.invocation_catalog_hash !== null
      && !isSha256Digest(operation.invocation_catalog_hash))
    || (operation.invocation_reason !== null && !validString(operation.invocation_reason))
    || !route
    || !isSha256Digest(envelope.plan_digest)
    || !isSha256Digest(envelope.catalog_revision)
    || !runtimeOperationMatchesSnapshot(envelope, operation)
  ) {
    return Promise.reject(new RuntimeSurfaceError(
      'DIGEST_MISMATCH',
      'The operation binding is not owned by the accepted operations snapshot.',
    ));
  }
  const forbiddenPayloadKeys = new Set([
    'approved',
    'approval_token',
    'caller',
    'caller_id',
    'target',
    'target_id',
    'provider',
    'provider_id',
    'authority',
    'authority_reference',
    'profile_id',
    'plan_hash',
    'catalog_hash',
  ]);
  if (Object.keys(payload).some((key) => forbiddenPayloadKeys.has(key))) {
    return Promise.reject(new RuntimeSurfaceError(
      'INVALID',
      'Operation input cannot provide authority, caller, target, provider, or approval fields.',
    ));
  }
  const allowedInputProperties = operationInputSchema?.properties;
  if (Object.keys(payload).some((key) => (
    !allowedInputProperties
    || !Object.prototype.hasOwnProperty.call(allowedInputProperties, key)
  ))) {
    return Promise.reject(new RuntimeSurfaceError(
      'INVALID',
      'Operation input contains a key not declared by the accepted operation schema.',
    ));
  }
  if (
    envelope.surface !== 'operations'
    || !operation.owner_pack_id
    || (operation.invocation_owner_pack_id !== null
      && operation.invocation_owner_pack_id !== operation.owner_pack_id)
  ) {
    return Promise.reject(new RuntimeSurfaceError(
      'DIGEST_MISMATCH',
      'The operation binding is not owned by the accepted operations snapshot.',
    ));
  }
  if (
    !operation.invokable
    || !operation.invocation_contribution_id
    || !operation.invocation_owner_pack_id
    || !operation.invocation_catalog_hash
  ) {
    return Promise.reject(new RuntimeSurfaceError(
      'APPROVAL_DENIED',
      'The selected operation is not currently invokable in the accepted snapshot.',
    ));
  }
  if (
    !envelope.catalog_revision
    || operation.catalog_digest !== envelope.catalog_revision
    || operation.invocation_catalog_hash !== envelope.catalog_revision
  ) {
    return Promise.reject(new RuntimeSurfaceError(
      'DIGEST_MISMATCH',
      runtimeSurfaceErrorMessage('DIGEST_MISMATCH'),
    ));
  }
  const authoritativeInvokableOperationKeys = extractAuthoritativeInvokableOperationKeys(envelope.data);
  if (!authoritativeInvokableOperationKeys?.has(`${operation.contract_id}::${operation.operation_id}`)) {
    return Promise.reject(new RuntimeSurfaceError(
      'APPROVAL_DENIED',
      'The selected operation is not listed by an approved, enabled Pack in the accepted snapshot.',
    ));
  }
  if (!operation.invocation_contribution_id || !operation.invocation_owner_pack_id) {
    return Promise.reject(new RuntimeSurfaceError(
      'INVALID',
      'The accepted operation has no exact frontend catalog contribution binding.',
    ));
  }
  return invokeFrontendCapability({
    profileId: envelope.profile_id,
    planHash: envelope.plan_digest,
    catalogHash: operation.invocation_catalog_hash,
    contributionId: operation.invocation_contribution_id,
    ownerPackId: operation.invocation_owner_pack_id,
    contractId: operation.contract_id,
    payload,
  }, {requestId});
}

export function classifyRuntimeSurfaceError(error: unknown): RuntimeSurfaceErrorCode {
  if (error instanceof RuntimeSurfaceError) return error.code;
  if (error instanceof ApiRequestTimeoutError || (error instanceof Error && error.name === 'AbortError')) {
    return 'TIMEOUT';
  }
  const errorData = isRecord(error) && isRecord(error.data) ? error.data : null;
  const typedCode = errorData && typeof errorData.code === 'string' ? errorData.code : null;
  if (typedCode === 'PROFILE_NOT_ACTIVE') return 'PROFILE_NOT_ACTIVE';
  if (typedCode === 'STALE_REVISION') return 'STALE';
  if (typedCode === 'DIGEST_MISMATCH') return 'DIGEST_MISMATCH';
  if (typedCode === 'UNAPPROVED') return 'APPROVAL_DENIED';
  if (typedCode === 'TIMEOUT') return 'TIMEOUT';
  const message = error instanceof Error ? error.message.toLowerCase() : '';
  if (message.includes('not active') || message.includes('profile_not_active')) return 'PROFILE_NOT_ACTIVE';
  if (message.includes('timeout') || message.includes('timed out')) return 'TIMEOUT';
  if (message.includes('digest') || message.includes('stale') || message.includes('revision')) {
    return 'DIGEST_MISMATCH';
  }
  if (message.includes('approval') || message.includes('denied') || message.includes('blocked')) {
    return 'APPROVAL_DENIED';
  }
  return 'FAILED';
}

export interface RuntimeSurfaceClient {
  read<T>(surface: RuntimeSurfaceId, input?: {
    expected_profile_revision?: string;
    expected_plan_digest?: string;
  }): Promise<RuntimeSurfaceEnvelope<T>>;
}

export function createRuntimeSurfaceClient(
  targets: Partial<Record<RuntimeSurfaceId, RuntimeSurfaceTarget>> = RUNTIME_SURFACE_TARGETS,
  transport: RuntimeSurfaceTransport = canonicalTransport,
): RuntimeSurfaceClient {
  return {
    read: async <T>(surface: RuntimeSurfaceId, input = {}) => {
      const target = targets[surface];
      if (!target) {
        throw new RuntimeSurfaceError('UNAVAILABLE', runtimeSurfaceErrorMessage('UNAVAILABLE'));
      }
      assertVerifiedRuntimeTarget(target);
      const requestInput = target.read_guards === false ? {} : input;
      assertTargetPayload(target, requestInput);
      const response = await transport.read<unknown>(target, requestInput);
      return validateRuntimeSurfaceEnvelope<T>(surface, response);
    },
  };
}

export const defaultRuntimeSurfaceClient = createRuntimeSurfaceClient();
