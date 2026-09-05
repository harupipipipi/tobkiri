import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

import {type Pack, useAppStore} from '@/src/store';
import type {ApiPackVMDoctor} from '@/src/lib/apiTypes';
import {setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';

const samplePack: Pack = {
  id: 'research-pack',
  name: 'Research Pack',
  version: '1.2.3',
  type: 'community',
  installed: true,
  enabled: true,
  description: 'Research tools',
  artifactDigest: 'sha256:research-artifact',
  profileId: 'profile-a',
  workspaceId: 'workspace-a',
  profileRevision: 'sha256:profile-a',
  planDigest: 'sha256:plan-a',
  catalogRevision: 'catalog-a',
  approvalStatus: 'approved',
  approvalReason: null,
  approved: true,
  hashValid: true,
  criticalChanged: false,
  approvalIssues: [],
  capabilities: [],
  flows: [],
  dependencies: [],
};

const revokedCatalogPack = {
  pack_id: samplePack.id,
  name: samplePack.name,
  version: samplePack.version,
  description: samplePack.description,
  is_core: false,
  installed: true,
  enabled: false,
  artifact_digest: samplePack.artifactDigest,
  approval_status: 'installed',
  approval_reason: 'approval_revoked',
  approved: false,
  hash_valid: true,
  critical_changed: false,
  approval_issues: ['approval_revoked'],
  profile_id: samplePack.profileId,
  workspace_id: samplePack.workspaceId,
  profile_revision: samplePack.profileRevision,
  plan_digest: samplePack.planDigest,
  catalog_revision: 'catalog-after-revoke',
};

const approvedCatalogPack = {
  ...revokedCatalogPack,
  enabled: false,
  approval_status: 'approved',
  approval_reason: null,
  approved: true,
  approval_issues: [],
};

const healthyDoctor: ApiPackVMDoctor = {
  ready: true,
  backend_id: 'tobkiri.python-pack-v4',
  platform: 'macos',
  instance: 'tobkiri-packvm-v4',
  reason: null,
  attestation_digest: `sha256:${'a'.repeat(64)}`,
};

let dom: JSDOM | null = null;
let previousState: ReturnType<typeof useAppStore.getState>;
let originalFetch: typeof fetch;

beforeEach(() => {
  setRuntimeDispatchStatus('runtime_ready');
  previousState = useAppStore.getState();
  originalFetch = globalThis.fetch;
  dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    sessionStorage: {value: dom.window.sessionStorage, configurable: true},
  });
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  useAppStore.setState(previousState, true);
  dom?.window.close();
  dom = null;
});

function installFetch(handler: (route: string, init?: RequestInit) => Promise<Response>): string[] {
  const routes: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const route = decodeURIComponent(url.replace('/api/contracts/defaultspack/', ''));
    routes.push(route);
    return handler(route, init);
  }) as typeof fetch;
  return routes;
}

function binding(catalogRevision = samplePack.catalogRevision) {
  return {
    profile_id: samplePack.profileId,
    workspace_id: samplePack.workspaceId,
    profile_revision: samplePack.profileRevision,
    plan_digest: samplePack.planDigest,
    catalog_revision: catalogRevision,
  };
}

function dynamicCatalog() {
  return {
    version: 'rumi.ui.contribution.v1',
    profile_id: samplePack.profileId,
    profile_revision: samplePack.profileRevision,
    activation_id: 'activation:profile-a',
    plan_hash: samplePack.planDigest,
    contributions: [],
    diagnostics: [],
    quarantined_pack_ids: [],
    catalog_hash: 'sha256:frontend-catalog',
  };
}

function operationStatusResponse(route: string, operationId: string): Response {
  const requestId = route.match(/[?&]request_id=([^&]+)/)?.[1];
  assert.ok(requestId);
  return new Response(JSON.stringify({
    success: true,
    data: {
      runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
      operation_status_api_version: 'io.tobkiri.control-operation-status.v1',
      request_id: requestId,
      operation_id: operationId,
      contract_id: 'tobkiri.host.pack-control.v4',
      request_digest: `sha256:${'a'.repeat(64)}`,
      state: 'indeterminate',
      result: null,
      result_digest: null,
      record_refs: [],
      safe_error_code: 'PROCESS_RESTART',
      created_at: 1,
      updated_at: 2,
    },
  }), {headers: {'Content-Type': 'application/json'}});
}

