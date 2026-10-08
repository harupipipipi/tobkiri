import assert from 'node:assert/strict';
import {test} from 'node:test';
import {parseProfilePackVersions} from './profilePackVersions';
const digest = (character: string) => `sha256:${character.repeat(64)}`;
const view = () => ({
  profile_id: 'my-profile', profile_definition_revision: digest('a'), catalog_revision: digest('b'), store_generation: 3,
  packs: [{pack_id: 'some-pack', role: 'provider', selected_digest: digest('c'), versions: [
    {artifact_digest: digest('c'), version: '1.0.0', origin: 'bundled', capabilities: [], contracts: []},
    {artifact_digest: digest('d'), version: '2.0.0', origin: 'signed-admission', publisher_id: 'publisher.example', key_id: 'e'.repeat(32), capabilities: [], contracts: []},
  ]}],
});
test('retained revisions require an exact selected digest and finite identities', () => {
  assert.deepEqual(parseProfilePackVersions(view()), view());
  const missing = view(); missing.packs[0].selected_digest = digest('f');
  assert.throws(() => parseProfilePackVersions(missing), /invalid/);
  const duplicate = view(); duplicate.packs[0].versions.push(duplicate.packs[0].versions[0]);
  assert.throws(() => parseProfilePackVersions(duplicate), /invalid/);
  assert.throws(() => parseProfilePackVersions({...view(), approved: true}), /invalid/);
  assert.throws(() => parseProfilePackVersions({...view(), profile_id: '../profile'}), /invalid/);
  assert.throws(() => parseProfilePackVersions({...view(), store_generation: -1}), /invalid/);
});
test('optional revision intent is finite and carries no enablement or approval', () => {
  const optional = view(); optional.packs[0].role = 'optional';
  assert.deepEqual(parseProfilePackVersions(optional), optional);
  assert.throws(() => parseProfilePackVersions({...optional, packs: [{...optional.packs[0], enabled: true}]}), /invalid/);
  optional.packs[0].role = 'host';
  assert.throws(() => parseProfilePackVersions(optional), /invalid/);
});
