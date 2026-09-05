import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import type {ApiPresentationState} from '@/src/lib/apiTypes';
import {PresentationSelector} from './PresentationSelector';

const approval = {
  state: 'verified' as const,
  provider_trust: 'verified' as const,
  grant_state: 'not_minted' as const,
  authority_mode: 'lease_only' as const,
  execution_domain: 'shell.tauri.default',
  effect_scope: ['app.shell.v1'],
  blast_radius: 'Brokered Contract requests only.',
};

const state: ApiPresentationState = {
  catalog: {
    schema: 'io.tobkiri.launcher.presentation-catalog.v1',
    generator: 'test',
    generator_version: '1.0.0',
    default_profile_id: 'defaults-modern',
    default_profile_source: 'profiles/defaults-modern.profile.yaml',
    default_profile_digest: 'sha256:' + '0'.repeat(64),
    default_selection: {
      base_pack_id: 'defaults-basepack',
      shell_provider_id: 'shell.tauri.default',
    },
    contract_revisions: [],
    source_manifest_digests: {'defaults-basepack': 'sha256:' + '1'.repeat(64)},
    generated_at: 1,
    base_packs: [{
      pack_id: 'defaults-basepack',
      display_name: 'Defaults Base Pack',
      version: '4.0.0',
      artifact_digest: 'sha256:base',
      backend_provider_ids: ['defaultspack'],
      state_owners: ['defaultspack.state'],
      backend_identity_digest: 'sha256:' + '3'.repeat(64),
      required_capabilities: ['navigation', 'commands'],
      allowed_families: ['graphical', 'terminal'],
      approval: {...approval, authority_mode: 'none'},
    }],
    shell_providers: [{
      provider_id: 'shell.tauri.default',
      display_name: 'Tauri Desktop',
      contract_id: 'app.shell.v1',
      contract_revision_digest: 'sha256:shell',
      experience_role: 'shell',
      presentation_kind: 'packaged_process',
      presentation_family: 'graphical',
      technology: 'tauri',
      capabilities: ['navigation', 'commands'],
      consumes_contracts: ['ui.route.contribution.v1', 'ui.panel.contribution.v1'],
      contributions: [],
      artifact_variants: [],
      artifact: {
        artifact_id: 'shell-tauri-default',
        variant: 'test',
        platform: 'test',
        architecture: 'test',
        path: null,
        sha256: null,
        prebuilt: true,
        production: true,
        development_command: null,
        bundle_identifier: null,
        status: 'missing',
        status_detail: 'The verified production artifact is not installed.',
      },
      approval,
      protocol_revision_digest: null,
    }],
  },
  selection: {
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
  },
  materialization: {
    status: 'blocked',
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
    selected_contributions: [],
    artifact: null,
    reason: 'The verified production artifact is not installed.',
  },
};

const verifiedArtifact = {
  ...state.catalog.shell_providers[0].artifact!,
  path: 'bundled/presentation-artifacts/shell.tauri.default.test/Tobkiri.app',
  sha256: 'sha256:' + '4'.repeat(64),
  status: 'verified' as const,
  status_detail: 'Pinned digest, prebuilt status, and production metadata verified.',
};

const verifiedState: ApiPresentationState = {
  ...state,
  catalog: {
    ...state.catalog,
    shell_providers: state.catalog.shell_providers.map((shell) => ({
      ...shell,
      artifact: verifiedArtifact,
    })),
  },
  materialization: {
    ...state.materialization,
    status: 'materialized',
    artifact: verifiedArtifact,
    reason: null,
  },
};

test('PresentationSelector exposes exact selection and blocks unverified launch', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root: Root = createRoot(container);
  const saved: Array<{base_pack_id: string; shell_provider_id: string}> = [];
  const changed: Array<{base_pack_id: string; shell_provider_id: string}> = [];
  let launches = 0;

  await act(async () => {
    root.render(
      <PresentationSelector
        state={state}
        selection={state.selection}
        onSelectionChange={(next) => {
          if (next) changed.push(next);
        }}
        onSave={(selection) => { saved.push(selection); }}
        onLaunch={() => { launches += 1; }}
      />,
    );
  });

  try {
    const baseButton = container.querySelector<HTMLButtonElement>('[data-testid="base-pack-defaults-basepack"]');
    const shellButton = container.querySelector<HTMLButtonElement>(
      '[data-testid="shell-provider-shell.tauri.default"]',
    );
    const saveButton = container.querySelector<HTMLButtonElement>('[data-testid="save-presentation"]');
    const launchButton = container.querySelector<HTMLButtonElement>('[data-testid="launch-presentation"]');
    assert.ok(baseButton);
    assert.ok(shellButton);
    assert.ok(saveButton);
    assert.ok(launchButton);
    assert.match(container.textContent ?? '', /defaults-modern/);
    assert.match(container.textContent ?? '', /Profile SHA-256/);
    assert.match(container.textContent ?? '', /Backend identity SHA-256/);
    assert.equal(
      container.querySelector('[data-testid="default-profile-digest"]')?.textContent?.includes(
        state.catalog.default_profile_digest,
      ),
      true,
    );
    assert.equal(
      container.querySelector('[data-testid="backend-identity-digest"]')?.textContent?.includes(
        state.catalog.base_packs[0].backend_identity_digest,
      ),
      true,
    );
    assert.equal(baseButton.getAttribute('aria-pressed'), 'true');
    assert.equal(shellButton.getAttribute('aria-pressed'), 'true');
    assert.equal(saveButton.disabled, true);
    assert.equal(launchButton.disabled, true);
    assert.match(container.textContent ?? '', /Launch blocked/);

    await act(async () => shellButton.click());
    assert.deepEqual(changed, [state.selection]);

    await act(async () => saveButton.click());
    assert.deepEqual(saved, []);

    await act(async () => {
      root.render(
        <PresentationSelector
          state={verifiedState}
          selection={verifiedState.selection}
          onSelectionChange={() => undefined}
          onSave={() => undefined}
          onLaunch={() => { launches += 1; }}
        />,
      );
    });
    const verifiedSaveButton = container.querySelector<HTMLButtonElement>('[data-testid="save-presentation"]');
    const verifiedLaunchButton = container.querySelector<HTMLButtonElement>('[data-testid="launch-presentation"]');
    assert.ok(verifiedSaveButton);
    assert.ok(verifiedLaunchButton);
    assert.equal(verifiedSaveButton.disabled, false);
    assert.equal(verifiedLaunchButton.disabled, false);
    await act(async () => verifiedLaunchButton.click());
    assert.equal(launches, 1);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
  }
});
