import assert from 'node:assert/strict';
import test from 'node:test';

import type { ApiDynamicFrontendCatalog, ApiPresentationCatalog } from './apiTypes';
import {
  checkShellCompatibility,
  compatibleShellProviders,
  defaultPresentationSelection,
  isVerifiedDynamicFrontendCatalog,
  isConversationCapabilityReady,
  launchDisabledReason,
  launchDisabledReasonForSelection,
  normalizePresentationSelection,
  resolveVerifiedViewCapability,
  selectShellAfterBaseChange,
  verifiedViewCapabilities,
} from './presentation';

const conversationCatalog = (): ApiDynamicFrontendCatalog => ({
  version: 'rumi.ui.contribution.v1',
  profile_id: 'research-a',
  profile_revision: `sha256:${'a'.repeat(64)}`,
  activation_id: 'activation:research-a',
  plan_hash: `sha256:${'a'.repeat(64)}`,
  contributions: [{
    contribution_id: 'research.conversation.complete',
    kind: 'route',
    mode: 'declarative',
    label: 'Research conversation',
    owner_pack_id: 'research-ui-pack',
    owner_pack_hash: `sha256:${'b'.repeat(64)}`,
    build_identity: 'research-ui-build',
    resolved_profile_id: 'research-a',
    resolved_profile_revision: `sha256:${'a'.repeat(64)}`,
    resolved_activation_id: 'activation:research-a',
    resolved_plan_hash: `sha256:${'a'.repeat(64)}`,
    descriptor_hash: `sha256:${'c'.repeat(64)}`,
    route: '/research/conversation',
    action_contract: 'conversation.turn.v1',
    operation_id: 'complete',
    provider_id: 'research-conversation-provider',
    function_id: 'research-conversation-function',
    view: {type: 'conversation_v4'},
  }],
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: `sha256:${'d'.repeat(64)}`,
});

const approval = {
  state: 'verified' as const,
  provider_trust: 'verified' as const,
  grant_state: 'not_minted' as const,
  authority_mode: 'lease_only' as const,
  execution_domain: 'test-shell',
  effect_scope: ['app.shell.v1'],
  blast_radius: 'Brokered only',
};

const catalog: ApiPresentationCatalog = {
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
  base_packs: [
    {
      pack_id: 'defaults-basepack',
      display_name: 'Defaults Base Pack',
      version: '4.0.0',
      artifact_digest: 'sha256:base',
      backend_provider_ids: ['defaultspack'],
      state_owners: ['defaultspack.state'],
      backend_identity_digest: 'sha256:' + '3'.repeat(64),
      required_capabilities: ['navigation', 'commands', 'notifications'],
      allowed_families: ['graphical', 'terminal'],
      approval: {...approval, authority_mode: 'none'},
    },
  ],
  shell_providers: [
    {
      provider_id: 'shell.tauri.default',
      display_name: 'Tauri Desktop',
      contract_id: 'app.shell.v1',
      contract_revision_digest: 'sha256:shell',
      experience_role: 'shell',
      presentation_kind: 'packaged_process',
      presentation_family: 'graphical',
      technology: 'tauri',
      capabilities: ['navigation', 'commands', 'notifications', 'rich_text', 'windows'],
      consumes_contracts: ['ui.route.contribution.v1', 'ui.panel.contribution.v1'],
      contributions: [],
      artifact_variants: [],
      artifact: null,
      approval,
      protocol_revision_digest: null,
    },
    {
      provider_id: 'shell.cli.default',
      display_name: 'CLI',
      contract_id: 'app.shell.v1',
      contract_revision_digest: 'sha256:shell',
      experience_role: 'shell',
      presentation_kind: 'terminal_stdio',
      presentation_family: 'terminal',
      technology: 'native',
      capabilities: ['navigation', 'commands', 'notifications'],
      consumes_contracts: ['cli.command.contribution.v1', 'cli.renderer.contribution.v1'],
      contributions: [],
      artifact_variants: [],
      artifact: null,
      approval,
      protocol_revision_digest: 'sha256:' + '2'.repeat(64),
    },
  ],
};

test('compatible Shell selection is Contract- and capability-based', () => {
  assert.deepEqual(
    compatibleShellProviders(catalog, 'defaults-basepack').map((shell) => shell.provider_id),
    ['shell.tauri.default', 'shell.cli.default'],
  );
  assert.equal(
    checkShellCompatibility(catalog.base_packs[0], catalog.shell_providers[0]).compatible,
    true,
  );
});

test('selection does not silently retain an incompatible Shell after Base change', () => {
  const next = selectShellAfterBaseChange(catalog, 'defaults-basepack', 'shell.missing');
  assert.deepEqual(next, {
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
  });
});

test('invalid saved selection is normalized to a compatible exact provider', () => {
  assert.deepEqual(
    normalizePresentationSelection(catalog, {
      base_pack_id: 'defaults-basepack',
      shell_provider_id: 'shell.not-installed',
    }),
    defaultPresentationSelection(catalog),
  );
});

test('new-setup default comes from the generated catalog selection, not array order', () => {
  const reordered = {
    ...catalog,
    shell_providers: [...catalog.shell_providers].reverse(),
  };
  assert.deepEqual(defaultPresentationSelection(reordered), reordered.default_selection);
});

