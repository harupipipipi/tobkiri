import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isPackInCatalogScope,
  parsePacksResponse,
} from './packScope';
import type {PackControlBinding} from './apiTypes';

const binding: PackControlBinding = {
  profile_id: 'profile-a',
  workspace_id: 'workspace-a',
  profile_revision: 'sha256:profile-a',
  plan_digest: 'sha256:plan-a',
  catalog_revision: 'catalog-a',
};

const row = {
  ...binding,
  pack_id: 'research-pack',
};

test('Pack catalog parser preserves the authoritative active Profile binding', () => {
  const parsed = parsePacksResponse({
    ...binding,
    packs: [row],
    count: 1,
  });

  assert.equal(parsed.profile_id, binding.profile_id);
  assert.equal(parsed.profile_revision, binding.profile_revision);
  assert.equal(parsed.packs[0]?.profile_id, binding.profile_id);
});

test('Pack catalog parser rejects rows mixed across Profile or revision scope', () => {
  assert.throws(
    () => parsePacksResponse({
      ...binding,
      packs: [{...row, profile_id: 'profile-b'}],
      count: 1,
    }),
    /different Profile scope/,
  );
  assert.throws(
    () => parsePacksResponse({
      ...binding,
      packs: [{...row, plan_digest: 'sha256:plan-b'}],
      count: 1,
    }),
    /different Profile scope/,
  );
  assert.throws(
    () => parsePacksResponse({...binding, packs: [], count: 1}),
    /count that does not match/,
  );
});

test('Pack row scope matching is fail-closed for every v4 binding field', () => {
  const pack = {
    profileId: binding.profile_id,
    workspaceId: binding.workspace_id,
    profileRevision: binding.profile_revision,
    planDigest: binding.plan_digest,
    catalogRevision: binding.catalog_revision,
  };

  assert.equal(isPackInCatalogScope(pack, binding), true);
  assert.equal(isPackInCatalogScope({...pack, catalogRevision: 'catalog-b'}, binding), false);
  assert.equal(isPackInCatalogScope(pack, null), false);
});
