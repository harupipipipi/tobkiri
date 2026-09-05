import type {NamedProfileRecord} from './profileRegistry';

export type NamedProfileSortMode = 'recommended' | 'recent' | 'name';
export type NamedProfileStatus = 'ready' | 'error';

export interface NamedProfileView {
  basePackId: string | null;
  displayName: string;
  packIds: string[];
  status: NamedProfileStatus;
  statusDescription: string | null;
  statusLabel: 'Ready' | 'Error';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function isDigest(value: unknown): boolean {
  return typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value);
}

function resolvedReference(value: unknown): boolean {
  return isDigest(value);
}

export function namedProfileDisplayName(entry: NamedProfileRecord): string {
  return nonEmptyString(entry.profile.display_name) ?? entry.profile_id;
}

function profileError(
  basePackId: string | null,
  packIds: string[],
  description: string,
): NamedProfileView {
  return {
    basePackId,
    displayName: '',
    packIds,
    status: 'error',
    statusDescription: description,
    statusLabel: 'Error',
  };
}

/**
 * Project one immutable registry definition into the small status view Home
 * needs. The registry remains the authority; this helper only interprets the
 * v4/v5 Profile document lifecycle and never performs a runtime action.
 */
export function buildNamedProfileView(entry: NamedProfileRecord): NamedProfileView {
  const profile = entry.profile;
  const displayName = namedProfileDisplayName(entry);
  const base = isRecord(profile.base) ? profile.base : null;
  const basePackId = nonEmptyString(base?.pack_id);
  const rawPacks = Array.isArray(profile.packs) ? profile.packs : [];
  const packIds = rawPacks
    .map((pack) => (isRecord(pack) ? nonEmptyString(pack.pack_id) : null))
    .filter((packId): packId is string => packId !== null);
  const withDisplayName = (view: NamedProfileView): NamedProfileView => ({
    ...view,
    displayName,
  });

  const profileApiVersion = nonEmptyString(profile.profile_api_version);
  if (profileApiVersion !== 'io.tobkiri.profile.v4' && profileApiVersion !== 'io.tobkiri.profile.v5') {
    return withDisplayName(profileError(
      basePackId,
      packIds,
      'This Profile is not a supported v4 definition.',
    ));
  }

  const state = nonEmptyString(profile.state);
  if (state === 'retired') {
    return withDisplayName(profileError(basePackId, packIds, 'This Profile is retired.'));
  }
  if (state !== 'resolved') {
    return withDisplayName(profileError(
      basePackId,
      packIds,
      'This Profile needs v4 resolution and activation review.',
    ));
  }

  if (!basePackId) {
    return withDisplayName(profileError(basePackId, packIds, 'The v4 Base Pack is missing.'));
  }
  if (!resolvedReference(profile.catalog_revision)) {
    return withDisplayName(profileError(basePackId, packIds, 'The Profile catalog revision is missing.'));
  }
  if (!base || !resolvedReference(base.artifact_digest) || !resolvedReference(base.definition_revision)) {
    return withDisplayName(profileError(basePackId, packIds, 'The Base Pack is not resolved.'));
  }

  if (profile.mode === 'interactive') {
    const shell = isRecord(profile.shell) ? profile.shell : null;
    if (
      !shell
      || !resolvedReference(shell.artifact_digest)
      || !resolvedReference(shell.definition_revision)
    ) {
      return withDisplayName(profileError(basePackId, packIds, 'The interactive Shell is not resolved.'));
    }
  }

  const unresolvedPack = rawPacks.find((pack) => (
    !isRecord(pack)
    || !nonEmptyString(pack.pack_id)
    || !resolvedReference(pack.artifact_digest)
  ));
  if (unresolvedPack) {
    const unresolvedPackId = isRecord(unresolvedPack)
      ? nonEmptyString(unresolvedPack.pack_id)
      : null;
    return withDisplayName(profileError(
      basePackId,
      packIds,
      `${unresolvedPackId ?? 'A required Pack'} is not resolved.`,
    ));
  }

  return {
    basePackId,
    displayName,
    packIds,
    status: 'ready',
    statusDescription: null,
    statusLabel: 'Ready',
  };
}

function recommendedScore(
  entry: NamedProfileRecord,
  view: NamedProfileView,
  activeProfileId: string | null,
): number {
  return (entry.profile_id === activeProfileId ? 100 : 0)
    + (view.status === 'ready' ? 20 : -10);
}

/** Filter and sort registry records using the original Home ordering model. */
export function filterAndSortNamedProfiles(
  profiles: NamedProfileRecord[],
  query: string,
  sortMode: NamedProfileSortMode,
  activeProfileId: string | null,
): NamedProfileRecord[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filtered = profiles.filter((entry) => {
    if (!normalizedQuery) return true;
    const view = buildNamedProfileView(entry);
    const haystack = [
      view.displayName,
      entry.profile_id,
      view.basePackId ?? '',
      ...view.packIds,
    ].join(' ').toLocaleLowerCase();
    return haystack.includes(normalizedQuery);
  });

  return [...filtered].sort((left, right) => {
    const leftView = buildNamedProfileView(left);
    const rightView = buildNamedProfileView(right);
    if (sortMode === 'name') {
      const nameOrder = leftView.displayName.localeCompare(rightView.displayName);
      if (nameOrder !== 0) return nameOrder;
    } else if (sortMode === 'recent') {
      if (right.updated_at !== left.updated_at) return right.updated_at - left.updated_at;
    } else {
      const scoreOrder = recommendedScore(right, rightView, activeProfileId)
        - recommendedScore(left, leftView, activeProfileId);
      if (scoreOrder !== 0) return scoreOrder;
    }

    if (right.updated_at !== left.updated_at) return right.updated_at - left.updated_at;
    if (left.order !== right.order) return left.order - right.order;
    return left.profile_id.localeCompare(right.profile_id);
  });
}
