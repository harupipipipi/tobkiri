import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';

import {
  apiFetch,
  approvePack,
  bootstrapPanelSession,
  checkHealth,
  clearApiPrefetchCache,
  createNamedProfile,
  deleteNamedProfile,
  duplicateNamedProfile,
  disablePack,
  enablePack,
  fetchDashboard,
  fetchFrontendContractOperation,
  fetchFrontendCatalog,
  fetchRuntimeOperationStatus,
  fetchPacks,
  fetchNamedProfiles,
  fetchPresentationState,
  installPack,
  invokeFrontendCapability,
  launchSelectedPresentation,
  revokePackApproval,
  restartKernel,
  selectPresentation,
  parseHealthResponse,
  setRuntimeDispatchStatus,
  updateNamedProfile,
} from './api.ts';
import {
  extractExactOperationDescriptors,
  invokeRuntimeOperation,
  RUNTIME_SURFACE_API_VERSION,
} from './runtimeSurface.ts';
import {GENERATED_FRONTEND_CONTRACT_MAP} from './generatedFrontendContractMap.ts';

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, String(value));
  }
}

let lastFetchUrl = '';
let lastFetchInit: RequestInit | undefined;
let exchangeCount = 0;
let presentationCatalogCount = 0;
let presentationSelection: Record<string, unknown> | undefined;
let presentationLaunchCount = 0;
let fetchHandler: ((input: string | URL | Request, init?: RequestInit) => Promise<Response>) | null = null;

function installBrowser(href = 'http://127.0.0.1:8765/panel/'): void {
  const storage = new MemoryStorage();
  const windowMock = {
    __TAURI__: {
      core: {
        invoke: async (command: string, args?: Record<string, unknown>) => {
          if (command === 'reauthorize_panel_session') return 'desktop-refresh-code';
          if (command === 'get_presentation_catalog') {
            presentationCatalogCount += 1;
            return {
              catalog: {base_packs: [], shell_providers: []},
              selection: null,
              materialization: {status: 'not_selected'},
            };
          }
          if (command === 'select_presentation') {
            presentationSelection = args?.selection as Record<string, unknown>;
            return {
              catalog: {base_packs: [], shell_providers: []},
              selection: args?.selection,
              materialization: {status: 'blocked'},
            };
          }
          if (command === 'launch_selected_presentation') {
            presentationLaunchCount += 1;
            return {
              status: 'launched',
              provider_id: 'shell.tauri.default',
              artifact_id: 'fixture-shell',
              message: 'fixture launched',
            };
          }
          return undefined;
        },
      },
    },
    history: {
      replaceState: (_state: unknown, _title: string, url?: string | URL | null) => {
        windowMock.location.href = new URL(String(url ?? ''), windowMock.location.href).toString();
      },
    },
    location: {href},
  };

  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {title: 'Tobkiri'},
    writable: true,
  });
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: storage,
    writable: true,
  });
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: windowMock,
    writable: true,
  });
}

function installFetchMock(): void {
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    if (lastFetchUrl === '/api/panel/auth/exchange') {
      exchangeCount += 1;
      return new Response(JSON.stringify({
        data: {csrf_token: 'csrf-from-server'},
        success: true,
      }), {headers: {'Content-Type': 'application/json'}});
    }
    if (lastFetchUrl === '/health') {
      return new Response(JSON.stringify({
        data: {
          needs_setup: false,
          panel_ready: true,
          runtime_ready: true,
          runtime_status: 'runtime_ready',
          runtime_error: null,
          host_catalog_verified: true,
          profile_ceremony_available: true,
          active_profile_ready: true,
          launch_ready: true,
          defaults_bootstrap_required: false,
          status: 'ok',
        },
        success: true,
      }), {headers: {'Content-Type': 'application/json'}});
    }
    const route = decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', ''));
    const data = route === 'POST /api/pack-control/approval-candidate'
      ? {candidate_id: 'candidate-one', pack_id: 'pack-a', snapshot_digest: `sha256:${'a'.repeat(64)}`}
      : route === 'GET /api/pack-control/catalog'
        ? {
          profile_id: 'profile-a',
          workspace_id: 'workspace-a',
          profile_revision: 'sha256:profile',
          plan_digest: 'sha256:plan',
          catalog_revision: 'catalog-a',
          packs: [],
          count: 0,
        }
        : {
          pack_id: 'pack-a',
          enabled: true,
          approved: true,
          approval_status: 'approved',
          profile_id: 'profile-a',
          workspace_id: 'workspace-a',
          profile_revision: 'sha256:profile',
          plan_digest: 'sha256:plan',
          catalog_revision: 'catalog-a',
        };
    return new Response(JSON.stringify({data, success: true}), {
      headers: {'Content-Type': 'application/json'},
    });
  };
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: (async (input: string | URL | Request, init?: RequestInit) => {
      if (!fetchHandler) throw new Error('Missing fetch handler');
      return fetchHandler(input, init);
    }) as typeof fetch,
    writable: true,
  });
}

