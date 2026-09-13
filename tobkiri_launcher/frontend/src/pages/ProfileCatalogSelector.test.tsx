import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test, {afterEach, beforeEach} from 'node:test';
import {MemoryRouter} from 'react-router';

import {ProfileCatalogSelector} from '@/src/components/advanced/ProfileCatalogSelector';
import type {ApiDynamicFrontendCatalog} from '@/src/lib/apiTypes';
import type {
  ProfileActivateResult,
  ProfileApproveResult,
  ProfileCeremonyClient,
  ProfileResolveResult,
  ProfileReviewResult,
} from '@/src/lib/profileCeremony';
import {
  RUNTIME_SURFACE_API_VERSION,
  RuntimeSurfaceError,
  type RuntimeProfileCatalogEntry,
  type RuntimeProfileCatalogProjection,
  type RuntimeSurfaceEnvelope,
} from '@/src/lib/runtimeSurface';
import type {RuntimeSurfaceState} from '@/src/hooks/useRuntimeSurface';
import {useAppStore, type Pack} from '@/src/store';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function optionalConversationCatalog(profileId = 'defaults', include = true): ApiDynamicFrontendCatalog {
  return {
    version: 'rumi.ui.contribution.v1',
    profile_id: profileId,
    profile_revision: digest('a'),
    activation_id: `activation:${profileId}`,
    plan_hash: digest('b'),
    contributions: include ? [{
      contribution_id: `${profileId}.conversation.complete`,
      owner_pack_id: `${profileId}-ui-pack`,
      label: `${profileId} conversation`,
      action_contract: 'conversation.turn.v1',
      operation_id: 'complete',
      provider_id: `${profileId}-conversation-provider`,
      function_id: `${profileId}-conversation-function`,
      kind: 'route',
      mode: 'declarative',
      route: `/${profileId}/conversation`,
      owner_pack_hash: digest('c'),
      build_identity: `${profileId}-conversation-build`,
      resolved_profile_id: profileId,
      resolved_profile_revision: digest('a'),
      resolved_activation_id: `activation:${profileId}`,
      resolved_plan_hash: digest('b'),
      descriptor_hash: digest('d'),
      view: {type: 'conversation_v4'},
    }] : [],
    diagnostics: [],
    quarantined_pack_ids: [],
    catalog_hash: digest('e'),
  };
}

function profileSnapshot(): RuntimeSurfaceEnvelope<unknown> {
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: 'profile',
    state: 'ready',
    profile_id: 'defaults',
    profile_revision: digest('a'),
    plan_digest: digest('b'),
    catalog_revision: digest('c'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://defaults/lock'},
      resolved_plan: {digest: digest('b'), source_ref: 'resolved-plan-v1://defaults/plan'},
      activation_record: {digest: digest('1'), source_ref: 'activation-record-v4://defaults/activation'},
      authority_snapshot: {digest: digest('e'), source_ref: 'authority-snapshot-v4://defaults/snapshot'},
    },
    data: {
      profile: {profile_id: 'defaults', profile_revision: digest('a'), catalog_revision: digest('c')},
      profile_document: {packs: [{pack_id: 'provider-pack', role: 'provider', artifact_digest: digest('4')}]},
      base: {pack_id: 'base-pack'},
      shell: {pack_id: 'shell-pack'},
      application: {pack_id: 'application-pack', role: 'application'},
      pack_closure: [{pack_id: 'provider-pack'}],
      profile_lock: {lock_digest: digest('d')},
      resolved_plan: {plan_digest: digest('b')},
      activation_record: {activation_id: 'activation-one'},
      authority_snapshot: {profile_authority_snapshot_digest: digest('e')},
      resolved_wiring: {requested_edges: [], bindings: []},
    },
  };
}

