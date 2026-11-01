/** Verified Host choices for editing a definition, independent of active Pack state. */
export interface ProfileCompositionInput {
  pack_ids: string[];
  profile_catalog_digest: string;
  bundle_lock_digest: string;
}
export interface ProfilePackChoice {
  pack_id: string;
  display_name: string;
  version: string;
  kind: string;
  artifact_digest: string;
  dependencies: string[];
}
export interface ProfileCompositionCatalog {
  composition_api_version: 'io.tobkiri.profile-composition.v4';
  profile_catalog_digest: string;
  bundle_lock_digest: string;
  packs: ProfilePackChoice[];
}
const digest = (value: unknown): value is string => typeof value === 'string' && /^sha256:[a-f0-9]{64}$/.test(value);
const id = (value: unknown): value is string => typeof value === 'string' && value.length <= 128 && /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/.test(value);
function exactRecord(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== keys.sort().join(',')) {
    throw new Error('Profile composition response or request has unexpected fields.');
  }
  return value as Record<string, unknown>;
}
function ids(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every(id) || new Set(value).size !== value.length) {
    throw new Error('Profile composition Pack IDs are invalid.');
  }
  return [...value];
}
export function parseProfileCompositionInput(value: unknown): ProfileCompositionInput {
  const record = exactRecord(value, ['pack_ids', 'profile_catalog_digest', 'bundle_lock_digest']);
  const packIds = ids(record.pack_ids);
  if (!packIds.length || !digest(record.profile_catalog_digest) || !digest(record.bundle_lock_digest)) {
    throw new Error('Profile composition requires Pack choices and verified catalog bindings.');
  }
  return {pack_ids: packIds, profile_catalog_digest: record.profile_catalog_digest, bundle_lock_digest: record.bundle_lock_digest};
}
export function parseProfileCompositionCatalog(value: unknown): ProfileCompositionCatalog {
  const record = exactRecord(value, ['composition_api_version', 'profile_catalog_digest', 'bundle_lock_digest', 'packs']);
  if (record.composition_api_version !== 'io.tobkiri.profile-composition.v4'
    || !digest(record.profile_catalog_digest) || !digest(record.bundle_lock_digest) || !Array.isArray(record.packs)) {
    throw new Error('Profile Pack catalog is invalid.');
  }
  const packs = record.packs.map((value) => {
    const pack = exactRecord(value, ['pack_id', 'display_name', 'version', 'kind', 'artifact_digest', 'dependencies']);
    if (!id(pack.pack_id) || !digest(pack.artifact_digest)
      || !['display_name', 'version', 'kind'].every((key) => typeof pack[key] === 'string' && (pack[key] as string).trim())) {
      throw new Error('Profile Pack catalog entry is invalid.');
    }
    return {...pack, dependencies: ids(pack.dependencies)} as unknown as ProfilePackChoice;
  });
  if (new Set(packs.map((pack) => pack.pack_id)).size !== packs.length) throw new Error('Profile Pack catalog has duplicate entries.');
  return {...record, packs} as unknown as ProfileCompositionCatalog;
}
export function profilePackDependencies(packs: ProfilePackChoice[], selected: string[]): Set<string> {
  const byId = new Map(packs.map((pack) => [pack.pack_id, pack]));
  const closure = new Set<string>();
  const pending = [...selected];
  while (pending.length) {
    const packId = pending.pop()!;
    if (closure.has(packId)) continue;
    closure.add(packId);
    pending.push(...(byId.get(packId)?.dependencies ?? []));
  }
  return closure;
}
