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
import {RUNTIME_SURFACE_API_VERSION, type RuntimeSurfaceEnvelope} from '@/src/lib/runtimeSurface';
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

test('Profile closure candidates show new catalog Packs and execute resolve through activation', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const {dom, container, root} = createDom();
  const surface = surfaceState();
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
        review: {profile: {}, profile_lock: {lock_digest: digest('d')}, resolved_plan: {plan_digest: digest('b')}, predecessor: {plan_digest: digest('b')}},
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
          packsLoading={false}
          loadPacks={async () => { packRefreshes += 1; }}
          client={client}
          onActivated={async () => { activated += 1; }}
        />,
      );
    });
    assert.match(container.textContent ?? '', /New Pack/);
    const newPackButton = buttonContaining(container, 'new-pack');
    assert.equal(newPackButton.disabled, false);
    assert.equal(buttonContaining(container, 'blocked-pack').disabled, true);

    await act(async () => { newPackButton.click(); });
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
      review: {profile: {}, profile_lock: {}, resolved_plan: {}, predecessor: {}},
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
          packsLoading={false}
          loadPacks={async () => {}}
          client={client}
        />,
      );
    });
    await act(async () => { buttonContaining(container, 'new-pack').click(); });
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
