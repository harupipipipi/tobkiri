/** Host-owned v4 Named Profile registry contract and request validation. */

export const PROFILE_REGISTRY_API_VERSION = 'io.tobkiri.profile-registry.v4' as const;

export interface NamedProfileRecord {
  profile_id: string;
  /** Immutable definition-store revision for this Named Profile record. */
  profile_revision: string;
  profile: Record<string, unknown>;
  order: number;
  parent_revision: string | null;
  tombstone: boolean;
  created_at: number;
  updated_at: number;
  legacy_ids: string[];
}

export interface NamedProfileRegistry {
  profile_registry_api_version: typeof PROFILE_REGISTRY_API_VERSION;
  generation: number;
  active_profile_id: string | null;
  /** Resolved/activated execution revision; it may differ from a definition revision. */
  active_profile_revision: string | null;
  profiles: NamedProfileRecord[];
  changed_profile?: NamedProfileRecord;
  action?: 'create' | 'update' | 'duplicate' | 'delete';
}

export interface CreateNamedProfileInput {
  profile_id: string;
  display_name: string;
  source_profile_id: string;
  expected_store_generation: number;
}

export interface UpdateNamedProfileInput {
  profile_id: string;
  display_name: string;
  expected_profile_revision: string;
  expected_store_generation: number;
}

export interface DuplicateNamedProfileInput {
  profile_id: string;
  new_profile_id: string;
  display_name: string;
  expected_profile_revision: string;
  expected_store_generation: number;
}

export interface DeleteNamedProfileInput {
  profile_id: string;
  expected_profile_revision: string;
  expected_store_generation: number;
}

export type NamedProfileMutationInput =
  | CreateNamedProfileInput
  | UpdateNamedProfileInput
  | DuplicateNamedProfileInput
  | DeleteNamedProfileInput;

export class ProfileRegistryContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ProfileRegistryContractError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isCanonicalId(value: unknown): value is string {
  return typeof value === 'string'
    && value.length <= 128
    && /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/.test(value);
}

function isDigest(value: unknown): value is string {
  return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

function requiredRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new ProfileRegistryContractError(`Profile registry ${label} is not an object.`);
  }
  return value;
}

function requiredInteger(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new ProfileRegistryContractError(`Profile registry ${label} is invalid.`);
  }
  return value;
}

function parseProfileRecord(value: unknown, label: string): NamedProfileRecord {
  const record = requiredRecord(value, label);
  const expectedKeys = [
    'profile_id',
    'profile_revision',
    'profile',
    'order',
    'parent_revision',
    'tombstone',
    'created_at',
    'updated_at',
    'legacy_ids',
  ].sort();
  const actualKeys = Object.keys(record).sort();
  if (actualKeys.length !== expectedKeys.length || actualKeys.some((key, index) => key !== expectedKeys[index])) {
    throw new ProfileRegistryContractError(`Profile registry ${label} has unexpected fields.`);
  }
  if (!isCanonicalId(record.profile_id)) {
    throw new ProfileRegistryContractError(`Profile registry ${label}.profile_id is invalid.`);
  }
  if (!isDigest(record.profile_revision)) {
    throw new ProfileRegistryContractError(`Profile registry ${label}.profile_revision is invalid.`);
  }
  const profile = requiredRecord(record.profile, `${label}.profile`);
  if (profile.profile_id !== record.profile_id) {
    throw new ProfileRegistryContractError(`Profile registry ${label} has a mismatched Profile ID.`);
  }
  if (profile.display_name !== undefined && (
    typeof profile.display_name !== 'string' || profile.display_name.trim().length === 0
  )) {
    throw new ProfileRegistryContractError(`Profile registry ${label}.display_name is invalid.`);
  }
  if (record.parent_revision !== null && !isDigest(record.parent_revision)) {
    throw new ProfileRegistryContractError(`Profile registry ${label}.parent_revision is invalid.`);
  }
  if (typeof record.tombstone !== 'boolean') {
    throw new ProfileRegistryContractError(`Profile registry ${label}.tombstone is invalid.`);
  }
  if (!Array.isArray(record.legacy_ids) || record.legacy_ids.some((id) => typeof id !== 'string' || !id)) {
    throw new ProfileRegistryContractError(`Profile registry ${label}.legacy_ids is invalid.`);
  }
  const parentRevision = record.parent_revision;
  const tombstone = record.tombstone;
  const legacyIds = record.legacy_ids;
  return {
    profile_id: record.profile_id as string,
    profile_revision: record.profile_revision as string,
    profile,
    order: requiredInteger(record.order, `${label}.order`),
    parent_revision: parentRevision as string | null,
    tombstone,
    created_at: requiredInteger(record.created_at, `${label}.created_at`),
    updated_at: requiredInteger(record.updated_at, `${label}.updated_at`),
    legacy_ids: [...legacyIds] as string[],
  };
}