beforeEach(() => {
  clearApiPrefetchCache();
  setRuntimeDispatchStatus('runtime_ready');
  lastFetchUrl = '';
  lastFetchInit = undefined;
  exchangeCount = 0;
  presentationCatalogCount = 0;
  presentationSelection = undefined;
  presentationLaunchCount = 0;
  installBrowser();
  installFetchMock();
});

test('Home and Packs use only exact v4 frontend contract routes', async () => {
  const operations: string[] = [];
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    const route = decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', ''));
    operations.push(route);
    const data = route === 'POST /api/pack-control/approval-candidate'
      ? {candidate_id: 'candidate-one', pack_id: 'pack-a', snapshot_digest: `sha256:${'a'.repeat(64)}`}
      : route === 'GET /api/pack-control/catalog'
        ? {
          profile_id: 'profile-a',
          workspace_id: 'workspace-a',
          profile_revision: 'sha256:profile',
          plan_digest: 'sha256:plan',
          catalog_revision: 'catalog-a',
          packs: [],
          count: 0,
        }
        : {
          pack_id: 'pack-a',
          enabled: true,
          approved: true,
          approval_status: 'approved',
          profile_id: 'profile-a',
          workspace_id: 'workspace-a',
          profile_revision: 'sha256:profile',
          plan_digest: 'sha256:plan',
          catalog_revision: 'catalog-a',
        };
    return new Response(JSON.stringify({data, success: true}), {
      headers: {'Content-Type': 'application/json'},
    });
  };

  await fetchDashboard();
  await fetchPacks();
  await installPack('pack-a');
  await approvePack('pack-a');
  await enablePack('pack-a');
  await disablePack('pack-a');

  assert.deepEqual(operations, [
    'GET /api/home/dashboard',
    'GET /api/pack-control/catalog',
    'POST /api/pack-control/install',
    'POST /api/pack-control/approval-candidate',
    'POST /api/pack-control/approval-approve',
    'POST /api/pack-control/enable',
    'POST /api/pack-control/disable',
  ]);
  assert.equal(lastFetchInit?.method, 'POST');
});

