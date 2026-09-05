import assert from 'node:assert/strict';
import test from 'node:test';

import {exactActivePackJoin, exactPackControlCatalogBinding} from './NodeManager';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const pack = {
  id: 'provider-pack',
  version: '1.2.3',
  artifactDigest: digest('a'),
  installed: true,
  enabled: true,
  approved: true,
  required: false,
  profileRevision: digest('b'),
  planDigest: digest('c'),
};

const activeRow = {
  pack_id: pack.id,
  version: pack.version,
  artifact_digest: pack.artifactDigest,
  installed: pack.installed,
  enabled: pack.enabled,
  approved: pack.approved,
  required: pack.required,
};

test('Node Manager requires the Pack control catalog to match the accepted Profile snapshot', () => {
  assert.equal(exactPackControlCatalogBinding([pack], {
    profile_revision: pack.profileRevision,
    plan_digest: pack.planDigest,
  }), true);
  assert.equal(exactPackControlCatalogBinding([pack], {
    profile_revision: digest('d'),
    plan_digest: pack.planDigest,
  }), false);
  assert.equal(exactPackControlCatalogBinding([], {
    profile_revision: pack.profileRevision,
    plan_digest: pack.planDigest,
  }), false);
});

test('Node Manager locks an active lifecycle control on artifact or state drift', () => {
  assert.equal(exactActivePackJoin(pack, activeRow), true);
  assert.equal(exactActivePackJoin(pack, {...activeRow, artifact_digest: digest('d')}), false);
  assert.equal(exactActivePackJoin(pack, {...activeRow, enabled: false}), false);
  assert.equal(exactActivePackJoin(pack, undefined), false);
});