/** Parse and validate the Host registry before any Profile is rendered. */
export function parseNamedProfileRegistry(value: unknown): NamedProfileRegistry {
  const record = requiredRecord(value, 'response');
  const allowedKeys = [
    'profile_registry_api_version',
    'generation',
    'active_profile_id',
    'active_profile_revision',
    'profiles',
    'changed_profile',
    'action',
  ];
  const actualKeys = Object.keys(record).sort();
  const baseKeys = allowedKeys.slice(0, 5).sort();
  const mutationKeys = [...baseKeys, 'action', 'changed_profile'].sort();
  if (
    (actualKeys.length !== baseKeys.length && actualKeys.length !== mutationKeys.length)
    || actualKeys.some((key, index) => (
      key !== (actualKeys.length === baseKeys.length ? baseKeys : mutationKeys)[index]
    ))
  ) {
    throw new ProfileRegistryContractError('Profile registry response has unexpected fields.');
  }
  if (record.profile_registry_api_version !== PROFILE_REGISTRY_API_VERSION) {
    throw new ProfileRegistryContractError('Profile registry response has an invalid API version.');
  }
  const generation = requiredInteger(record.generation, 'generation');
  if (record.active_profile_id !== null && !isCanonicalId(record.active_profile_id)) {
    throw new ProfileRegistryContractError('Profile registry active_profile_id is invalid.');
  }
  if (record.active_profile_revision !== null && !isDigest(record.active_profile_revision)) {
    throw new ProfileRegistryContractError('Profile registry active_profile_revision is invalid.');
  }
  if ((record.active_profile_id === null) !== (record.active_profile_revision === null)) {
    throw new ProfileRegistryContractError('Profile registry active pointer is incomplete.');
  }
  if (!Array.isArray(record.profiles)) {
    throw new ProfileRegistryContractError('Profile registry profiles is not an array.');
  }
  const profiles = record.profiles.map((profile, index) => parseProfileRecord(profile, `profiles[${index}]`));
  const profileIds = new Set<string>();
  for (const profile of profiles) {
    if (profile.tombstone) {
      throw new ProfileRegistryContractError('Profile registry profiles cannot expose tombstones.');
    }
    if (profileIds.has(profile.profile_id)) {
      throw new ProfileRegistryContractError('Profile registry contains duplicate Profile IDs.');
    }
    profileIds.add(profile.profile_id);
  }
  if (record.active_profile_id !== null) {
    // The live pointer identifies the resolved execution snapshot. The selected
    // record carries the immutable definition revision, so these digests are
    // intentionally validated independently rather than compared for equality.
    const active = profiles.find((profile) => profile.profile_id === record.active_profile_id);
    if (!active) {
      throw new ProfileRegistryContractError('Profile registry active pointer references an unknown Profile.');
    }
  }
  const hasChangedProfile = Object.prototype.hasOwnProperty.call(record, 'changed_profile');
  const hasAction = Object.prototype.hasOwnProperty.call(record, 'action');
  if (hasChangedProfile !== hasAction) {
    throw new ProfileRegistryContractError('Profile registry mutation result is incomplete.');
  }
  const action = hasAction ? record.action : undefined;
  if (action !== undefined && !['create', 'update', 'duplicate', 'delete'].includes(String(action))) {
    throw new ProfileRegistryContractError('Profile registry action is invalid.');
  }
  const changedProfile = hasChangedProfile
    ? parseProfileRecord(record.changed_profile, 'changed_profile')
    : undefined;
  const activeProfileId = record.active_profile_id as string | null;
  const activeProfileRevision = record.active_profile_revision as string | null;
  return {
    profile_registry_api_version: PROFILE_REGISTRY_API_VERSION,
    generation,
    active_profile_id: activeProfileId,
    active_profile_revision: activeProfileRevision,
    profiles,
    ...(changedProfile ? {changed_profile: changedProfile} : {}),
    ...(action ? {action: action as NamedProfileRegistry['action']} : {}),
  };
}

function requiredMutationId(value: unknown, label: string): string {
  if (!isCanonicalId(value)) {
    throw new ProfileRegistryContractError(`${label} is not a canonical Profile ID.`);
  }
  return value;
}

function requiredDisplayName(value: unknown): string {
  if (typeof value !== 'string' || value.trim().length === 0 || value.trim().length > 160) {
    throw new ProfileRegistryContractError('Profile display name must be between 1 and 160 characters.');
  }
  return value.trim();
}

function requiredGeneration(value: unknown): number {
  return requiredInteger(value, 'expected_store_generation');
}

function requiredRevision(value: unknown): string {
  if (!isDigest(value)) {
    throw new ProfileRegistryContractError('Profile revision must be a sha256 digest.');
  }
  return value;
}

/** Validate a mutation payload before it crosses the authenticated Host boundary. */
export function validateNamedProfileMutation(
  action: 'create' | 'update' | 'duplicate' | 'delete',
  value: unknown,
): NamedProfileMutationInput {
  const record = requiredRecord(value, `${action} request`);
  const expectedKeys = action === 'create'
    ? ['display_name', 'expected_store_generation', 'profile_id', 'source_profile_id']
    : action === 'update'
      ? ['display_name', 'expected_profile_revision', 'expected_store_generation', 'profile_id']
      : action === 'duplicate'
        ? ['display_name', 'expected_profile_revision', 'expected_store_generation', 'new_profile_id', 'profile_id']
        : ['expected_profile_revision', 'expected_store_generation', 'profile_id'];
  const actualKeys = Object.keys(record).sort();
  const sortedExpected = [...expectedKeys].sort();
  if (actualKeys.length !== sortedExpected.length || actualKeys.some((key, index) => key !== sortedExpected[index])) {
    throw new ProfileRegistryContractError(`Profile ${action} request contains unexpected fields.`);
  }
  const common = {
    profile_id: requiredMutationId(record.profile_id, 'profile_id'),
    expected_store_generation: requiredGeneration(record.expected_store_generation),
  };
  if (action === 'create') {
    return {
      ...common,
      display_name: requiredDisplayName(record.display_name),
      source_profile_id: requiredMutationId(record.source_profile_id, 'source_profile_id'),
    };
  }
  const revision = requiredRevision(record.expected_profile_revision);
  if (action === 'update') {
    return {...common, display_name: requiredDisplayName(record.display_name), expected_profile_revision: revision};
  }
  if (action === 'duplicate') {
    return {
      ...common,
      display_name: requiredDisplayName(record.display_name),
      expected_profile_revision: revision,
      new_profile_id: requiredMutationId(record.new_profile_id, 'new_profile_id'),
    };
  }
  return {...common, expected_profile_revision: revision};
}
