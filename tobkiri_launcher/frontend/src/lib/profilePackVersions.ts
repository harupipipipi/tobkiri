/** Exact Host metadata for retained, explicitly selected Pack revisions. */
import {hostApiFetch} from './hostClient';

export interface PackRevision {
  artifact_digest: string;
  version: string;
  origin: 'bundled' | 'signed-admission';
  publisher_id?: string;
  key_id?: string;
  capabilities: string[];
  contracts: Record<string, unknown>[];
}
export interface ProfilePackVersions {
  profile_id: string;
  profile_definition_revision: string;
  store_generation: number;
  catalog_revision: string;
  packs: {pack_id: string; role: string; selected_digest: string; versions: PackRevision[]}[];
}
const idPattern = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const digestPattern = /^sha256:[0-9a-f]{64}$/;
const isId = (value: unknown): value is string => typeof value === 'string' && value.length <= 128 && idPattern.test(value);
const isDigest = (value: unknown): value is string => typeof value === 'string' && digestPattern.test(value);
const isRecord = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const deny = (): never => { throw new Error('Pack revision response is invalid.'); };
const exactKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).sort().join('|') === [...keys].sort().join('|');

export function parseProfilePackVersions(value: unknown): ProfilePackVersions {
  if (!isRecord(value) || !exactKeys(value, ['profile_id', 'profile_definition_revision', 'store_generation', 'catalog_revision', 'packs'])
    || !isId(value.profile_id) || !isDigest(value.profile_definition_revision) || !isDigest(value.catalog_revision)
    || !Number.isSafeInteger(value.store_generation) || (value.store_generation as number) < 0 || !Array.isArray(value.packs)) return deny();
  const seen = new Set<string>();
  for (const pack of value.packs) {
    if (!isRecord(pack) || !exactKeys(pack, ['pack_id', 'role', 'selected_digest', 'versions'])
      || !isId(pack.pack_id) || seen.has(pack.pack_id) || !['application', 'provider', 'backend', 'contribution', 'optional'].includes(String(pack.role))
      || !isDigest(pack.selected_digest) || !Array.isArray(pack.versions) || !pack.versions.length) return deny();
    seen.add(pack.pack_id);
    const digests = new Set<string>();
    for (const revision of pack.versions) {
      if (!isRecord(revision) || !['bundled', 'signed-admission'].includes(String(revision.origin))
        || !exactKeys(revision, ['artifact_digest', 'version', 'origin', 'capabilities', 'contracts', ...(revision.origin === 'signed-admission' ? ['publisher_id', 'key_id'] : [])])
        || !isDigest(revision.artifact_digest) || digests.has(revision.artifact_digest)
        || typeof revision.version !== 'string' || !revision.version.trim()
        || !Array.isArray(revision.capabilities) || revision.capabilities.some((item) => typeof item !== 'string')
        || !Array.isArray(revision.contracts) || revision.contracts.some((item) => !isRecord(item))
        || (revision.origin === 'signed-admission' && (!isId(revision.publisher_id) || typeof revision.key_id !== 'string' || !/^[0-9a-f]{32}$/.test(revision.key_id)))) return deny();
      digests.add(revision.artifact_digest);
    }
    if (!digests.has(pack.selected_digest)) return deny();
  }
  return value as unknown as ProfilePackVersions;
}

export async function fetchProfilePackVersions(profileId: string): Promise<ProfilePackVersions> {
  if (!isId(profileId)) return deny();
  const result = parseProfilePackVersions(await hostApiFetch<unknown>(
    `/api/v4/profiles/pack-versions?profile_id=${profileId}`, {cache: 'no-store'}, {timeoutMs: 30_000},
  ));
  if (result.profile_id !== profileId) return deny();
  return result;
}

export async function selectProfilePackVersion(view: ProfilePackVersions, packId: string, digest: string): Promise<boolean> {
  parseProfilePackVersions(view);
  const row = view.packs.find((pack) => pack.pack_id === packId);
  if (!row?.versions.some((revision) => revision.artifact_digest === digest)) return deny();
  const result = await hostApiFetch<unknown>('/api/v4/profiles/select-pack-version', {
    method: 'POST', body: JSON.stringify({
      profile_id: view.profile_id, pack_id: packId, artifact_digest: digest,
      expected_selected_digest: row.selected_digest, expected_profile_revision: view.profile_definition_revision,
      expected_store_generation: view.store_generation, expected_catalog_revision: view.catalog_revision,
    }),
  }, {timeoutMs: 30_000});
  if (!isRecord(result) || !exactKeys(result, ['profile_id', 'profile_definition_revision', 'activation_required'])
    || result.profile_id !== view.profile_id || !isDigest(result.profile_definition_revision) || typeof result.activation_required !== 'boolean') return deny();
  return result.activation_required;
}
