import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {ProfileCeremonyPanel} from './ProfileCeremonyPanel';
import type {
  ProfileActivateResult,
  ProfileApproveResult,
  ProfileCeremonyClient,
  ProfileResolveResult,
  ProfileReviewResult,
} from '@/src/lib/profileCeremony';
import {
  RUNTIME_SURFACE_API_VERSION,
  type RuntimeProfileCatalogEntry,
  type RuntimeProfileCatalogProjection,
  type RuntimeSurfaceEnvelope,
} from '@/src/lib/runtimeSurface';
import type {RuntimeSurfaceState} from '@/src/hooks/useRuntimeSurface';
import type {Pack} from '@/src/store';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function snapshot(): RuntimeSurfaceEnvelope<unknown> {
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
      activation_record: {digest: digest('1'), source_ref: 'activation-record-v1://defaults/activation'},
      authority_snapshot: {digest: digest('e'), source_ref: 'authority-snapshot-v4://defaults/snapshot'},
    },
    data: {
      profile: {profile_id: 'defaults', profile_revision: digest('a'), catalog_revision: digest('c')},
      profile_document: {packs: [{pack_id: 'provider-pack', role: 'provider', artifact_digest: digest('1')}]},
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

function pack(id: string, approved = true): Pack {
  return {
    id,
    name: id === 'new-pack' ? 'New Pack' : 'Provider Pack',
    version: '1.0.0',
    type: 'community',
    installed: true,
    enabled: true,
    description: 'fixture',
    artifactDigest: digest('1'),
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

function surfaceState() {
  let refreshCount = 0;
  const state = {
    data: snapshot(),
    status: 'ready' as const,
    error: null,
    stale: false,
    canMutate: false as const,
    refresh: async () => { refreshCount += 1; },
  };
  return {state, getRefreshCount: () => refreshCount};
}

function catalogEntry(
  packIds = ['provider-pack', 'new-pack'],
  profileId = 'defaults',
  active = true,
): RuntimeProfileCatalogEntry {
  return {
    profile_id: profileId,
    display_name: active ? 'Defaults' : 'Alternate Profile',
    active,
    lifecycle_state: active ? 'active' : 'available',
    available: true,
    diagnostics: [],
    definition: {
      digest: digest('6'),
      ref: `profile-v4://${profileId}/${digest('6')}`,
      catalog_revision: digest('c'),
      source_path: 'profiles/defaults.json',
      provenance: {},
    },
    bindings: {
      base: {
        pack_id: 'base-pack',
        definition_revision: digest('1'),
        definition_digest: digest('1'),
        artifact_digest: digest('1'),
      },
      shell: {
        provider_id: 'shell-provider',
        pack_id: 'shell-pack',
        definition_revision: digest('1'),
        definition_digest: digest('1'),
        artifact_digest: digest('1'),
      },
      application: null,
    },
    pack_closure: packIds.map((packId) => ({
      pack_id: packId,
      role: 'provider',
      version: '1.0.0',
      artifact_digest: digest('1'),
      artifact_ref: `pack-v4://${packId}@${digest('1')}`,
    })),
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
    candidate: {
      state: 'not_staged',
      candidate_id: null,
      candidate_digest: null,
      expires_at: null,
    },
  };
}

function catalogSurfaceState(entry = catalogEntry()): RuntimeSurfaceState<RuntimeProfileCatalogProjection> {
  const profiles = entry.active
    ? [entry]
    : [catalogEntry(['provider-pack'], 'defaults'), entry];
  const projection: RuntimeProfileCatalogProjection = {
    catalog_api_version: 'io.tobkiri.profile-catalog-presentation.v4',
    catalog_digest: digest('c'),
    bundle_lock_digest: digest('d'),
    catalog_ref: `profile-catalog-v4://bundle/${digest('c')}`,
    active_profile_id: 'defaults',
    count: profiles.length,
    profiles,
  };
  return {
    data: {...snapshot(), surface: 'profiles', data: projection} as RuntimeSurfaceEnvelope<RuntimeProfileCatalogProjection>,
    status: 'ready',
    error: null,
    stale: false,
    canMutate: false,
    refresh: async () => {},
  };
}

function authoritativeSelection(entry = catalogEntry()) {
  return {
    entry,
    catalogDigest: digest('c'),
    bundleLockDigest: digest('d'),
  };
}

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

function buttonContaining(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll('button')].find((candidate) => candidate.textContent?.includes(text));
  assert.ok(button, `missing button ${text}`);
  return button as HTMLButtonElement;
}

test('Profile closure candidates come from the authoritative catalog and execute resolve through activation', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const surface = surfaceState();
  const selection = authoritativeSelection(catalogEntry(['provider-pack']));
  const calls: Array<{step: string; payload: Record<string, unknown>}> = [];
  let packRefreshes = 0;
  let activated = 0;
  const client: ProfileCeremonyClient = {
    resolve: async (input): Promise<ProfileResolveResult> => {
      calls.push({step: 'resolve', payload: {...input}});
      return {
        state: 'resolved',
        candidate_id: 'candidate-one',
        candidate_digest: digest('2'),
        expires_in: 60,
        review: {
          profile: {profile_id: 'defaults'},
          profile_lock: {lock_digest: digest('d')},
          resolved_plan: {plan_digest: digest('b')},
          predecessor: {plan_digest: digest('b')},
          catalog_binding: {
            profile_definition_digest: digest('6'),
            profile_catalog_digest: digest('c'),
            bundle_lock_digest: digest('d'),
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
      return {state: 'active', profile_id: 'defaults', activation_id: 'activation-two', plan_digest: digest('b'), security_epoch: 4, fencing_token: 8, authoritative_snapshot: snapshot()};
    },
  };

  try {
    await act(async () => {
      root.render(
        <ProfileCeremonyPanel
          surface={surface.state}
          packs={[pack('provider-pack'), pack('new-pack'), pack('blocked-pack', false)]}
          loadPacks={async () => { packRefreshes += 1; }}
          client={client}
          onActivated={async () => { activated += 1; }}
          authoritativeSelection={selection}
          catalogSurface={catalogSurfaceState(selection.entry)}
        />,
      );
    });
    assert.match(container.textContent ?? '', /Authoritative Pack closure/);
    await act(async () => { buttonContaining(container, 'Add Pack · New Pack').click(); });
    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    await act(async () => { buttonContaining(container, 'Review exact candidate').click(); });
    await act(async () => { buttonContaining(container, 'Request Kernel approval').click(); });
    await act(async () => { buttonContaining(container, 'Activate approved Profile').click(); });

    assert.deepEqual(calls.map((call) => call.step), ['resolve', 'review', 'approve', 'activate']);
    const desired = calls[0].payload.desired_pack_ids as string[];
    assert.deepEqual(desired.sort(), ['new-pack', 'provider-pack']);
    assert.equal(desired.includes('base-pack'), false);
    assert.equal(desired.includes('shell-pack'), false);
    assert.equal(desired.includes('application-pack'), false);
    assert.equal(activated, 1);
    assert.equal(packRefreshes, 1);
    assert.equal(surface.getRefreshCount(), 1);
    assert.match(container.textContent ?? '', /Authority Kernel approval recorded|active/i);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('Profile ceremony does not offer an error-copy action while its catalog is loading', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const surface = surfaceState();
  const selection = authoritativeSelection(catalogEntry(['provider-pack']));
  try {
    await act(async () => {
      root.render(
        <ProfileCeremonyPanel
          surface={surface.state}
          packs={[pack('provider-pack')]}
          loadPacks={async () => undefined}
          authoritativeSelection={selection}
          catalogSurface={{
            ...catalogSurfaceState(selection.entry),
            status: 'loading',
          }}
        />,
      );
    });
    assert.match(container.textContent ?? '', /The authoritative Profile catalog is loading/);
    assert.equal(
      container.querySelector('button[aria-label="Copy Profile catalog warning"]'),
      null,
    );
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('a non-active Profile can stage and review a successor closure without activation', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const surface = surfaceState();
  const alternate = catalogEntry(['provider-pack'], 'alternate', false);
  const selection = authoritativeSelection(alternate);
  const calls: string[] = [];
  let approvalCalls = 0;
  let activationCalls = 0;
  const client: ProfileCeremonyClient = {
    resolve: async (input): Promise<ProfileResolveResult> => {
      calls.push('resolve');
      assert.deepEqual(input.desired_pack_ids.sort(), ['new-pack', 'provider-pack']);
      return {
        state: 'resolved',
        candidate_id: 'alternate-candidate',
        candidate_digest: digest('2'),
        expires_in: 60,
        review: {
          profile: {profile_id: 'alternate'},
          profile_lock: {lock_digest: digest('d')},
          resolved_plan: {profile_revision: digest('f'), plan_digest: digest('b')},
          predecessor: {plan_digest: digest('b')},
          catalog_binding: {
            profile_definition_digest: digest('6'),
            profile_catalog_digest: digest('c'),
            bundle_lock_digest: digest('d'),
          },
        },
        next_action: 'review',
        write_set: [],
      };
    },
    review: async (): Promise<ProfileReviewResult> => {
      calls.push('review');
      return {
        state: 'reviewed',
        candidate_id: 'alternate-candidate',
        candidate_digest: digest('2'),
        next_action: 'approval',
        write_set: [],
      };
    },
    approve: async (): Promise<ProfileApproveResult> => {
      approvalCalls += 1;
      throw new Error('approval must wait for the user');
    },
    activate: async (): Promise<ProfileActivateResult> => {
      activationCalls += 1;
      throw new Error('inactive Profile must not activate during browsing');
    },
  };

  try {
    await act(async () => {
      root.render(
        <ProfileCeremonyPanel
          surface={surface.state}
          packs={[pack('provider-pack'), pack('new-pack')]}
          loadPacks={async () => {}}
          client={client}
          authoritativeSelection={selection}
          catalogSurface={catalogSurfaceState(alternate)}
        />,
      );
    });
    assert.match(container.textContent ?? '', /Alternate Profile/);
    const addPack = buttonContaining(container, 'Add Pack · New Pack');
    assert.equal(addPack.getAttribute('aria-label'), 'Add Pack New Pack to Alternate Profile closure');
    await act(async () => { addPack.click(); });
    assert.match(container.textContent ?? '', /Successor staged/);
    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    await act(async () => { buttonContaining(container, 'Review exact candidate').click(); });

    assert.deepEqual(calls, ['resolve', 'review']);
    assert.equal(approvalCalls, 0);
    assert.equal(activationCalls, 0);
    assert.match(container.textContent ?? '', /Request Kernel approval/);
    assert.doesNotMatch(container.textContent ?? '', /Activate approved Profile/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});

test('Profile ceremony fails closed when a custom review client substitutes another candidate', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const surface = surfaceState();
  let approvalCalls = 0;
  const client: ProfileCeremonyClient = {
    resolve: async (): Promise<ProfileResolveResult> => ({
      state: 'resolved',
      candidate_id: 'candidate-a',
      candidate_digest: digest('2'),
      expires_in: 60,
        review: {
          profile: {profile_id: 'defaults'},
          profile_lock: {},
          resolved_plan: {},
          predecessor: {},
          catalog_binding: {
            profile_definition_digest: digest('6'),
            profile_catalog_digest: digest('c'),
            bundle_lock_digest: digest('d'),
          },
        },
      next_action: 'review',
      write_set: [],
    }),
    review: async (): Promise<ProfileReviewResult> => ({
      state: 'reviewed',
      candidate_id: 'candidate-b',
      candidate_digest: digest('3'),
      next_action: 'approval',
      write_set: [],
    }),
    approve: async (): Promise<ProfileApproveResult> => {
      approvalCalls += 1;
      throw new Error('approval must not run');
    },
    activate: async (): Promise<ProfileActivateResult> => {
      throw new Error('activation must not run');
    },
  };

  try {
    await act(async () => {
      root.render(
        <ProfileCeremonyPanel
          surface={surface.state}
          packs={[pack('provider-pack'), pack('new-pack')]}
          loadPacks={async () => {}}
          client={client}
          authoritativeSelection={authoritativeSelection()}
          catalogSurface={catalogSurfaceState()}
        />,
      );
    });
    await act(async () => { buttonContaining(container, 'Resolve candidate').click(); });
    await act(async () => { buttonContaining(container, 'Review exact candidate').click(); });

    assert.match(container.textContent ?? '', /different candidate/);
    assert.equal(approvalCalls, 0);
    assert.doesNotMatch(container.textContent ?? '', /Request Kernel approval/);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    Object.defineProperty(globalThis, 'window', {value: previousWindow, configurable: true});
    Object.defineProperty(globalThis, 'document', {value: previousDocument, configurable: true});
  }
});
