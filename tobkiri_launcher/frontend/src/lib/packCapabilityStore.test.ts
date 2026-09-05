import assert from 'node:assert/strict';
import {afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

import {type Pack, useAppStore} from '@/src/store';
import type {ApiPackVMDoctor} from '@/src/lib/apiTypes';
import {setRuntimeDispatchStatus} from '@/src/lib/runtimeDispatchGate';

const operation = {
  operation_id: 'rumi_file_inspect_pack.file-inspect',
  contract_id: 'tobkiri.service.file.inspect.v1',
  provider_id: 'rumi_file_inspect_pack.file-inspect.service',
  capabilities: ['file.inspect'],
  input_schema: {type: 'object'},
  invokable: true,
};

const samplePack: Pack = {
  id: 'rumi_file_inspect_pack',
  name: 'Tobkiri File Inspect',
  version: '1.0.0',
  type: 'community',
  installed: true,
  enabled: true,
  description: 'Inspect selected workspace files.',
  artifactDigest: 'sha256:artifact',
  profileId: 'profile-a',
  workspaceId: 'workspace-a',
  profileRevision: 'sha256:profile',
  planDigest: 'sha256:plan',
  catalogRevision: 'catalog-a',
  approvalStatus: 'approved',
  approvalReason: null,
  approved: true,
  hashValid: true,
  criticalChanged: false,
  approvalIssues: [],
  capabilities: [{name: 'file.inspect', description: 'Inspect files.'}],
  operations: [{
    operationId: operation.operation_id,
    contractId: operation.contract_id,
    providerId: operation.provider_id,
    capabilities: operation.capabilities,
    inputSchema: operation.input_schema,
    invokable: true,
  }],
  flows: [operation.operation_id],
  dependencies: [],
};

const frontendCatalog = {
  version: 'rumi.ui.contribution.v1',
  profile_id: 'profile-a',
  profile_revision: 'sha256:profile',
  activation_id: 'activation:profile-a',
  plan_hash: 'sha256:plan',
  contributions: [{
    contribution_id: 'file-inspect',
    owner_pack_id: samplePack.id,
    label: operation.operation_id,
    operation_id: operation.operation_id,
    provider_id: operation.provider_id,
    action_contract: operation.contract_id,
    resolved_profile_id: samplePack.profileId,
    resolved_profile_revision: samplePack.profileRevision,
    resolved_activation_id: 'activation:profile-a',
    resolved_plan_hash: samplePack.planDigest,
  }],
  diagnostics: [],
  quarantined_pack_ids: [],
  catalog_hash: 'sha256:catalog',
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

function packApiRecord() {
  return {
    pack_id: samplePack.id,
    name: samplePack.name,
    version: samplePack.version,
    description: samplePack.description,
    is_core: false,
    installed: true,
    enabled: true,
    artifact_digest: samplePack.artifactDigest,
    approval_status: 'approved',
    approval_reason: null,
    approved: true,
    hash_valid: true,
    critical_changed: false,
    approval_issues: [],
    capabilities: samplePack.capabilities,
    operations: [operation],
    flows: [operation.operation_id],
    dependencies: [],
    profile_id: samplePack.profileId,
    workspace_id: samplePack.workspaceId,
    profile_revision: samplePack.profileRevision,
    plan_digest: samplePack.planDigest,
    catalog_revision: samplePack.catalogRevision,
  };
}

function installFetch(
  handler: (route: string, init?: RequestInit) => Promise<Response>,
): {routes: string[]; bodies: Record<string, unknown>[]} {
  const routes: string[] = [];
  const bodies: Record<string, unknown>[] = [];
  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const route = decodeURIComponent(url.replace('/api/contracts/defaultspack/', ''));
    routes.push(route);
    if (init?.body) bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    return handler(route, init);
  }) as typeof fetch;
  return {routes, bodies};
}

function catalogResponse(): Response {
  return new Response(JSON.stringify({
    success: true,
    data: {dynamic_host: frontendCatalog},
  }), {headers: {'Content-Type': 'application/json'}});
}

