import assert from 'node:assert/strict';
import test from 'node:test';

import {
  PROFILE_REGISTRY_API_VERSION,
  ProfileRegistryContractError,
  parseNamedProfileRegistry,
  validateNamedProfileMutation,
} from './profileRegistry';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function profileRecord(
  profileId: string,
  revision: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    profile_id: profileId,
    profile_revision: revision,
    profile: {profile_id: profileId, display_name: profileId},
    order: 0,
    parent_revision: null,
    tombstone: false,
    created_at: 1,
    updated_at: 1,
    legacy_ids: [],
    ...overrides,
  };
}

function registry(
  profiles: Record<string, unknown>[],
  activeProfileId: string | null = (profiles[0]?.profile_id as string | null) ?? null,
  activeProfileRevision?: string | null,
): Record<string, unknown> {
  const active = profiles.find((profile) => profile.profile_id === activeProfileId);
  return {
    profile_registry_api_version: PROFILE_REGISTRY_API_VERSION,
    generation: 3,
    active_profile_id: activeProfileId,
    active_profile_revision: activeProfileRevision === undefined
      ? active?.profile_revision ?? null
      : activeProfileRevision,
    profiles,
  };
}

test('Named Profile registry parser preserves definition records and separate active runtime pointer', () => {
  const parsed = parseNamedProfileRegistry(registry([
    profileRecord('work-a', digest('a')),
    profileRecord('work-b', digest('b'), {order: 1, parent_revision: digest('a')}),
  ], 'work-b'));

  assert.equal(parsed.profile_registry_api_version, PROFILE_REGISTRY_API_VERSION);
  assert.equal(parsed.active_profile_id, 'work-b');
  assert.equal(parsed.active_profile_revision, digest('b'));
  assert.deepEqual(parsed.profiles.map((profile) => profile.profile_id), ['work-a', 'work-b']);
  assert.equal(parsed.profiles[1]?.parent_revision, digest('a'));
});

test('Named Profile registry accepts a resolved active revision distinct from the definition revision', () => {
  const parsed = parseNamedProfileRegistry(registry([
    profileRecord('work-a', digest('a')),
  ], 'work-a', digest('e')));

  assert.equal(parsed.profiles[0]?.profile_revision, digest('a'));
  assert.equal(parsed.active_profile_id, 'work-a');
  assert.equal(parsed.active_profile_revision, digest('e'));
});

test('Named Profile registry parser rejects malformed identity, active pointers, and tombstone leaks', () => {
  const cases: Array<[string, Record<string, unknown>]> = [
    ['duplicate IDs', registry([profileRecord('work-a', digest('a')), profileRecord('work-a', digest('b'))])],
    ['unknown active Profile', {...registry([profileRecord('work-a', digest('a'))]), active_profile_id: 'missing'}],
    ['tombstone in live list', registry([profileRecord('work-a', digest('a'), {tombstone: true})])],
    ['mismatched profile document ID', registry([profileRecord('work-a', digest('a'), {profile: {profile_id: 'other'}})])],
    ['unexpected response field', {...registry([profileRecord('work-a', digest('a'))]), defaults: true}],
  ];

  for (const [label, value] of cases) {
    assert.throws(
      () => parseNamedProfileRegistry(value),
      ProfileRegistryContractError,
      label,
    );
  }
});

test('Named Profile mutations accept only exact typed optimistic-concurrency payloads', () => {
  assert.deepEqual(
    validateNamedProfileMutation('create', {
      profile_id: 'work-a',
      display_name: 'Work A',
      source_profile_id: 'defaults',
      expected_store_generation: 3,
    }),
    {
      profile_id: 'work-a',
      display_name: 'Work A',
      source_profile_id: 'defaults',
      expected_store_generation: 3,
    },
  );
  assert.deepEqual(
    validateNamedProfileMutation('duplicate', {
      profile_id: 'work-a',
      new_profile_id: 'work-b',
      display_name: 'Work B',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
    }),
    {
      profile_id: 'work-a',
      new_profile_id: 'work-b',
      display_name: 'Work B',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
    },
  );
  assert.throws(
    () => validateNamedProfileMutation('update', {
      profile_id: 'work-a',
      display_name: 'Work A',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
      approved: true,
    }),
    /unexpected fields/,
  );
  assert.throws(
    () => validateNamedProfileMutation('delete', {
      profile_id: 'Work A',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
    }),
    /canonical Profile ID/,
  );
});

test('mutation responses retain the tombstone only as changed history, never as a live Profile', () => {
  const parsed = parseNamedProfileRegistry({
    ...registry([profileRecord('work-a', digest('a'))]),
    active_profile_id: null,
    active_profile_revision: null,
    changed_profile: profileRecord('work-b', digest('b'), {
      tombstone: true,
      parent_revision: digest('a'),
    }),
    action: 'delete',
  });

  assert.equal(parsed.profiles.length, 1);
  assert.equal(parsed.changed_profile?.tombstone, true);
  assert.equal(parsed.action, 'delete');
});