function profileCatalogEntry(profileId: string, active: boolean): RuntimeProfileCatalogEntry {
  const definitionDigest = active ? digest('5') : digest('6');
  const closure = [
    {pack_id: 'base-pack', role: 'base', version: '1.0.0', artifact_digest: digest('1'), artifact_ref: `pack-v4://base-pack@${digest('1')}`},
    {pack_id: 'shell-pack', role: 'shell', version: '1.0.0', artifact_digest: digest('2'), artifact_ref: `pack-v4://shell-pack@${digest('2')}`},
    {pack_id: 'application-pack', role: 'application', version: '1.0.0', artifact_digest: digest('3'), artifact_ref: `pack-v4://application-pack@${digest('3')}`},
    {pack_id: 'provider-pack', role: 'provider', version: '1.0.0', artifact_digest: digest('4'), artifact_ref: `pack-v4://provider-pack@${digest('4')}`},
  ];
  return {
    profile_id: profileId,
    display_name: profileId === 'defaults' ? 'Defaults Profile' : 'Alternate Profile',
    active,
    lifecycle_state: active ? 'active' : 'available',
    available: true,
    diagnostics: [],
    definition: {
      digest: definitionDigest,
      ref: `profile-v4://${profileId}/${definitionDigest}`,
      catalog_revision: null,
      source_path: `ecosystem/defaultspack/v4/${profileId}.profile.v4.json`,
      provenance: {source_kind: 'repository'},
    },
    bindings: {
      base: {pack_id: 'base-pack', definition_revision: null, definition_digest: null, artifact_digest: digest('1')},
      shell: {provider_id: 'shell.provider', pack_id: 'shell-pack', definition_revision: null, definition_digest: null, artifact_digest: digest('2')},
      application: {pack_id: 'application-pack', artifact_digest: digest('3'), artifact_ref: `pack-v4://application-pack@${digest('3')}`},
    },
    pack_closure: closure,
    records: {
      profile_revision: active ? digest('a') : null,
      profile_lock_digest: active ? digest('d') : null,
      plan_digest: active ? digest('b') : null,
    },
    authority_snapshot: {
      state: active ? 'active' : 'captured_on_resolve',
      digest: active ? digest('e') : null,
      ref: active ? `authority-snapshot-v4://${profileId}/${digest('e')}` : null,
      definition_references: [],
    },
    candidate: {state: 'not_staged', candidate_id: null, candidate_digest: null, expires_at: null},
  };
}

function catalogEnvelope(activeProfileId = 'defaults'): RuntimeSurfaceEnvelope<RuntimeProfileCatalogProjection> {
  const profiles = [
    profileCatalogEntry('defaults', activeProfileId === 'defaults'),
    profileCatalogEntry('alternate', activeProfileId === 'alternate'),
  ];
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: 'profiles',
    state: 'ready',
    profile_id: activeProfileId,
    profile_revision: digest('a'),
    plan_digest: digest('b'),
    catalog_revision: digest('c'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://defaults/lock'},
      resolved_plan: {digest: digest('b'), source_ref: 'resolved-plan-v1://defaults/plan'},
      activation_record: {digest: digest('1'), source_ref: 'activation-record-v4://defaults/activation'},
      authority_snapshot: {digest: digest('e'), source_ref: 'authority-snapshot-v4://defaults/snapshot'},
    },
    data: {
      catalog_api_version: 'io.tobkiri.profile-catalog-presentation.v4',
      catalog_digest: digest('c'),
      bundle_lock_digest: digest('8'),
      catalog_ref: `profile-catalog-v4://bundle/${digest('c')}`,
      active_profile_id: activeProfileId,
      count: profiles.length,
      profiles,
    },
  };
}

function pack(id: string, artifactDigest = digest('4'), approved = true): Pack {
  return {
    id,
    name: id,
    version: '1.0.0',
    type: 'community',
    installed: true,
    enabled: true,
    description: 'fixture',
    artifactDigest,
    profileId: 'defaults',
    workspaceId: 'workspace',
    profileRevision: digest('a'),
    planDigest: digest('b'),
    catalogRevision: digest('9'),
    approvalStatus: approved ? 'approved' : 'pending',
    approvalReason: approved ? null : 'approval_required',
    approved,
    hashValid: true,
    criticalChanged: false,
    approvalIssues: approved ? [] : ['approval_required'],
    capabilities: [],
    operations: [],
    flows: [],
    dependencies: [],
  };
}

