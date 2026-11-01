import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import React from 'react';
import {renderToString} from 'react-dom/server';

import {parseDefaultsSetupState} from '../lib/defaultsSetup';
import {DefaultsReview} from './DefaultsReview';

function fixture() {
  const value = JSON.parse(readFileSync(new URL(
    '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
    import.meta.url,
  ), 'utf8'));
  value.recommended_default_profile.confirmation.pack_pin_predecessor = {
    schema: 'io.tobkiri.defaults-pack-pin-predecessor.v1',
    pointer_digest: `sha256:${'1'.repeat(64)}`,
    profile_id: 'defaults',
    profile_revision: `sha256:${'2'.repeat(64)}`,
    activation_id: 'activation:defaults-current-version',
    plan_digest: `sha256:${'3'.repeat(64)}`,
    lock_digest: `sha256:${'4'.repeat(64)}`,
    generation: 2,
    active_definition_digest: `sha256:${'7'.repeat(64)}`,
    registry_profile_revision: `sha256:${'5'.repeat(64)}`,
    registry_store_generation: 3,
    registry_snapshot_digest: `sha256:${'6'.repeat(64)}`,
  };
  return value;
}

test('saved Pack version review retains and presents its exact current activation binding', () => {
  const value = fixture();
  const setup = parseDefaultsSetupState(value);
  assert.deepEqual(setup.recommended_default_profile.confirmation.pack_pin_predecessor,
    value.recommended_default_profile.confirmation.pack_pin_predecessor);
  const html = renderToString(<DefaultsReview setup={setup} reviewed={false}
    activating={false} error={null} reconfirmationRequired={true}
    onReviewedChange={() => undefined} onActivate={() => undefined}
  />).replaceAll('<!-- -->', '');
  assert.match(html, /Review saved Pack versions/);
  assert.match(html, /activation:defaults-current-version/);
  assert.match(html, /Saved Pack version revision/);
  assert.match(html, /I also confirm these saved Pack versions/);
  assert.match(html, /disabled=""/);
});

test('saved Pack version predecessor rejects missing, malformed, and injected authority fields', () => {
  for (const [field, invalid] of [
    ['schema', 'unsupported'], ['pointer_digest', 'not-a-digest'],
    ['profile_id', 'other-profile'], ['activation_id', '../escape'],
    ['generation', 0], ['registry_store_generation', 1.5],
    ['registry_profile_revision', null], ['approved', true],
  ] as const) {
    const value = fixture();
    value.recommended_default_profile.confirmation.pack_pin_predecessor[field] = invalid;
    assert.throws(() => parseDefaultsSetupState(value), undefined, field);
  }
  const missing = fixture();
  delete missing.recommended_default_profile.confirmation.pack_pin_predecessor.registry_snapshot_digest;
  assert.throws(() => parseDefaultsSetupState(missing), /missing fields/);
  const mixed = fixture();
  mixed.recommended_default_profile.confirmation.definition_predecessor = {};
  assert.throws(() => parseDefaultsSetupState(mixed), /conflicting predecessor/);
});
