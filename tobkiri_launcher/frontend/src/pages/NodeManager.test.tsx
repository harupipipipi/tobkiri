import assert from 'node:assert/strict';
import test from 'node:test';

import {
  capabilityDomains,
  capabilityPackId,
  capabilityProfileForStartup,
} from './NodeManager';

test('capabilityPackId uses Pack metadata before the node identifier fallback', () => {
  assert.equal(capabilityPackId({
    node_id: 'defaultspack.chat',
    kind: 'tool',
    ports: [],
    bindings: {},
    metadata: { pack_id: 'custom-pack' },
  }), 'custom-pack');

  assert.equal(capabilityPackId({
    node_id: 'defaultspack.chat',
    kind: 'tool',
    ports: [],
    bindings: {},
    metadata: {},
  }), 'defaultspack');
});

test('capabilityDomains normalizes domains declared by node metadata and requirements', () => {
  assert.deepEqual(capabilityDomains({
    node_id: 'defaultspack.browser',
    kind: 'tool',
    ports: [],
    bindings: {},
    metadata: { allowed_domains: ['example.com'] },
    requirements: { network: { domains: ['api.example.com', 'example.com'] } },
  }), ['api.example.com', 'example.com']);
});

test('capabilityProfileForStartup keeps Pack Nodes behind the startup profile bridge', () => {
  const startupProfile = {
    profile_id: 'defaults-profile',
    capability_profile_id: 'defaultspack.startup',
  } as never;
  const capabilityProfiles = [
    { profile_id: 'defaultspack.startup' },
    { profile_id: 'other.profile' },
  ] as never;

  assert.equal(
    capabilityProfileForStartup(startupProfile, capabilityProfiles),
    'defaultspack.startup',
  );
});