test('an invalid generated default is not replaced with an arbitrary provider', () => {
  const invalid = {
    ...catalog,
    default_selection: {
      base_pack_id: 'defaults-basepack',
      shell_provider_id: 'shell.missing',
    },
  };
  assert.equal(defaultPresentationSelection(invalid), null);
});

test('launch remains blocked until a verified materialization exists', () => {
  assert.match(
    launchDisabledReason({
      status: 'blocked',
      base_pack_id: 'defaults-basepack',
      shell_provider_id: 'shell.tauri.default',
      selected_contributions: [],
      artifact: null,
      reason: 'No verified prebuilt artifact is installed.',
    }) ?? '',
    /No verified prebuilt artifact/,
  );
  assert.match(
    launchDisabledReasonForSelection(
      {
        status: 'materialized',
        base_pack_id: 'defaults-basepack',
        shell_provider_id: 'shell.tauri.default',
        selected_contributions: [],
        artifact: null,
        reason: null,
      },
      {
        base_pack_id: 'defaults-basepack',
        shell_provider_id: 'shell.tauri.default',
      },
      {
        base_pack_id: 'defaults-basepack',
        shell_provider_id: 'shell.cli.default',
      },
    ) ?? '',
    /Save the current Base Pack and Shell selection/,
  );
  assert.equal(
    launchDisabledReason({
      status: 'materialized',
      base_pack_id: 'defaults-basepack',
      shell_provider_id: 'shell.tauri.default',
      selected_contributions: [],
      artifact: null,
      reason: null,
    }),
    null,
  );
});

test('Conversation readiness requires one exact live capability binding', () => {
  assert.equal(isConversationCapabilityReady(conversationCatalog()), true);

  const tamperCases: Array<[string, (candidate: ApiDynamicFrontendCatalog) => void]> = [
    ['version', (candidate) => { candidate.version = 'other'; }],
    ['profile id', (candidate) => { candidate.profile_id = ''; }],
    ['profile revision', (candidate) => { candidate.profile_revision = 'sha256:short'; }],
    ['activation id', (candidate) => { candidate.activation_id = ''; }],
    ['plan hash', (candidate) => { candidate.plan_hash = 'sha256:short'; }],
    ['catalog hash', (candidate) => { candidate.catalog_hash = 'sha256:short'; }],
    ['quarantine', (candidate) => { candidate.quarantined_pack_ids = ['research-ui-pack']; }],
    ['kind', (candidate) => { candidate.contributions[0].kind = 'action'; }],
    ['mode', (candidate) => { candidate.contributions[0].mode = 'same_origin_builtin'; }],
    ['missing route', (candidate) => { candidate.contributions[0].route = ''; }],
    ['missing owner', (candidate) => { candidate.contributions[0].owner_pack_id = ''; }],
    ['contract', (candidate) => { candidate.contributions[0].action_contract = 'other.v1'; }],
    ['operation', (candidate) => { candidate.contributions[0].operation_id = 'other'; }],
    ['missing provider', (candidate) => { candidate.contributions[0].provider_id = ''; }],
    ['missing function', (candidate) => { candidate.contributions[0].function_id = ''; }],
    ['missing build', (candidate) => { candidate.contributions[0].build_identity = ''; }],
    ['owner hash', (candidate) => { candidate.contributions[0].owner_pack_hash = ''; }],
    ['descriptor hash', (candidate) => { candidate.contributions[0].descriptor_hash = ''; }],
    ['profile binding', (candidate) => {
      candidate.contributions[0].resolved_profile_id = 'other-profile';
    }],
    ['profile revision binding', (candidate) => {
      candidate.contributions[0].resolved_profile_revision = 'sha256:stale';
    }],
    ['activation binding', (candidate) => {
      candidate.contributions[0].resolved_activation_id = 'activation:stale';
    }],
    ['plan binding', (candidate) => {
      candidate.contributions[0].resolved_plan_hash = 'sha256:stale';
    }],
    ['view', (candidate) => { candidate.contributions[0].view = {type: 'other'}; }],
  ];
  for (const [label, tamper] of tamperCases) {
    const candidate = structuredClone(conversationCatalog());
    tamper(candidate);
    assert.equal(isConversationCapabilityReady(candidate), false, label);
  }

  const duplicate = conversationCatalog();
  duplicate.contributions.push({...duplicate.contributions[0]});
  assert.equal(isConversationCapabilityReady(duplicate), false);
});

test('Conversation is an optional Profile capability resolved from live bindings', () => {
  const catalog = conversationCatalog();
  const capability = resolveVerifiedViewCapability(catalog, {
    viewType: 'conversation_v4',
    actionContract: 'conversation.turn.v1',
    operationId: 'complete',
  });

  assert.equal(isVerifiedDynamicFrontendCatalog(catalog), true);
  assert.equal(capability?.owner_pack_id, 'research-ui-pack');
  assert.equal(capability?.route, '/research/conversation');
  assert.equal(
    verifiedViewCapabilities({...catalog, quarantined_pack_ids: ['unrelated-pack']}, {
      viewType: 'conversation_v4',
      actionContract: 'conversation.turn.v1',
      operationId: 'complete',
    }).length,
    1,
  );
  assert.equal(
    isConversationCapabilityReady({...catalog, contributions: []}),
    false,
  );
});