function surfaceState(): RuntimeSurfaceState<unknown> {
  return {
    data: profileSnapshot(),
    status: 'ready',
    error: null,
    stale: false,
    canMutate: false,
    refresh: async () => undefined,
  };
}

function catalogState(
  data: RuntimeSurfaceEnvelope<RuntimeProfileCatalogProjection>,
  overrides: Partial<RuntimeSurfaceState<RuntimeProfileCatalogProjection>> = {},
): RuntimeSurfaceState<RuntimeProfileCatalogProjection> {
  return {
    data,
    status: 'ready',
    error: null,
    stale: false,
    canMutate: false,
    refresh: async () => undefined,
    ...overrides,
  };
}

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

let previousLocalStorage: unknown;
let previousSessionStorage: unknown;

beforeEach(() => {
  previousLocalStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  previousSessionStorage = (globalThis as typeof globalThis & {sessionStorage?: unknown}).sessionStorage;
});

afterEach(() => {
  Object.defineProperties(globalThis, {
    localStorage: {value: previousLocalStorage, configurable: true},
    sessionStorage: {value: previousSessionStorage, configurable: true},
  });
});

function buttonByLabel(container: HTMLElement, label: string): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  assert.ok(button, `missing button ${label}`);
  return button;
}

function buttonContaining(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.includes(text));
  assert.ok(button, `missing button ${text}`);
  return button as HTMLButtonElement;
}

function ceremonyOwnerCount(container: HTMLElement): number {
  return container.querySelectorAll('[aria-label="Profile change steps"]').length;
}

function ceremonyClient(calls: Array<{step: string; payload: Record<string, unknown>}>, gate?: {promise: Promise<void>; release: () => void}): ProfileCeremonyClient {
  return {
    resolve: async (input): Promise<ProfileResolveResult> => {
      calls.push({step: 'resolve', payload: {...input}});
      if (gate) await gate.promise;
      return {
        state: 'resolved',
        candidate_id: 'candidate-one',
        candidate_digest: digest('2'),
        expires_in: 60,
        review: {
          profile: {profile_id: 'alternate'},
          profile_lock: {lock_digest: digest('d')},
          resolved_plan: {plan_digest: digest('b')},
          predecessor: {plan_digest: digest('b')},
          catalog_binding: {
            profile_definition_digest: digest('6'),
            profile_catalog_digest: digest('c'),
            bundle_lock_digest: digest('8'),
          },
        },
        next_action: 'review',
        write_set: [],
      };
    },
    review: async (input): Promise<ProfileReviewResult> => {
      calls.push({step: 'review', payload: {...input}});
      return {state: 'reviewed', candidate_id: 'candidate-one', candidate_digest: digest('2'), next_action: 'approval', write_set: []};
    },
    approve: async (input): Promise<ProfileApproveResult> => {
      calls.push({step: 'approve', payload: {...input}});
      return {
        state: 'approved',
        approval_id: 'approval-one',
        approval_digest: digest('3'),
        expires_in: 30,
        next_action: 'activation',
        write_set: [],
        authority_approval: {approval_id: 'approval-one', approval_digest: digest('3'), decision: 'approved', security_epoch: 4},
      };
    },
    activate: async (input): Promise<ProfileActivateResult> => {
      calls.push({step: 'activate', payload: {...input}});
      return {state: 'active', profile_id: 'alternate', activation_id: 'activation-two', plan_digest: digest('b'), security_epoch: 4, fencing_token: 8, authoritative_snapshot: profileSnapshot()};
    },
  };
}

