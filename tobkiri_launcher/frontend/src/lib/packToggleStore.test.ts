import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

import {
  type Pack,
  getPackMutationReconciliationHandle,
  useAppStore,
  waitForPackMutationReconciliation,
} from '@/src/store';
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
const GLOBAL_SURFACE_KEYS = [
  'window',
  'document',
  'navigator',
  'localStorage',
  'sessionStorage',
  'fetch',
] as const;
type GlobalSurfaceKey = (typeof GLOBAL_SURFACE_KEYS)[number];
type GlobalSurfaceSnapshot = {
  [key in GlobalSurfaceKey]: PropertyDescriptor | undefined;
};
let previousGlobals: GlobalSurfaceSnapshot;

function captureGlobalSurface(): GlobalSurfaceSnapshot {
  return Object.fromEntries(
    GLOBAL_SURFACE_KEYS.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]),
  ) as GlobalSurfaceSnapshot;
}

function restoreGlobalSurface(snapshot: GlobalSurfaceSnapshot): void {
  for (const key of GLOBAL_SURFACE_KEYS) {
    const descriptor = snapshot[key];
    if (descriptor) {
      Object.defineProperty(globalThis, key, descriptor);
    } else {
      Reflect.deleteProperty(globalThis, key);
    }
  }
}

beforeEach(() => {
  setRuntimeDispatchStatus('runtime_ready');
  previousState = useAppStore.getState();
  previousGlobals = captureGlobalSurface();
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
  return getPackMutationReconciliationHandle().promise.finally(() => {
    useAppStore.setState(previousState, true);
    dom?.window.close();
    dom = null;
    restoreGlobalSurface(previousGlobals);
  });
});

function binding() {
  return {
    profile_id: samplePack.profileId,
    workspace_id: samplePack.workspaceId,
    profile_revision: samplePack.profileRevision,
    plan_digest: samplePack.planDigest,
    catalog_revision: samplePack.catalogRevision,
  };
}

function catalogPack(enabled: boolean) {
  return {
    pack_id: samplePack.id,
    name: samplePack.name,
    version: samplePack.version,
    description: samplePack.description,
    is_core: false,
    installed: true,
    enabled,
    artifact_digest: samplePack.artifactDigest,
    approval_status: 'approved',
    approval_reason: null,
    approved: true,
    hash_valid: true,
    critical_changed: false,
    approval_issues: [],
    ...binding(),
  };
}

