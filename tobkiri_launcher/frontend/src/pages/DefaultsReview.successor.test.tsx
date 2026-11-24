import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import React from 'react';
import {renderToString} from 'react-dom/server';
import {parseDefaultsSetupState} from '../lib/defaultsSetup';
import {DefaultsReview} from './DefaultsReview';

function fixture() {
  const setup = JSON.parse(readFileSync(new URL(
    '../../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json',
    import.meta.url,
  ), 'utf8'));
  setup.recommended_default_profile.confirmation.definition_predecessor = {
    profile_revision: `sha256:${'1'.repeat(64)}`,
    definition_digest: `sha256:${'1'.repeat(64)}`,
    store_generation: 2,
    snapshot_digest: `sha256:${'2'.repeat(64)}`,
    handover_digest: `sha256:${'3'.repeat(64)}`,
    display_name: 'Imported custom Defaults',
    pack_ids: ['old.extra', ...setup.recommended_default_profile.pack_ids.slice(1)],
    base_pack_id: 'defaults-basepack',
    shell_pack_id: 'shell.tauri.default',
    shell_provider_id: 'shell.tauri.default',
    shell_artifact_digest: `sha256:${'4'.repeat(64)}`,
    shell_executable_artifact_digest: `sha256:${'5'.repeat(64)}`,
    shell_definition_revision: `sha256:${'6'.repeat(64)}`,
  };
  return setup;
}

test('successor review keeps the exact predecessor and explicitly presents selection changes', () => {
  const value = fixture();
  const setup = parseDefaultsSetupState(value);
  assert.deepEqual(setup.recommended_default_profile.confirmation.definition_predecessor,
    value.recommended_default_profile.confirmation.definition_predecessor);
  const html = renderToString(<DefaultsReview
    setup={setup} reviewed={false} activating={false} error={null}
    onReviewedChange={() => undefined} onActivate={() => undefined}
  />).replaceAll('<!-- -->', '');
  assert.match(html, /Imported custom Defaults/);
  assert.match(html, /Successor name: Tobkiri Defaults/);
  assert.match(html, /immutable history/);
  assert.match(html, /operation bindings, and scopes are replaced/);
  assert.match(html, /Added Packs:/);
  assert.match(html, /Removed Packs: old.extra/);
  assert.match(html, /Imported Shell: shell.tauri.default/);
  assert.match(html, /Imported predecessor revision/);
  assert.match(html, /Completed handover digest/);
  assert.match(html, /I also confirm the displayed imported predecessor/);
  assert.match(html, /disabled=""/);
});

test('successor confirmation remains disabled after a commit or denial', () => {
  const value = fixture();
  value.state = 'activation_denied';
  value.denial_diagnostic = 'Registry changed';
  const html = renderToString(<DefaultsReview
    setup={parseDefaultsSetupState(value)} reviewed={true} activating={false}
    activationCommitted={true} error={null}
    onReviewedChange={() => undefined} onActivate={() => undefined}
  />).replaceAll('<!-- -->', '');
  assert.match(html, /Registry changed/);
  assert.match(html, /disabled=""/);
});

test('strict predecessor contract rejects injected authority and malformed bindings', () => {
  for (const [key, invalid] of [
    ['approved', true], ['store_generation', 1.5], ['handover_digest', 'missing'],
    ['pack_ids', ['duplicate', 'duplicate']], ['base_pack_id', '../escape'],
    ['profile_revision', `sha256:${'0'.repeat(64)}`],
  ] as const) {
    const value = fixture();
    value.recommended_default_profile.confirmation.definition_predecessor[key] = invalid;
    assert.throws(() => parseDefaultsSetupState(value));
  }
  const absent = fixture();
  delete absent.recommended_default_profile.confirmation.definition_predecessor.snapshot_digest;
  assert.throws(() => parseDefaultsSetupState(absent), /missing fields/);
});