test('authoritative Profile selection binds exact identity and completes resolve-review-approval-activation', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const calls: Array<{step: string; payload: Record<string, unknown>}> = [];
  let packRefreshes = 0;
  let catalogRefreshes = 0;
  let currentCatalog = catalogEnvelope();
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(currentCatalog, {refresh: async () => { catalogRefreshes += 1; }})}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => { packRefreshes += 1; }}
          client={ceremonyClient(calls)}
          onActivated={async () => { catalogRefreshes += 1; }}
        />,
      );
    });
    await act(async () => undefined);

    const alternate = buttonByLabel(container, 'Select Profile Alternate Profile (alternate)');
    assert.equal(alternate.disabled, false);
    assert.equal(alternate.getAttribute('aria-pressed'), 'false');
    await act(async () => { alternate.click(); });
    assert.equal(container.querySelectorAll('button[aria-label^="Toggle Defaults Pack"]').length, 0);
    assert.match(container.textContent ?? '', /Authoritative Pack closure/);

    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    await act(async () => { buttonContaining(container, 'Review exact candidate').click(); });
    await act(async () => { buttonContaining(container, 'Request Kernel approval').click(); });
    await act(async () => { buttonContaining(container, 'Activate approved Profile').click(); });

    assert.deepEqual(calls.map((call) => call.step), ['resolve', 'review', 'approve', 'activate']);
    assert.deepEqual(calls[0].payload.desired_pack_ids, ['provider-pack']);
    assert.equal(calls[0].payload.profile_id, 'alternate');
    assert.equal(calls[0].payload.profile_definition_digest, digest('6'));
    assert.equal(calls[0].payload.profile_catalog_digest, digest('c'));
    assert.equal(calls[0].payload.bundle_lock_digest, digest('8'));
    assert.equal(Object.prototype.hasOwnProperty.call(calls[0].payload, 'approved'), false);
    assert.equal(packRefreshes, 1);
    assert.equal(catalogRefreshes, 1);

    currentCatalog = catalogEnvelope('alternate');
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(currentCatalog, {refresh: async () => { catalogRefreshes += 1; }})}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
          client={ceremonyClient([])}
        />,
      );
    });
    assert.equal(buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').getAttribute('aria-pressed'), 'true');
    assert.match(container.textContent ?? '', /Active/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('stale catalogs lock selection and ceremony actions while retaining visible evidence', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope(), {status: 'stale', stale: true, error: {code: 'STALE', message: 'stale catalog'}})}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    assert.equal(buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').disabled, true);
    assert.match(container.textContent ?? '', /catalog is stale/i);
    assert.equal(buttonContaining(container, 'Resolve candidate').disabled, true);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('selector gives every named Profile the same ceremony and never falls back to Defaults', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    assert.equal(ceremonyOwnerCount(container), 1);
    await act(async () => {
      buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').click();
    });
    assert.equal(ceremonyOwnerCount(container), 1);
    assert.equal(container.querySelectorAll('button[aria-label^="Toggle Defaults Pack"]').length, 0);
    assert.doesNotMatch(container.textContent ?? '', /Defaults Pack-set editor/);

    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope(), {data: null, status: 'loading'})}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    assert.equal(ceremonyOwnerCount(container), 0);
    assert.match(container.textContent ?? '', /Loading authoritative Profile definitions/);
    assert.match(container.textContent ?? '', /Select a verified Profile definition/);

    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope(), {
            data: null,
            status: 'error',
            error: {code: 'FAILED', message: 'HTTP API session mismatch'},
          })}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    assert.equal(ceremonyOwnerCount(container), 0);
    assert.match(container.textContent ?? '', /HTTP API session mismatch/);
    assert.ok(container.querySelector('[role="alert"]'));
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('Profile catalog failure copies the complete visible diagnostic, not a hidden raw error', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  let copied = '';
  Object.defineProperty(dom.window.navigator, 'clipboard', {
    configurable: true,
    value: {writeText: async (text: string) => { copied = text; }},
  });
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope(), {
            data: null,
            status: 'error',
            stale: true,
            error: {code: 'FAILED', message: 'The Broker rejected the signed catalog response.'},
          })}
          packs={[]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    const copy = buttonByLabel(container, 'Copy Profile catalog error');
    await act(async () => {
      copy.click();
      await Promise.resolve();
    });
    assert.match(container.textContent ?? '', /Authoritative Profile catalog is locked/);
    assert.match(container.textContent ?? '', /The Broker rejected the signed catalog response\./);
    assert.match(container.textContent ?? '', /The last accepted definitions remain read-only until the catalog refreshes\./);
    assert.equal(copied, [
      'Authoritative Profile catalog is locked',
      'The Broker rejected the signed catalog response.',
      'The last accepted definitions remain read-only until the catalog refreshes.',
    ].join('\n'));
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
    Object.defineProperty(globalThis, 'navigator', {value: previousNavigator, configurable: true});
  }
});