function dynamicCatalog() {
  return {
    version: 'rumi.ui.contribution.v1',
    profile_id: samplePack.profileId,
    profile_revision: samplePack.profileRevision,
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

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function normalizeOperationStatusRoute(route: string): string {
  if (!route.startsWith('GET /api/runtime-surface/operation-status?')) return route;
  assert.match(route, /^GET \/api\/runtime-surface\/operation-status\?request_id=[0-9a-f-]{36}$/i);
  return 'GET /api/runtime-surface/operation-status';
}

function installFetch(
  handler: (route: string, init?: RequestInit) => Promise<Response>,
): string[] {
  const routes: string[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const route = decodeURIComponent(url.replace('/api/contracts/defaultspack/', ''));
    routes.push(route);
    return handler(route, init);
  }) as typeof fetch;
  return routes;
}

function setStore(errors: string[]): void {
  useAppStore.setState({
    packs: [samplePack],
    packTogglePending: {},
    frontendCatalog: null,
    frontendCatalogError: null,
    packVmDoctor: healthyDoctor,
    addToast: (message, type) => {
      if (type === 'error') errors.push(message);
    },
  });
}

test('disable waits for the typed response, refreshes state, and survives a later catalog reload', async () => {
  let serverEnabled = true;
  const routes = installFetch(async (route, init) => {
    if (route === 'POST /api/pack-control/disable') {
      assert.deepEqual(JSON.parse(String(init?.body)), {pack_id: samplePack.id});
      serverEnabled = false;
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, enabled: false},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(serverEnabled)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const errors: string[] = [];
  setStore(errors);

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), true);
  assert.equal(useAppStore.getState().packs[0].enabled, false);
  assert.deepEqual(useAppStore.getState().packTogglePending, {});
  assert.deepEqual(errors, []);

  await useAppStore.getState().loadPacks();
  assert.equal(useAppStore.getState().packs[0].enabled, false);
  assert.equal(routes[0], 'POST /api/pack-control/disable');
  assert.deepEqual(routes.slice(1).sort(), [
    'GET /api/pack-control/catalog',
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ].sort());
});

test('install confirms the exact Pack response and reconciles every affected surface', async () => {
  const availablePack: Pack = {
    ...samplePack,
    installed: false,
    enabled: false,
    approved: false,
    approvalStatus: 'available',
    approvalReason: 'install_required',
    approvalIssues: ['install_required'],
  };
  const routes = installFetch(async (route, init) => {
    if (route === 'POST /api/pack-control/install') {
      assert.deepEqual(JSON.parse(String(init?.body)), {pack_id: samplePack.id});
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, installed: true},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(true)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const successes: string[] = [];
  useAppStore.setState({
    packs: [availablePack],
    packInstallPending: {},
    packVmDoctor: healthyDoctor,
    addToast: (message, type) => {
      if (type === 'success') successes.push(message);
    },
  });

  await useAppStore.getState().installPack(samplePack.id);

  assert.deepEqual(routes, [
    'POST /api/pack-control/install',
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ]);
  assert.equal(useAppStore.getState().packs[0].installed, true);
  assert.deepEqual(useAppStore.getState().packInstallPending, {});
  assert.deepEqual(successes, ['Pack installed.']);
});

test('install remains confirmed when the PackVM-owned frontend catalog is intentionally unavailable', async () => {
  const availablePack: Pack = {
    ...samplePack,
    installed: false,
    enabled: false,
    approved: false,
    approvalStatus: 'available',
    approvalReason: 'install_required',
    approvalIssues: ['install_required'],
  };
  const routes = installFetch(async (route, init) => {
    if (route === 'POST /api/pack-control/install') {
      assert.deepEqual(JSON.parse(String(init?.body)), {pack_id: samplePack.id});
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, installed: true},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/pack-control/catalog');
    return new Response(JSON.stringify({
      success: true,
      data: {...binding(), packs: [catalogPack(false)], count: 1},
    }), {headers: {'Content-Type': 'application/json'}});
  });
  const successes: string[] = [];
  useAppStore.setState({
    packs: [availablePack],
    packInstallPending: {},
    packMutationUnknown: {},
    packVmDoctor: {
      ...healthyDoctor,
      ready: false,
      reason: 'packaged macOS VZ helper production identity is unavailable',
      attestation_digest: null,
    },
    frontendCatalog: null,
    frontendCatalogError: 'packaged macOS VZ helper production identity is unavailable',
    addToast: (message, type) => {
      if (type === 'success') successes.push(message);
    },
  });

  await useAppStore.getState().installPack(samplePack.id);

  assert.deepEqual(routes, [
    'POST /api/pack-control/install',
    'GET /api/pack-control/catalog',
  ]);
  assert.equal(useAppStore.getState().packs[0].installed, true);
  assert.deepEqual(useAppStore.getState().packMutationUnknown, {});
  assert.deepEqual(successes, ['Pack installed.']);
});

test('install remains indeterminate while PackVM doctor readiness is unknown', async () => {
  const availablePack: Pack = {
    ...samplePack,
    installed: false,
    enabled: false,
    approved: false,
    approvalStatus: 'available',
    approvalReason: 'install_required',
    approvalIssues: ['install_required'],
  };
  const routes = installFetch(async (route) => {
    if (route === 'POST /api/pack-control/install') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, installed: true},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(false)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route.startsWith('GET /api/runtime-surface/operation-status?')) {
      return operationStatusResponse(route, 'pack.install');
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  useAppStore.setState({
    packs: [availablePack],
    packInstallPending: {},
    packMutationUnknown: {},
    packVmDoctor: null,
    frontendCatalog: null,
    frontendCatalogError: null,
  });

  await assert.rejects(
    useAppStore.getState().installPack(samplePack.id),
    /mutation result is unknown/i,
  );

  assert.deepEqual(routes.map(normalizeOperationStatusRoute), [
    'POST /api/pack-control/install',
    'GET /api/pack-control/catalog',
    'GET /api/runtime-surface/operation-status',
    'GET /api/pack-control/catalog',
  ]);
  assert.equal(Object.keys(useAppStore.getState().packMutationUnknown).length, 1);
});

test('install refreshes the PackVM catalog when readiness becomes available in flight', async () => {
  const availablePack: Pack = {
    ...samplePack,
    installed: false,
    enabled: false,
    approved: false,
    approvalStatus: 'available',
    approvalReason: 'install_required',
    approvalIssues: ['install_required'],
  };
  const routes = installFetch(async (route) => {
    if (route === 'POST /api/pack-control/install') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, installed: true},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      useAppStore.setState({packVmDoctor: healthyDoctor});
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(false)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  useAppStore.setState({
    packs: [availablePack],
    packInstallPending: {},
    packMutationUnknown: {},
    packVmDoctor: {
      ...healthyDoctor,
      ready: false,
      reason: 'PackVM startup is pending',
      attestation_digest: null,
    },
    frontendCatalog: null,
    frontendCatalogError: null,
  });

  await useAppStore.getState().installPack(samplePack.id);

  assert.deepEqual(routes, [
    'POST /api/pack-control/install',
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ]);
  assert.deepEqual(useAppStore.getState().packMutationUnknown, {});
});

test('disable denial leaves the Pack enabled, clears pending, and surfaces the server error', async () => {
  const routes = installFetch(async (route) => {
    assert.equal(route, 'POST /api/pack-control/disable');
    return new Response(JSON.stringify({
      success: false,
      data: null,
      error: 'HTTP 409 pack_disable_denied',
    }), {status: 409, headers: {'Content-Type': 'application/json'}});
  });
  const errors: string[] = [];
  setStore(errors);

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  assert.equal(useAppStore.getState().packs[0].enabled, true);
  assert.deepEqual(useAppStore.getState().packTogglePending, {});
  assert.deepEqual(routes, ['POST /api/pack-control/disable']);
  assert.deepEqual(errors, ['HTTP 409 pack_disable_denied']);
});

test('disable timeout leaves the Pack enabled and does not leave a stuck switch', async () => {
  const routes = installFetch(async (route) => {
    if (route === 'POST /api/pack-control/disable') {
      throw new Error('POST request timed out after 10000ms: /api/pack-control/disable');
    }
    if (route.startsWith('GET /api/runtime-surface/operation-status?')) {
      return operationStatusResponse(route, 'pack.disable');
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(true)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const errors: string[] = [];
  setStore(errors);

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  assert.equal(useAppStore.getState().packs[0].enabled, true);
  assert.deepEqual(useAppStore.getState().packTogglePending, {});
  assert.equal(Object.keys(useAppStore.getState().packMutationUnknown).length, 1);
  assert.equal(routes[0], 'POST /api/pack-control/disable');
  assert.deepEqual(routes.slice(1).map(normalizeOperationStatusRoute).sort(), [
    'GET /api/pack-control/catalog',
    'GET /api/runtime-surface/operation-status',
    'GET /api/ui/catalog',
  ].sort());
  assert.equal(errors.length, 2);
});

test('a stale catalog response cannot re-enable a Pack after a confirmed disable', async () => {
  let catalogReads = 0;
  const routes = installFetch(async (route, init) => {
    if (route === 'POST /api/pack-control/disable') {
      assert.deepEqual(JSON.parse(String(init?.body)), {pack_id: samplePack.id});
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, enabled: false},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      catalogReads += 1;
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(catalogReads === 1)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const errors: string[] = [];
  setStore(errors);

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), true);
  assert.equal(useAppStore.getState().packs[0].enabled, false);
  assert.equal(catalogReads, 1);
  assert.deepEqual(errors, []);
  assert.equal(routes[0], 'POST /api/pack-control/disable');
});

test('delayed restart reconciliation is quiescent before jsdom cleanup', async () => {
  let statusReads = 0;
  let catalogReads = 0;
  installFetch(async (route) => {
    if (route === 'POST /api/pack-control/disable') {
      throw new Error('POST request timed out after 10000ms: /api/pack-control/disable');
    }
    if (route.startsWith('GET /api/runtime-surface/operation-status?')) {
      statusReads += 1;
      await delay(15);
      return operationStatusResponse(route, 'pack.disable');
    }
    if (route === 'GET /api/pack-control/catalog') {
      catalogReads += 1;
      await delay(catalogReads === 1 ? 20 : 5);
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(true)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    assert.equal(route, 'GET /api/ui/catalog');
    await delay(10);
    return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
      headers: {'Content-Type': 'application/json'},
    });
  });
  const errors: string[] = [];
  setStore(errors);

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  await useAppStore.getState().loadPacks();
  await waitForPackMutationReconciliation();
  assert.ok(statusReads >= 2, 'expected direct and hydrated status reads');
  assert.ok(catalogReads >= 2, 'expected direct and hydrated refresh reads');

  const activeDom = dom;
  assert.ok(activeDom);
  activeDom.window.close();
  dom = null;
  await delay(0);
  assert.deepEqual(errors, [
    'The request result is unknown. Refresh the authoritative projection before trying again; no new request will be sent automatically.',
  ]);
});

test('disable ignores a response for the wrong Pack or requested state', async () => {
  const routes = installFetch(async (route) => {
    assert.equal(route, 'POST /api/pack-control/disable');
    return new Response(JSON.stringify({
      success: true,
      data: {...binding(), pack_id: 'other-pack', enabled: true},
    }), {headers: {'Content-Type': 'application/json'}});
  });
  const errors: string[] = [];
  setStore(errors);

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  assert.equal(useAppStore.getState().packs[0].enabled, true);
  assert.deepEqual(useAppStore.getState().packTogglePending, {});
  assert.deepEqual(routes, ['POST /api/pack-control/disable']);
  assert.deepEqual(errors, ['Tobkiri did not confirm the requested Pack state.']);
});

test('disable rejects a duplicate submission while the first request is pending', async () => {
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const routes = installFetch(async (route, init) => {
    if (route === 'POST /api/pack-control/disable') {
      assert.deepEqual(JSON.parse(String(init?.body)), {pack_id: samplePack.id});
      await pending;
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), pack_id: samplePack.id, enabled: false},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/pack-control/catalog') {
      return new Response(JSON.stringify({
        success: true,
        data: {...binding(), packs: [catalogPack(false)], count: 1},
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (route === 'GET /api/ui/catalog') {
      return new Response(JSON.stringify({success: true, data: {dynamic_host: dynamicCatalog()}}), {
        headers: {'Content-Type': 'application/json'},
      });
    }
    assert.fail(`unexpected route: ${route}`);
  });
  const errors: string[] = [];
  setStore(errors);

  const first = useAppStore.getState().togglePack(samplePack.id);
  assert.equal(useAppStore.getState().packTogglePending[samplePack.id], true);
  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  assert.deepEqual(routes, ['POST /api/pack-control/disable']);

  release?.();
  assert.equal(await first, true);
  assert.equal(useAppStore.getState().packs[0].enabled, false);
  assert.deepEqual(useAppStore.getState().packTogglePending, {});
  assert.deepEqual(errors, []);
  assert.deepEqual(routes.slice(1).sort(), [
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ].sort());
});

test('required Profile Pack is rejected before a disable request is sent', async () => {
  const routes = installFetch(async (route) => {
    assert.fail(`unexpected route: ${route}`);
  });
  const errors: string[] = [];
  setStore(errors);
  useAppStore.setState({packs: [{...samplePack, required: true}]});

  assert.equal(await useAppStore.getState().togglePack(samplePack.id), false);
  assert.deepEqual(routes, []);
  assert.equal(useAppStore.getState().packs[0].enabled, true);
  assert.deepEqual(useAppStore.getState().packTogglePending, {});
});