function normalizeOperationStatusRoute(route: string): string {
  if (!route.startsWith('GET /api/runtime-surface/operation-status?')) return route;
  assert.match(route, /^GET \/api\/runtime-surface\/operation-status\?request_id=[0-9a-f-]{36}$/i);
  return 'GET /api/runtime-surface/operation-status';
}

test('store revoke action calls the typed route, refreshes catalog, and confirms state', async () => {
  const routes = installFetch(async (route, init) => {
    if (route === 'POST /api/pack-control/approval-revoke') {
      assert.deepEqual(JSON.parse(String(init?.body)), {pack_id: samplePack.id});
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, approved: false, enabled: false, approval_status: 'revoked'},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(revokedCatalogPack.catalog_revision), packs: [revokedCatalogPack], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const successes: string[] = [];
  useAppStore.setState({
    packs: [samplePack],
    packVmDoctor: healthyDoctor,
    packApprovalPending: {},
    addToast: (message, type) => {
      if (type === 'success') successes.push(message);
    },
  });

  await useAppStore.getState().revokePackApproval(samplePack.id);

  assert.deepEqual(routes, [
    'POST /api/pack-control/approval-revoke',
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ]);
  const pack = useAppStore.getState().packs[0];
  assert.equal(pack.approved, false);
  assert.equal(pack.enabled, false);
  assert.equal(pack.approvalReason, 'approval_revoked');
  assert.deepEqual(useAppStore.getState().packApprovalPending, {});
  assert.deepEqual(successes, ['Pack approval revoked.']);
});

test('store revoke action fails closed without optimistic state or refresh on typed error', async () => {
  const routes = installFetch(async (route) => {
    assert.equal(route, 'POST /api/pack-control/approval-revoke');
    return new Response(JSON.stringify({
      success: false,
      data: null,
      error: 'HTTP 409 approval_revocation_denied',
    }), {status: 409, headers: {'Content-Type': 'application/json'}});
  });
  const errors: string[] = [];
  useAppStore.setState({
    packs: [samplePack],
    packApprovalPending: {},
    addToast: (message, type) => {
      if (type === 'error') errors.push(message);
    },
  });

  await assert.rejects(
    useAppStore.getState().revokePackApproval(samplePack.id),
    /HTTP 409 approval_revocation_denied/,
  );

  assert.deepEqual(routes, ['POST /api/pack-control/approval-revoke']);
  assert.deepEqual(useAppStore.getState().packs, [samplePack]);
  assert.deepEqual(useAppStore.getState().packApprovalPending, {});
  assert.deepEqual(errors, ['HTTP 409 approval_revocation_denied']);
});

test('store revoke action clears pending and surfaces a timeout without changing approval state', async () => {
  const routes = installFetch(async (route) => {
    if (route === 'POST /api/pack-control/approval-revoke') {
      throw new Error('POST request timed out after 10000ms: /api/pack-control/approval-revoke');
    }
    if (route.startsWith('GET /api/runtime-surface/operation-status?')) {
      return operationStatusResponse(route, 'approval.revoke');
    }
    assert.equal(route, 'GET /api/pack-control/catalog');
    return new Response(JSON.stringify({
      success: true,
      data: {...binding(approvedCatalogPack.catalog_revision), packs: [approvedCatalogPack], count: 1},
    }), {headers: {'Content-Type': 'application/json'}});
  });
  const errors: string[] = [];
  useAppStore.setState({
    packs: [samplePack],
    packApprovalPending: {},
    addToast: (message, type) => {
      if (type === 'error') errors.push(message);
    },
  });

  await assert.rejects(
    useAppStore.getState().revokePackApproval(samplePack.id),
    /result is unknown/,
  );

  await assert.rejects(
    useAppStore.getState().revokePackApproval(samplePack.id),
    /result is unknown/,
  );

  assert.equal(routes[0], 'POST /api/pack-control/approval-revoke');
  assert.deepEqual(routes.slice(1).map(normalizeOperationStatusRoute).sort(), [
    'GET /api/pack-control/catalog',
    'GET /api/runtime-surface/operation-status',
  ].sort());
  const refreshedPack = useAppStore.getState().packs[0];
  assert.equal(refreshedPack.approved, samplePack.approved);
  assert.equal(refreshedPack.enabled, samplePack.enabled);
  assert.equal(refreshedPack.approvalStatus, samplePack.approvalStatus);
  assert.equal(refreshedPack.approvalReason, samplePack.approvalReason);
  assert.deepEqual(refreshedPack.approvalIssues, samplePack.approvalIssues);
  assert.equal(refreshedPack.catalogRevision, approvedCatalogPack.catalog_revision);
  assert.deepEqual(useAppStore.getState().packApprovalPending, {});
  assert.equal(errors.length, 1);
  assert.match(errors[0], /result is unknown/);
  assert.equal(Object.keys(useAppStore.getState().packMutationUnknown).length, 1);
});

test('required Profile Pack is rejected before a revoke request is sent', async () => {
  const routes = installFetch(async (route) => {
    assert.fail(`unexpected route: ${route}`);
  });
  useAppStore.setState({
    packs: [{...samplePack, required: true}],
    packApprovalPending: {},
  });

  await useAppStore.getState().revokePackApproval(samplePack.id);

  assert.deepEqual(routes, []);
  assert.deepEqual(useAppStore.getState().packApprovalPending, {});
  assert.equal(useAppStore.getState().packs[0].approved, true);
});

test('approval guards the entire candidate-to-approve chain against duplicate submits', async () => {
  let releaseCandidate: (() => void) | undefined;
  const candidateGate = new Promise<void>((resolve) => { releaseCandidate = resolve; });
  const routes = installFetch(async (route) => {
    if (route === 'POST /api/pack-control/approval-candidate') {
      await candidateGate;
      return new Response(JSON.stringify({
        success: true,
        data: {
          candidate_id: 'candidate-one',
          pack_id: samplePack.id,
          snapshot_digest: `sha256:${'b'.repeat(64)}`,
        },
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'POST /api/pack-control/approval-approve') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, approved: true, enabled: false, approval_status: 'approved'},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(approvedCatalogPack.catalog_revision), packs: [approvedCatalogPack], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const pendingPack: Pack = {
    ...samplePack,
    enabled: false,
    approved: false,
    approvalStatus: 'pending',
    approvalReason: 'approval_required',
    approvalIssues: ['approval_required'],
  };
  useAppStore.setState({
    packs: [pendingPack],
    packApprovalPending: {},
    packVmDoctor: healthyDoctor,
  });

  const first = useAppStore.getState().approvePack(samplePack.id);
  const second = useAppStore.getState().approvePack(samplePack.id);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(routes, ['POST /api/pack-control/approval-candidate']);
  assert.deepEqual(useAppStore.getState().packApprovalPending, {[samplePack.id]: true});
  releaseCandidate?.();
  await Promise.all([first, second]);

  assert.deepEqual(routes, [
    'POST /api/pack-control/approval-candidate',
    'POST /api/pack-control/approval-approve',
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ]);
  assert.deepEqual(useAppStore.getState().packApprovalPending, {});
  assert.equal(useAppStore.getState().packs[0].approved, true);
});

test('approval candidate success followed by approval failure clears pending without false approval', async () => {
  const routes = installFetch(async (route) => {
    if (route === 'POST /api/pack-control/approval-candidate') {
      return new Response(JSON.stringify({
        success: true,
        data: {
          candidate_id: 'candidate-one',
          pack_id: samplePack.id,
          snapshot_digest: `sha256:${'b'.repeat(64)}`,
        },
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'POST /api/pack-control/approval-approve');
    return new Response(JSON.stringify({
      success: true,
      data: {...binding(), pack_id: samplePack.id, approved: true, enabled: false, approval_status: 'revoked'},
    }), {headers: {'Content-Type': 'application/json'}});
  });
  const errors: string[] = [];
  const pendingPack: Pack = {
    ...samplePack,
    enabled: false,
    approved: false,
    approvalStatus: 'pending',
    approvalReason: 'approval_required',
    approvalIssues: ['approval_required'],
  };
  useAppStore.setState({
    packs: [pendingPack],
    packApprovalPending: {},
    addToast: (message, type) => {
      if (type === 'error') errors.push(message);
    },
  });

  await assert.rejects(
    useAppStore.getState().approvePack(samplePack.id),
    /did not confirm approval/,
  );
  assert.deepEqual(routes, [
    'POST /api/pack-control/approval-candidate',
    'POST /api/pack-control/approval-approve',
  ]);
  assert.deepEqual(useAppStore.getState().packApprovalPending, {});
  assert.equal(useAppStore.getState().packs[0].approved, false);
  assert.deepEqual(errors, ['Tobkiri did not confirm approval for the requested Pack.']);
});