test('Named Profile CRUD uses exact Host routes, payloads, and registry response validation', async () => {
  const digest = (character: string): string => `sha256:${character.repeat(64)}`;
  const profile = (profileId: string, revision: string) => ({
    profile_id: profileId,
    profile_revision: revision,
    profile: {profile_id: profileId, display_name: profileId},
    order: 0,
    parent_revision: null,
    tombstone: false,
    created_at: 1,
    updated_at: 1,
    legacy_ids: [],
  });
  const registry = {
    profile_registry_api_version: 'io.tobkiri.profile-registry.v4',
    generation: 3,
    active_profile_id: 'defaults',
    active_profile_revision: digest('a'),
    profiles: [profile('defaults', digest('a'))],
  };
  const requests: Array<{url: string; method: string; body: unknown}> = [];
  fetchHandler = async (input, init) => {
    requests.push({
      url: String(input),
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return new Response(JSON.stringify({success: true, data: registry}), {
      headers: {'Content-Type': 'application/json'},
    });
  };

  await fetchNamedProfiles();
  await createNamedProfile({
    profile_id: 'work-a',
    display_name: 'Work A',
    source_profile_id: 'defaults',
    expected_store_generation: 3,
  });
  await updateNamedProfile({
    profile_id: 'work-a',
    display_name: 'Work A updated',
    expected_profile_revision: digest('a'),
    expected_store_generation: 3,
  });
  await duplicateNamedProfile({
    profile_id: 'work-a',
    new_profile_id: 'work-b',
    display_name: 'Work B',
    expected_profile_revision: digest('a'),
    expected_store_generation: 3,
  });
  await deleteNamedProfile({
    profile_id: 'work-b',
    expected_profile_revision: digest('a'),
    expected_store_generation: 3,
  });

  assert.deepEqual(requests.map(({url, method}) => `${method} ${url}`), [
    'GET /api/v4/profiles',
    'POST /api/v4/profiles/create',
    'POST /api/v4/profiles/update',
    'POST /api/v4/profiles/duplicate',
    'POST /api/v4/profiles/delete',
  ]);
  assert.deepEqual(requests.slice(1).map((request) => request.body), [
    {
      profile_id: 'work-a',
      display_name: 'Work A',
      source_profile_id: 'defaults',
      expected_store_generation: 3,
    },
    {
      profile_id: 'work-a',
      display_name: 'Work A updated',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
    },
    {
      profile_id: 'work-a',
      new_profile_id: 'work-b',
      display_name: 'Work B',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
    },
    {
      profile_id: 'work-b',
      expected_profile_revision: digest('a'),
      expected_store_generation: 3,
    },
  ]);

  const requestCount = requests.length;
  assert.throws(
    () => createNamedProfile({
      profile_id: 'Work A',
      display_name: 'Rejected',
      source_profile_id: 'defaults',
      expected_store_generation: 3,
    }),
    /canonical Profile ID/,
  );
  assert.equal(requests.length, requestCount);
});

test('Pack approval rejects a candidate or approval response for a different state', async () => {
  const operations: string[] = [];
  fetchHandler = async (input, init) => {
    const route = decodeURIComponent(String(input).replace('/api/contracts/defaultspack/', ''));
    operations.push(route);
    if (route === 'POST /api/pack-control/approval-candidate') {
      return new Response(JSON.stringify({
        success: true,
        data: {
          candidate_id: 'candidate-one',
          pack_id: 'pack-b',
          snapshot_digest: `sha256:${'a'.repeat(64)}`,
        },
      }), {headers: {'Content-Type': 'application/json'}});
    }
    throw new Error(`unexpected route ${route}`);
  };

  await assert.rejects(approvePack('pack-a'), /different Pack/);
  assert.deepEqual(operations, ['POST /api/pack-control/approval-candidate']);
});

test('dynamic catalog and capability invocation use the exact canonical v4 routes', async () => {
  const operations: string[] = [];
  let invocationBody: Record<string, unknown> | undefined;
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    const route = decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', ''));
    operations.push(route);
    if (route === 'POST /api/ui/capability/invoke') {
      invocationBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return new Response(JSON.stringify({success: true, data: {kind: 'stat', size: 12}}), {
        headers: {'Content-Type': 'application/json'},
      });
    }
    return new Response(JSON.stringify({
      success: true,
      data: {
        dynamic_host: {
          version: 'rumi.ui.contribution.v1',
          profile_id: 'profile-a',
          profile_revision: 'sha256:profile-a',
          activation_id: 'activation:profile-a',
          plan_hash: 'sha256:plan-a',
          contributions: [{
            contribution_id: 'file-inspect',
            owner_pack_id: 'rumi_file_inspect_pack',
            label: 'rumi_file_inspect_pack.file-inspect',
            action_contract: 'tobkiri.service.file.inspect.v1',
            operation_id: 'rumi_file_inspect_pack.file-inspect',
          }],
          diagnostics: [],
          quarantined_pack_ids: [],
          catalog_hash: 'sha256:catalog-a',
        },
      },
    }), {headers: {'Content-Type': 'application/json'}});
  };

  const catalog = await fetchFrontendCatalog();
  assert.equal(lastFetchInit?.cache, 'no-store');
  const result = await invokeFrontendCapability({
    profileId: catalog.profile_id,
    profileRevision: catalog.profile_revision,
    activationId: catalog.activation_id,
    planHash: catalog.plan_hash,
    catalogHash: catalog.catalog_hash,
    contributionId: 'file-inspect',
    ownerPackId: 'rumi_file_inspect_pack',
    contractId: 'tobkiri.service.file.inspect.v1',
    payload: {name: 'stat', path: 'docs/example.txt'},
  });

  assert.deepEqual(operations, [
    'GET /api/ui/catalog',
    'POST /api/ui/capability/invoke',
  ]);
  assert.equal(typeof invocationBody?.request_id, 'string');
  assert.equal(typeof invocationBody?.expires_at, 'number');
  assert.deepEqual(invocationBody, {
    request_id: invocationBody?.request_id,
    expires_at: invocationBody?.expires_at,
    profile_id: 'profile-a',
    profile_revision: 'sha256:profile-a',
    activation_id: 'activation:profile-a',
    plan_hash: 'sha256:plan-a',
    catalog_hash: 'sha256:catalog-a',
    contribution_id: 'file-inspect',
    owner_pack_id: 'rumi_file_inspect_pack',
    contract_id: 'tobkiri.service.file.inspect.v1',
    payload: {name: 'stat', path: 'docs/example.txt'},
  });
  assert.deepEqual(result, {kind: 'stat', size: 12});
  assert.doesNotMatch(lastFetchUrl, /api\/v4\/dispatch/);
});

test('capability invocation keeps the supplied request identity in both body and replay-protection header', async () => {
  const requestId = '11111111-1111-4111-8111-111111111111';
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    return new Response(JSON.stringify({success: true, data: {accepted: true}}), {
      headers: {'Content-Type': 'application/json'},
    });
  };

  await invokeFrontendCapability({
    profileId: 'profile-a',
    profileRevision: 'sha256:profile-a',
    activationId: 'activation:profile-a',
    planHash: 'sha256:plan-a',
    catalogHash: 'sha256:catalog-a',
    contributionId: 'contribution-a',
    ownerPackId: 'pack-a',
    contractId: 'contract-a',
    payload: {},
  }, {requestId});

  const headers = lastFetchInit?.headers as Record<string, string>;
  const body = JSON.parse(String(lastFetchInit?.body)) as Record<string, unknown>;
  assert.equal(headers['X-Tobkiri-Request-ID'], requestId);
  assert.equal(body.request_id, requestId);
});