test('selected Profile shows only its own optional verified capability snapshot', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousState = useAppStore.getState();
  const {dom, container, root} = createDom();
  useAppStore.setState({
    frontendCatalog: optionalConversationCatalog(),
    frontendCatalogLoading: false,
    frontendCatalogError: null,
  });
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    assert.match(container.textContent ?? '', /Optional verified capabilities/);
    assert.match(container.textContent ?? '', /defaults conversation/);
    assert.match(container.textContent ?? '', /defaults-ui-pack/);

    await act(async () => {
      buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').click();
    });
    assert.match(container.textContent ?? '', /This Profile is browse-only/);
    assert.doesNotMatch(container.textContent ?? '', /defaults-ui-pack/);
    assert.doesNotMatch(container.textContent ?? '', /defaults conversation/);

    await act(async () => {
      useAppStore.setState({frontendCatalog: optionalConversationCatalog('alternate', false)});
    });
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope('alternate'))}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    assert.match(container.textContent ?? '', /No verified conversation capability is published/);
    assert.ok(container.querySelector('[data-testid="profile-conversation-capability"] [role="status"]'));
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('Profile capability errors copy the displayed safe diagnostic instead of hidden catalog detail', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousState = useAppStore.getState();
  const {dom, container, root} = createDom();
  let copied = '';
  Object.defineProperty(dom.window.navigator, 'clipboard', {
    configurable: true,
    value: {writeText: async (text: string) => { copied = text; }},
  });
  useAppStore.setState({
    frontendCatalog: null,
    frontendCatalogLoading: false,
    frontendCatalogError: 'Host-only catalog transport detail',
  });
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    const capability = container.querySelector<HTMLElement>(
      '[data-testid="profile-conversation-capability"]',
    );
    assert.ok(capability);
    assert.match(capability.textContent ?? '', /No accepted capability snapshot is bound to this active Profile\./);
    assert.doesNotMatch(capability.textContent ?? '', /Host-only catalog transport detail/);
    const copy = buttonByLabel(capability, 'Copy Profile capability error');
    await act(async () => {
      copy.click();
      await Promise.resolve();
    });
    assert.equal(copied, 'No accepted capability snapshot is bound to this active Profile.');
  } finally {
    act(() => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
    Object.defineProperty(globalThis, 'navigator', {value: previousNavigator, configurable: true});
  }
});