function packsResponse(): Response {
  return new Response(JSON.stringify({
    success: true,
    data: {
      profile_id: samplePack.profileId,
      workspace_id: samplePack.workspaceId,
      profile_revision: samplePack.profileRevision,
      plan_digest: samplePack.planDigest,
      catalog_revision: samplePack.catalogRevision,
      packs: [packApiRecord()],
      count: 1,
    },
  }), {headers: {'Content-Type': 'application/json'}});
}

function operationStatusResponse(route: string, operationId: string, contractId: string): Response {
  const requestId = route.match(/[?&]request_id=([^&]+)/)?.[1];
  assert.ok(requestId);
  return new Response(JSON.stringify({
    success: true,
    data: {
      runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
      operation_status_api_version: 'io.tobkiri.control-operation-status.v1',
      request_id: requestId,
      operation_id: operationId,
      contract_id: contractId,
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

function readyState(): void {
  useAppStore.setState({
    packs: [samplePack],
    frontendCatalog,
    frontendCatalogError: null,
    frontendCatalogLoading: false,
    packVmDoctor: healthyDoctor,
    packOperationPending: {},
  });
}

test('store invokes only the verified Pack contribution and refreshes both projections', async () => {
  readyState();
  const {routes, bodies} = installFetch(async (route) => {
    if (route === 'POST /api/ui/capability/invoke') {
      return new Response(JSON.stringify({success: true, data: {kind: 'stat', size: 12}}), {
        headers: {'Content-Type': 'application/json'},
      });
    }
    if (route === 'GET /api/pack-control/catalog') return packsResponse();
    if (route === 'GET /api/ui/catalog') return catalogResponse();
    throw new Error(`unexpected route ${route}`);
  });

  const result = await useAppStore.getState().invokePackOperation(
    samplePack.id,
    operation.operation_id,
    {name: 'stat', path: 'docs/example.txt', profile_id: samplePack.profileId, workspace_id: samplePack.workspaceId},
  );

  assert.deepEqual(result, {kind: 'stat', size: 12});
  assert.equal(routes[0], 'POST /api/ui/capability/invoke');
  assert.deepEqual(routes.slice(1).sort(), [
    'GET /api/pack-control/catalog',
    'GET /api/ui/catalog',
  ]);
  assert.equal(bodies.length, 1);
  assert.equal(typeof bodies[0].request_id, 'string');
  assert.equal(bodies[0].profile_id, samplePack.profileId);
  assert.equal(bodies[0].profile_revision, samplePack.profileRevision);
  assert.equal(bodies[0].activation_id, frontendCatalog.activation_id);
  assert.equal(bodies[0].plan_hash, samplePack.planDigest);
  assert.equal(bodies[0].owner_pack_id, samplePack.id);
  assert.equal(bodies[0].contract_id, operation.contract_id);
  assert.deepEqual(bodies[0].payload, {
    name: 'stat',
    path: 'docs/example.txt',
    profile_id: samplePack.profileId,
    workspace_id: samplePack.workspaceId,
  });
  assert.equal(Object.keys(bodies[0]).some((key) => /approved|secret|executor|provider/i.test(key)), false);
  assert.deepEqual(useAppStore.getState().packOperationPending, {});
});

test('missing approval or a revoked Pack fails closed before any capability request', async () => {
  const {routes} = installFetch(async () => {
    throw new Error('network must not be reached');
  });
  const unapproved: Pack = {
    ...samplePack,
    approved: false,
    enabled: false,
    approvalStatus: 'revoked',
    approvalReason: 'approval_revoked',
    approvalIssues: ['approval_revoked'],
  };
  useAppStore.setState({
    packs: [unapproved],
    frontendCatalog,
    packVmDoctor: healthyDoctor,
    packOperationPending: {},
  });

  await assert.rejects(
    useAppStore.getState().invokePackOperation(unapproved.id, operation.operation_id, {
      name: 'stat',
      path: 'docs/example.txt',
    }),
    /installed, approved, enabled Pack/,
  );
  assert.deepEqual(routes, []);
});

test('missing verified contribution fails closed instead of falling back to a provider', async () => {
  const {routes} = installFetch(async () => {
    throw new Error('provider fallback must not be reached');
  });
  useAppStore.setState({
    packs: [samplePack],
    frontendCatalog: {...frontendCatalog, contributions: []},
    packVmDoctor: healthyDoctor,
    packOperationPending: {},
  });

  await assert.rejects(
    useAppStore.getState().invokePackOperation(samplePack.id, operation.operation_id, {
      name: 'stat',
      path: 'docs/example.txt',
    }),
    /not exposed this Pack operation/,
  );
  assert.deepEqual(routes, []);
});

test('typed capability failure does not refresh or leave optimistic result state', async () => {
  readyState();
  const {routes} = installFetch(async (route) => {
    assert.equal(route, 'POST /api/ui/capability/invoke');
    return new Response(JSON.stringify({success: false, data: null, error: 'capability_denied'}), {
      status: 409,
      headers: {'Content-Type': 'application/json'},
    });
  });

  await assert.rejects(
    useAppStore.getState().invokePackOperation(samplePack.id, operation.operation_id, {
      name: 'stat',
      path: 'docs/example.txt',
    }),
    /capability_denied/,
  );
  assert.deepEqual(routes, ['POST /api/ui/capability/invoke']);
  assert.deepEqual(useAppStore.getState().packs, [samplePack]);
  assert.deepEqual(useAppStore.getState().packOperationPending, {});
});

test('double invocation is rejected while the first canonical request is pending', async () => {
  readyState();
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const {routes} = installFetch(async (route) => {
    if (route === 'POST /api/ui/capability/invoke') {
      await pending;
      return new Response(JSON.stringify({success: true, data: {ok: true}}), {
        headers: {'Content-Type': 'application/json'},
      });
    }
    if (route === 'GET /api/pack-control/catalog') return packsResponse();
    if (route === 'GET /api/ui/catalog') return catalogResponse();
    throw new Error(`unexpected route ${route}`);
  });

  try {
    const first = useAppStore.getState().invokePackOperation(samplePack.id, operation.operation_id, {
      name: 'stat',
      path: 'docs/example.txt',
    });
    await assert.rejects(
      useAppStore.getState().invokePackOperation(samplePack.id, operation.operation_id, {
        name: 'stat',
        path: 'docs/example.txt',
      }),
      /already in progress/,
    );
    assert.deepEqual(routes, ['POST /api/ui/capability/invoke']);
    release?.();
    await first;
  } finally {
    release?.();
  }
  assert.deepEqual(useAppStore.getState().packOperationPending, {});
});

test('a lost capability response transitions to unknown and never sends a replacement POST', async () => {
  readyState();
  let postCount = 0;
  const {routes} = installFetch(async (route) => {
    if (route === 'POST /api/ui/capability/invoke') {
      postCount += 1;
      throw new Error('POST request timed out after 10000ms');
    }
    if (route.startsWith('GET /api/runtime-surface/operation-status?')) {
      return operationStatusResponse(route, operation.operation_id, operation.contract_id);
    }
    if (route === 'GET /api/pack-control/catalog') return packsResponse();
    if (route === 'GET /api/ui/catalog') return catalogResponse();
    throw new Error(`unexpected route ${route}`);
  });

  await assert.rejects(
    useAppStore.getState().invokePackOperation(samplePack.id, operation.operation_id, {
      name: 'stat',
      path: 'docs/example.txt',
    }),
    /result is unknown/,
  );
  await assert.rejects(
    useAppStore.getState().invokePackOperation(samplePack.id, operation.operation_id, {
      name: 'stat',
      path: 'docs/example.txt',
    }),
    /result is unknown/,
  );
  assert.equal(postCount, 1);
  assert.equal(routes[0], 'POST /api/ui/capability/invoke');
  assert.deepEqual(routes.slice(1).map(normalizeOperationStatusRoute).sort(), [
    'GET /api/pack-control/catalog',
    'GET /api/runtime-surface/operation-status',
    'GET /api/ui/catalog',
  ].sort());
  assert.deepEqual(useAppStore.getState().packOperationPending, {});
  assert.equal(Object.keys(useAppStore.getState().packOperationUnknown).length, 1);
});