test('operation status uses the canonical GET target and a fresh authenticated request identity', async () => {
  const requestId = '22222222-2222-4222-8222-222222222222';
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    return new Response(JSON.stringify({success: true, data: {state: 'pending'}}), {
      headers: {'Content-Type': 'application/json'},
    });
  };

  const result = await fetchRuntimeOperationStatus(requestId);

  assert.deepEqual(result, {state: 'pending'});
  assert.equal(
    decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', '')),
    `GET /api/runtime-surface/operation-status?request_id=${requestId}`,
  );
  const headers = lastFetchInit?.headers as Record<string, string>;
  assert.match(headers['X-Tobkiri-Request-ID'], /^[0-9a-f-]{36}$/i);
  assert.notEqual(headers['X-Tobkiri-Request-ID'], requestId);
});

test('runtime operation invocation uses only its exact invocation contribution and catalog hash', async () => {
  let body: Record<string, unknown> | undefined;
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    return new Response(JSON.stringify({success: true, data: {accepted: true}}), {
      headers: {'Content-Type': 'application/json'},
    });
  };
  const digest = (character: string): string => `sha256:${character.repeat(64)}`;
  const envelope = {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: 'operations' as const,
    state: 'ready' as const,
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
    data: {},
  };
  const operation = {
    operation_id: 'operation.one',
    contract_id: 'contract.one.v1',
    owner_pack_id: 'provider-pack',
    contribution_id: 'catalog-only-contribution',
    target_provider_id: 'provider.one',
    artifact_digest: digest('1'),
    invocation_contribution_id: 'invocation-contribution',
    invocation_owner_pack_id: 'provider-pack',
    invocation_catalog_hash: digest('c'),
    invocation_reason: null,
    invokable: true,
    catalog_digest: digest('c'),
    activation_id: 'activation:defaults-one',
    function_id: 'function.one',
    function_principal_id: 'principal.function.one',
    caller_function_id: 'caller.function.one',
    authority_reference: 'authority://one',
    route: {
      contract_id: 'contract.one.v1',
      operation_id: 'operation.one',
      function_id: 'function.one',
      provider_pack_id: 'provider-pack',
    },
    schema: {
      input_schema: {
        type: 'object',
        properties: {prompt: {type: 'string'}},
      },
    },
    input_schema: {
      type: 'object',
      properties: {prompt: {type: 'string'}},
    },
  };
  envelope.data = {
    operations: [operation],
    packs: [{
      pack_id: 'provider-pack',
      role: 'provider',
      kind: 'normal',
      version: '1.0.0',
      display_name: 'Provider Pack',
      artifact_digest: digest('1'),
      artifact_ref: `pack-v4://provider-pack@${digest('1')}`,
      installed: true,
      enabled: true,
      approved: true,
      required: false,
      invokable_operations: ['contract.one.v1::operation.one'],
    }],
  };
  const [acceptedOperation] = extractExactOperationDescriptors(envelope.data);
  assert.ok(acceptedOperation);
  assert.equal(acceptedOperation.invocation_catalog_hash, digest('c'));
  assert.equal(acceptedOperation.invocation_contribution_id, 'invocation-contribution');
  assert.equal(acceptedOperation.function_principal_id, 'principal.function.one');
  assert.equal(acceptedOperation.caller_function_id, 'caller.function.one');
  assert.equal(acceptedOperation.authority_reference, 'authority://one');
  assert.equal(acceptedOperation.target_provider_id, 'provider.one');
  assert.deepEqual(acceptedOperation.route, {
    contract_id: 'contract.one.v1',
    operation_id: 'operation.one',
    function_id: 'function.one',
    provider_pack_id: 'provider-pack',
  });
  assert.deepEqual(Object.keys(acceptedOperation.input_schema?.properties ?? {}), ['prompt']);

  const result = await invokeRuntimeOperation({
    envelope,
    operation: acceptedOperation,
    payload: {prompt: 'hello'},
  });
  assert.deepEqual(result, {accepted: true});
  assert.equal(decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', '')), 'POST /api/ui/capability/invoke');
  assert.equal(body?.contribution_id, 'invocation-contribution');
  assert.equal(body?.catalog_hash, digest('c'));
  assert.equal(body?.profile_id, 'defaults');
  assert.equal(body?.profile_revision, digest('a'));
  assert.equal(body?.activation_id, 'activation:defaults-one');
  assert.equal(body?.plan_hash, digest('b'));
  assert.equal(Object.prototype.hasOwnProperty.call(body ?? {}, 'catalog_revision'), false);
  assert.equal(Object.prototype.hasOwnProperty.call(body ?? {}, 'operation_digest'), false);
  assert.deepEqual(body?.payload, {prompt: 'hello'});
  await assert.rejects(
    invokeRuntimeOperation({
      envelope,
      operation: acceptedOperation,
      payload: {prompt: 'hello', unexpected: true},
    }),
    (error: unknown) => error instanceof Error && /not declared by the accepted operation schema/.test(error.message),
  );
});

