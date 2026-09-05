import assert from 'node:assert/strict';
import test from 'node:test';

import type {NamedProfileRecord} from './profileRegistry';
import {
  buildNamedProfileView,
  filterAndSortNamedProfiles,
} from './profileRegistryView';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function profileRecord(
  profileId: string,
  displayName: string,
  state: 'needs_resolution' | 'resolved' | 'retired',
  order: number,
  updatedAt: number,
): NamedProfileRecord {
  const resolved = state === 'resolved';
  return {
    profile_id: profileId,
    profile_revision: digest(profileId === 'alpha' ? 'a' : 'b'),
    profile: {
      profile_id: profileId,
      profile_api_version: 'io.tobkiri.profile.v4',
      display_name: displayName,
      state,
      mode: 'interactive',
      catalog_revision: resolved ? digest('1') : null,
      base: {
        pack_id: 'defaultspack',
        artifact_digest: resolved ? digest('c') : null,
        definition_revision: resolved ? digest('d') : null,
      },
      shell: resolved ? {
        artifact_digest: digest('e'),
        definition_revision: digest('f'),
      } : null,
      packs: [{pack_id: 'defaultspack', artifact_digest: resolved ? digest('2') : null}],
    },
    order,
    parent_revision: null,
    tombstone: false,
    created_at: 1,
    updated_at: updatedAt,
    legacy_ids: [],
  };
}

test('v4 Profile documents expose honest ready and error states', () => {
  const ready = buildNamedProfileView(profileRecord('alpha', 'Alpha', 'resolved', 0, 1));
  assert.equal(ready.status, 'ready');
  assert.equal(ready.basePackId, 'defaultspack');
  assert.deepEqual(ready.packIds, ['defaultspack']);

  const unresolved = buildNamedProfileView(profileRecord('beta', 'Beta', 'needs_resolution', 1, 2));
  assert.equal(unresolved.status, 'error');
  assert.match(unresolved.statusDescription ?? '', /resolution/);

  const retired = buildNamedProfileView(profileRecord('gamma', 'Gamma', 'retired', 2, 3));
  assert.equal(retired.status, 'error');
  assert.match(retired.statusDescription ?? '', /retired/i);
});

test('Profile search covers IDs, names, base Packs, and original recommended ordering', () => {
  const profiles = [
    profileRecord('unresolved', 'Research', 'needs_resolution', 0, 100),
    profileRecord('ready-work', 'Work', 'resolved', 1, 10),
    profileRecord('ready-defaults', 'Defaults', 'resolved', 2, 1),
  ];

  assert.deepEqual(
    filterAndSortNamedProfiles(profiles, 'research', 'recommended', null).map((entry) => entry.profile_id),
    ['unresolved'],
  );
  assert.deepEqual(
    filterAndSortNamedProfiles(profiles, 'defaultspack', 'name', null).map((entry) => entry.profile_id),
    ['ready-defaults', 'unresolved', 'ready-work'],
  );
  assert.deepEqual(
    filterAndSortNamedProfiles(profiles, '', 'recommended', 'ready-defaults').map((entry) => entry.profile_id),
    ['ready-defaults', 'ready-work', 'unresolved'],
  );
  assert.deepEqual(
    filterAndSortNamedProfiles(profiles, '', 'recent', null).map((entry) => entry.profile_id),
    ['unresolved', 'ready-work', 'ready-defaults'],
  );
});
