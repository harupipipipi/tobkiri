import type {PackControlBinding, PacksResponseData} from './apiTypes';
import type {Pack} from '../store';

export const PACK_CONTROL_BINDING_FIELDS = [
  'profile_id',
  'workspace_id',
  'profile_revision',
  'plan_digest',
  'catalog_revision',
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredBindingString(value: unknown, field: string, label: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Tobkiri returned an invalid Pack catalog ${label} ${field}.`);
  }
  return value;
}

/** Parse the binding that gives Pack state its active Profile scope. */
export function parsePackControlBinding(
  value: unknown,
  label = 'response',
): PackControlBinding {
  if (!isRecord(value)) {
    throw new Error(`Tobkiri returned an invalid Pack catalog ${label}.`);
  }
  return {
    profile_id: requiredBindingString(value.profile_id, 'profile_id', label),
    workspace_id: requiredBindingString(value.workspace_id, 'workspace_id', label),
    profile_revision: requiredBindingString(value.profile_revision, 'profile_revision', label),
    plan_digest: requiredBindingString(value.plan_digest, 'plan_digest', label),
    catalog_revision: requiredBindingString(value.catalog_revision, 'catalog_revision', label),
  };
}

/** Compare two v4 bindings without allowing a Profile or revision mix-up. */
export function packControlBindingsEqual(
  left: PackControlBinding,
  right: PackControlBinding,
): boolean {
  return PACK_CONTROL_BINDING_FIELDS.every((field) => left[field] === right[field]);
}

/** Validate that every Pack row belongs to the same catalog scope as its response. */
export function parsePacksResponse(value: unknown): PacksResponseData {
  if (!isRecord(value)) {
    throw new Error('Tobkiri returned an invalid Pack catalog response.');
  }
  const binding = parsePackControlBinding(value, 'response');
  if (!Array.isArray(value.packs)) {
    throw new Error('Tobkiri returned an invalid Pack catalog list.');
  }
  if (
    typeof value.count !== 'number'
    || !Number.isSafeInteger(value.count)
    || value.count < 0
    || value.count !== value.packs.length
  ) {
    throw new Error('Tobkiri returned a Pack catalog count that does not match its rows.');
  }
  value.packs.forEach((pack, index) => {
    const rowBinding = parsePackControlBinding(pack, `packs[${index}]`);
    if (!packControlBindingsEqual(rowBinding, binding)) {
      throw new Error(
        `Tobkiri returned Pack catalog row ${index} with a different Profile scope.`,
      );
    }
  });
  return value as unknown as PacksResponseData;
}

/** Return true only when a Pack row is bound to the authoritative catalog scope. */
export function isPackInCatalogScope(
  pack: Pick<Pack, 'profileId' | 'workspaceId' | 'profileRevision' | 'planDigest' | 'catalogRevision'>,
  binding: PackControlBinding | null,
): boolean {
  return binding !== null
    && pack.profileId === binding.profile_id
    && pack.workspaceId === binding.workspace_id
    && pack.profileRevision === binding.profile_revision
    && pack.planDigest === binding.plan_digest
    && pack.catalogRevision === binding.catalog_revision;
}