test('approval revocation uses the exact typed v4 contract route and payload', async () => {
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    return new Response(JSON.stringify({
      data: {
        pack_id: 'pack-a',
        approved: false,
        enabled: false,
        approval_status: 'revoked',
      },
      success: true,
    }), {headers: {'Content-Type': 'application/json'}});
  };

  const response = await revokePackApproval('pack-a');

  assert.equal(
    decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', '')),
    'POST /api/pack-control/approval-revoke',
  );
  assert.deepEqual(JSON.parse(String(lastFetchInit?.body)), {pack_id: 'pack-a'});
  assert.deepEqual(response, {
    pack_id: 'pack-a',
    approved: false,
    enabled: false,
    approval_status: 'revoked',
  });
});

test('kernel restart uses the exact typed v4 contract route', async () => {
  fetchHandler = async (input, init) => {
    lastFetchUrl = String(input);
    lastFetchInit = init;
    return new Response(JSON.stringify({
      data: {restarting: true, message: 'Kernel restart requested.'},
      success: true,
    }), {headers: {'Content-Type': 'application/json'}});
  };

  const response = await restartKernel();

  assert.equal(
    decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', '')),
    'POST /api/pack-control/restart',
  );
  assert.equal(lastFetchInit?.method, 'POST');
  assert.deepEqual(JSON.parse(String(lastFetchInit?.body)), {});
  assert.deepEqual(response, {restarting: true, message: 'Kernel restart requested.'});
});