test('selector keyboard semantics remain labelled and focusable in a compact viewport', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  Object.defineProperty(dom.window, 'innerWidth', {value: 320, configurable: true});
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    assert.equal(dom.window.innerWidth, 320);
    const group = container.querySelector<HTMLElement>('[role="group"][aria-label="Select a verified Profile"]');
    assert.ok(group);
    const profileButtons = [...group.querySelectorAll<HTMLButtonElement>('button')];
    assert.equal(profileButtons.length, 2);
    for (const button of profileButtons) {
      assert.ok(button.getAttribute('aria-label')?.startsWith('Select Profile'));
      assert.ok(['true', 'false'].includes(button.getAttribute('aria-pressed') ?? ''));
      assert.match(button.className, /min-h-11/);
      assert.match(button.className, /focus-visible:ring/);
    }
    const alternate = buttonByLabel(container, 'Select Profile Alternate Profile (alternate)');
    alternate.focus();
    assert.equal(dom.window.document.activeElement, alternate);
    alternate.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    await act(async () => { alternate.click(); });
    assert.equal(alternate.getAttribute('aria-pressed'), 'true');
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('component surfaces an unknown ceremony timeout and prevents a replacement step', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const timeoutClient = ceremonyClient([]);
  let resolveCalls = 0;
  timeoutClient.resolve = async () => {
    resolveCalls += 1;
    throw new RuntimeSurfaceError('TIMEOUT', 'HTTP request timed out while resolving the Profile.');
  };
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
          client={timeoutClient}
        />,
      );
    });
    await act(async () => undefined);
    await act(async () => { buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').click(); });
    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    assert.match(container.textContent ?? '', /Profile ceremony stopped fail-closed/);
    assert.match(container.textContent ?? '', /Profile ceremony result is unknown/);
    assert.match(container.textContent ?? '', /no new request will be sent automatically/);
    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    assert.equal(resolveCalls, 1);
    assert.equal([...container.querySelectorAll('button')].some((button) => button.textContent?.includes('Review exact candidate')), false);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('mismatched resolve binding fails closed before review or activation', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const calls: Array<{step: string; payload: Record<string, unknown>}> = [];
  const badClient = ceremonyClient(calls);
  badClient.resolve = async (input): Promise<ProfileResolveResult> => {
    calls.push({step: 'resolve', payload: {...input}});
    return {
      state: 'resolved',
      candidate_id: 'candidate-one',
      candidate_digest: digest('2'),
      expires_in: 60,
      review: {
        profile: {profile_id: 'unexpected-profile'},
        profile_lock: {},
        resolved_plan: {},
        predecessor: {},
        catalog_binding: {
          profile_definition_digest: digest('f'),
          profile_catalog_digest: digest('c'),
          bundle_lock_digest: digest('8'),
        },
      },
      next_action: 'review',
      write_set: [],
    };
  };
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
          client={badClient}
        />,
      );
    });
    await act(async () => undefined);
    await act(async () => { buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').click(); });
    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    assert.equal(calls.length, 1);
    assert.match(container.textContent ?? '', /stopped fail-closed|digest mismatch/i);
    assert.equal([...container.querySelectorAll('button')].some((button) => button.textContent?.includes('Review exact candidate')), false);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('resolve double-submit is prevented while the authoritative request is pending', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const calls: Array<{step: string; payload: Record<string, unknown>}> = [];
  let release!: () => void;
  const gate = {promise: new Promise<void>((resolve) => { release = resolve; }), release};
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope())}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
          client={ceremonyClient(calls, gate)}
        />,
      );
    });
    await act(async () => undefined);
    await act(async () => { buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').click(); });
    const resolveButton = buttonContaining(container, 'Resolve candidate');
    await act(async () => {
      resolveButton.click();
      resolveButton.click();
    });
    assert.equal(calls.length, 1);
    assert.equal(resolveButton.disabled, true);
    await act(async () => { release(); });
    assert.equal(calls.length, 1);
    assert.match(container.textContent ?? '', /Review exact candidate/);
  } finally {
    release();
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('fresh selector mount rehydrates the active marker from the catalog projection and shows empty state without inventing entries', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  try {
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalogEnvelope('alternate'))}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
    assert.equal(buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').getAttribute('aria-pressed'), 'true');

    const empty: RuntimeSurfaceEnvelope<RuntimeProfileCatalogProjection> = {
      ...catalogEnvelope('alternate'),
      data: {
        ...catalogEnvelope('alternate').data,
        active_profile_id: null,
        count: 0,
        profiles: [],
      },
    };
    await act(async () => {
      root.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(empty)}
          packs={[]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    assert.match(container.textContent ?? '', /No Profile definitions are currently published/);
    assert.equal(container.querySelectorAll('button[aria-label^="Select Profile"]').length, 0);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('restart-style remount rehydrates the active marker from the new authoritative catalog snapshot', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let mountedRoot = root;
  const renderCatalog = async (catalog: RuntimeSurfaceEnvelope<RuntimeProfileCatalogProjection>) => {
    await act(async () => {
      mountedRoot.render(
        <ProfileCatalogSelector
          profileSurface={surfaceState()}
          catalogSurface={catalogState(catalog)}
          packs={[pack('provider-pack')]}
          packsLoading={false}
          loadPacks={async () => undefined}
        />,
      );
    });
    await act(async () => undefined);
  };
  try {
    await renderCatalog(catalogEnvelope());
    await act(async () => { buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').click(); });
    assert.equal(buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').getAttribute('aria-pressed'), 'true');

    await act(async () => { mountedRoot.unmount(); });
    mountedRoot = createRoot(container);
    await renderCatalog(catalogEnvelope());
    assert.equal(buttonByLabel(container, 'Select Profile Defaults Profile (defaults)').getAttribute('aria-pressed'), 'true');
    assert.equal(buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').getAttribute('aria-pressed'), 'false');

    await act(async () => { mountedRoot.unmount(); });
    mountedRoot = createRoot(container);
    await renderCatalog(catalogEnvelope('alternate'));
    assert.equal(buttonByLabel(container, 'Select Profile Alternate Profile (alternate)').getAttribute('aria-pressed'), 'true');
    assert.match(container.textContent ?? '', /Active/);
  } finally {
    act(() => mountedRoot.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('Pack catalog metadata changes refresh authoritative candidates without adding definitions', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  let catalogRefreshes = 0;
  try {
    const catalog = catalogEnvelope();
    const renderSelector = (currentPack: Pack) => root.render(
      <ProfileCatalogSelector
        profileSurface={surfaceState()}
        catalogSurface={catalogState(catalog, {refresh: async () => { catalogRefreshes += 1; }})}
        packs={[currentPack]}
        packsLoading={false}
        loadPacks={async () => undefined}
      />,
    );
    await act(async () => { renderSelector(pack('provider-pack')); });
    await act(async () => undefined);
    assert.equal(catalogRefreshes, 0);
    assert.equal(container.querySelectorAll('button[aria-label^="Select Profile"]').length, 2);

    await act(async () => {
      renderSelector({...pack('provider-pack'), catalogRevision: digest('f')});
    });
    await act(async () => undefined);
    assert.equal(catalogRefreshes, 1);
    assert.equal(container.querySelectorAll('button[aria-label^="Select Profile"]').length, 2);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('Profile catalog remains browseable while runtime ceremony actions are gated', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  try {
    await act(async () => {
      root.render(
        <MemoryRouter>
          <ProfileCatalogSelector
            profileSurface={surfaceState()}
            catalogSurface={catalogState(catalogEnvelope())}
            packs={[pack('provider-pack')]}
            packsLoading={false}
            loadPacks={async () => undefined}
            runtimeVerified={false}
          />
        </MemoryRouter>,
      );
    });
    await act(async () => undefined);

    assert.match(container.textContent ?? '', /Defaults Profile/);
    assert.match(container.textContent ?? '', /Alternate Profile/);
    assert.match(container.textContent ?? '', /Profile activation is unavailable/);
    assert.match(container.textContent ?? '', /Complete Setup verification/);
    assert.ok(container.querySelector('a[href="/setup"]'));
    assert.equal([...container.querySelectorAll('button')].some((button) => button.textContent?.includes('Resolve candidate')), false);
    assert.ok(container.querySelector('[data-testid="profile-ceremony-gate"]'));
    assert.equal(
      container.querySelector('button[aria-label="Copy Profile ceremony gate warning"]'),
      null,
    );
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});