test('v4 contract failure is surfaced and never treated as a successful fallback', async () => {
  fetchHandler = async (input) => {
    lastFetchUrl = String(input);
    return new Response(JSON.stringify({success: false, data: null, error: 'retired'}), {status: 410});
  };

  await assert.rejects(fetchPacks(), /retired/);
  assert.match(lastFetchUrl, /^\/api\/contracts\/defaultspack\//);
});

test('unsafe frontend requests time out and reject instead of leaving lifecycle controls pending', async () => {
  fetchHandler = async () => new Promise<Response>(() => {});

  await assert.rejects(
    apiFetch('/api/v4/packvm/prepare', {method: 'POST'}, {timeoutMs: 1}),
    /POST request timed out after 1ms: \/api\/v4\/packvm\/prepare/,
  );
});

test('presentation wrappers use Launcher-owned Tauri commands', async () => {
  await fetchPresentationState();
  await selectPresentation({
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
  });
  const result = await launchSelectedPresentation();

  assert.equal(presentationCatalogCount, 1);
  assert.deepEqual(presentationSelection, {
    base_pack_id: 'defaults-basepack',
    shell_provider_id: 'shell.tauri.default',
  });
  assert.equal(presentationLaunchCount, 1);
  assert.equal(result.status, 'launched');
});

test('presentation wrappers fail closed outside Launcher instead of using a retired HTTP route', async () => {
  const windowValue = window as Window & {__TAURI__?: unknown; __TAURI_INTERNALS__?: unknown};
  delete windowValue.__TAURI__;
  delete windowValue.__TAURI_INTERNALS__;

  await assert.rejects(fetchPresentationState(), /only available in Tobkiri Launcher/);
  assert.equal(lastFetchUrl, '');
});

test('panel bootstrap exchanges its session code before setup requests', async () => {
  installBrowser('http://127.0.0.1:8765/panel/setup?code=one-time-code');
  installFetchMock();

  await bootstrapPanelSession();

  assert.equal(exchangeCount, 1);
  assert.equal(lastFetchUrl, '/api/panel/auth/exchange');
  assert.equal(window.location.href, 'http://127.0.0.1:8765/panel/setup');
});

test('setup and health requests remain separate from Pack contract dispatch', async () => {
  await apiFetch('/api/setup/packs');
  assert.equal(lastFetchUrl, '/api/setup/packs');
  await checkHealth();
  assert.equal(lastFetchUrl, '/health');
  await apiFetch('/api/setup/packs/install', {method: 'POST'});
  assert.equal(lastFetchUrl, '/api/setup/packs/install');
  await assert.rejects(
    apiFetch('/api/setup/packs/install'),
    /exact method\/path allowlist/,
  );
  await assert.rejects(
    apiFetch('/api/pack-control/disable', {method: 'POST'}),
    /exact method\/path allowlist/,
  );
});

test('health parsing recognizes reconfirmation and preserves the typed setup path', async () => {
  const health = parseHealthResponse({
    status: 'ok',
    needs_setup: true,
    panel_ready: true,
    runtime_ready: false,
    runtime_status: 'profile_reconfirmation_required',
    runtime_error: 'internal denial detail is not surfaced by the UI',
    host_catalog_verified: true,
    profile_ceremony_available: true,
    active_profile_ready: false,
    launch_ready: false,
    defaults_bootstrap_required: false,
  });
  assert.equal(health.runtime_status, 'profile_reconfirmation_required');
  assert.equal(health.runtime_ready, false);

  setRuntimeDispatchStatus('profile_reconfirmation_required');
  await apiFetch('/api/setup/packs');
  assert.equal(lastFetchUrl, '/api/setup/packs');
  await assert.rejects(
    fetchDashboard(),
    /Profile reconfirmation is required.*Setup first/,
  );
  assert.equal(lastFetchUrl, '/api/setup/packs');
});

test('dispatch gate releases only after the Host publishes runtime_ready', async () => {
  setRuntimeDispatchStatus('profile_reconfirmation_required');
  await assert.rejects(
    fetchFrontendContractOperation('POST', '/api/runtime-surface/profile-change/activate', {
      approval_id: 'approval',
      approval_digest: `sha256:${'a'.repeat(64)}`,
    }),
    /Profile reconfirmation is required/,
  );
  assert.equal(lastFetchUrl, '');

  setRuntimeDispatchStatus('runtime_ready');
  await fetchFrontendContractOperation('GET', '/api/home/dashboard');
  assert.equal(
    decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', '')),
    'GET /api/home/dashboard',
  );
});

test('health parsing rejects an unknown or tampered runtime status', () => {
  assert.throws(
    () => parseHealthResponse({status: 'ok', runtime_status: 'runtime_ready_with_empty_map'}),
    /runtime_status is invalid/,
  );
  assert.throws(
    () => parseHealthResponse({
      status: 'ok',
      needs_setup: false,
      panel_ready: true,
      runtime_ready: 'yes',
      runtime_status: 'runtime_ready',
      runtime_error: null,
    }),
    /runtime_ready is invalid/,
  );
});

test('health parsing accepts only coherent lifecycle relationships across all permutations', () => {
  const statuses = [
    'starting',
    'panel_ready',
    'profile_reconfirmation_required',
    'runtime_ready',
    'error',
  ] as const;
  const booleans = [false, true];
  const errors = [null, 'denied'];

  for (const runtimeStatus of statuses) {
    for (const status of ['ok', 'error'] as const) {
      for (const needsSetup of booleans) {
        for (const panelReady of booleans) {
          for (const runtimeReady of booleans) {
            for (const runtimeError of errors) {
              const candidate = {
                status,
                needs_setup: needsSetup,
                panel_ready: panelReady,
                runtime_ready: runtimeReady,
                runtime_status: runtimeStatus,
                runtime_error: runtimeError,
                host_catalog_verified: true,
                profile_ceremony_available: true,
                active_profile_ready: runtimeReady,
                launch_ready: runtimeReady,
                defaults_bootstrap_required: false,
              };
              const coherent = runtimeStatus === 'starting'
                ? status === 'ok' && !panelReady && !runtimeReady && runtimeError === null
                : runtimeStatus === 'panel_ready'
                  ? status === 'ok' && panelReady && !runtimeReady && runtimeError === null
                  : runtimeStatus === 'profile_reconfirmation_required'
                    ? status === 'ok' && needsSetup && panelReady && !runtimeReady
                      && runtimeError === 'denied'
                    : runtimeStatus === 'runtime_ready'
                      ? status === 'ok' && !needsSetup && panelReady && runtimeReady
                        && runtimeError === null
                      : status === 'error' && panelReady && !runtimeReady
                        && runtimeError === 'denied';
              if (coherent) {
                assert.doesNotThrow(() => parseHealthResponse(candidate));
              } else {
                assert.throws(
                  () => parseHealthResponse(candidate),
                  /contradictory|invalid|empty/,
                  JSON.stringify(candidate),
                );
              }
            }
          }
        }
      }
    }
  }
});

test('exact route allowlist rejects legacy, map-external, wildcard, and malformed host paths', async () => {
  await apiFetch('/api/contracts/defaultspack/GET%20%2Fapi%2Fhome%2Fdashboard');
  assert.equal(lastFetchUrl, '/api/contracts/defaultspack/GET%20%2Fapi%2Fhome%2Fdashboard');
  await apiFetch('/api/v4/packvm/progress?operation_id=one%20two');
  const lastAllowedRequest = lastFetchUrl;

  const invalidRequests: Array<[string, RequestInit?]> = [
    ['/api/contracts/defaultspack/POST%20%2Fapi%2Fhome%2Fdashboard', {method: 'POST'}],
    ['/api/contracts/defaultspack/GET%20%2Fapi%2Fpanel%2Fdashboard'],
    ['/api/contracts/defaultspack/GET%20%2Fapi%2Fruntime-recovery%2Fv4%2Fprofile'],
    ['/api/panel/dashboard'],
    ['/api/panel/startup/profiles'],
    ['/api/panel/auth/exchange'],
    ['/api/runtime-recovery/v4/profile'],
    ['/api/registry/default'],
    ['/api/setup/packs?unexpected=1'],
    ['/api/setup/packs', {method: 'POST'}],
    ['/api/v4/packvm/prepare?unexpected=1', {method: 'POST'}],
    ['/api/v4/packvm/doctor', {method: 'POST'}],
    ['/api/v4/packvm/progress?operation_id=one&unexpected=two'],
    ['/api/v4/packvm/progress?operation_id=one=two'],
    ['/api/v4/packvm/progress?operation_id=one', {method: 'POST'}],
    ['/health', {method: 'POST'}],
  ];
  for (const [path, options] of invalidRequests) {
    await assert.rejects(
      apiFetch(path, options),
      /exact method\/path allowlist/,
      path,
    );
  }
  assert.equal(lastAllowedRequest, '/api/v4/packvm/progress?operation_id=one%20two');
  assert.equal(lastFetchUrl, lastAllowedRequest);
});

test('runtime surface GET guards stay outside the encoded contract operation key', async () => {
  await fetchFrontendContractOperation('GET', '/api/runtime-surface/profile', {
    expected_profile_revision: 'revision-one',
    expected_plan_digest: 'sha256:plan-one',
  });

  assert.equal(
    lastFetchUrl,
    '/api/contracts/defaultspack/GET%20%2Fapi%2Fruntime-surface%2Fprofile?expected_profile_revision=revision-one&expected_plan_digest=sha256%3Aplan-one',
  );
  assert.equal(
    decodeURIComponent(lastFetchUrl.slice('/api/contracts/defaultspack/'.length).split('?')[0]),
    'GET /api/runtime-surface/profile',
  );
  assert.doesNotMatch(lastFetchUrl.split('?')[0], /expected_profile/);
});

test('runtime surface settings target has no guard query and unknown GET keys fail before dispatch', async () => {
  await fetchFrontendContractOperation('GET', '/api/runtime-surface/settings');
  assert.equal(lastFetchUrl, '/api/contracts/defaultspack/GET%20%2Fapi%2Fruntime-surface%2Fsettings');

  assert.throws(
    () => fetchFrontendContractOperation('GET', '/api/runtime-surface/profile', {surface: 'profile'}),
    /unknown key/,
  );
  assert.throws(
    () => fetchFrontendContractOperation('GET', '/api/runtime-surface/profile?surface=profile'),
    /target is invalid/,
  );
  assert.equal(lastFetchUrl, '/api/contracts/defaultspack/GET%20%2Fapi%2Fruntime-surface%2Fsettings');
});

test('frontend contract transport rejects map-external targets, method mismatches, and retired recovery paths', async () => {
  const before = lastFetchUrl;
  assert.throws(
    () => fetchFrontendContractOperation('GET', '/api/not-in-the-map'),
    /not declared by the verified frontend Contract Map|no exact route/i,
  );
  assert.throws(
    () => fetchFrontendContractOperation('POST', '/api/runtime-surface/profile'),
    /not declared by the verified frontend Contract Map|no exact route/i,
  );
  assert.throws(
    () => fetchFrontendContractOperation('GET', '/api/runtime-recovery/v4/profile'),
    /not declared by the verified frontend Contract Map|no exact route/i,
  );
  assert.equal(lastFetchUrl, before);
});

test('every single-target product route is dispatched through the generated map', async () => {
  const singleTargetRoutes = GENERATED_FRONTEND_CONTRACT_MAP.routes.filter(
    (route) => route.targets.length === 1,
  );
  assert.ok(singleTargetRoutes.length > 10);
  for (const route of singleTargetRoutes) {
    await fetchFrontendContractOperation(route.method, route.path);
    assert.equal(
      decodeURIComponent(lastFetchUrl.replace('/api/contracts/defaultspack/', '')).split('?')[0],
      `${route.method} ${route.path}`,
      route.path,
    );
  }
});

test('all generated map bindings use the exact method/path and reject ambiguous capability dispatch', () => {
  assert.throws(
    () => fetchFrontendContractOperation('POST', '/api/ui/capability/invoke'),
    /multiple operations/i,
  );
  assert.throws(
    () => fetchFrontendContractOperation('PUT' as never, '/api/pack-control/catalog'),
    /unsupported|not declared/i,
  );
});
