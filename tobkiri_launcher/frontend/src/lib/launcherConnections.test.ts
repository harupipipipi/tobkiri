import assert from 'node:assert/strict';
import test from 'node:test';

import {
  fetchLauncherApprovalStatus,
  fetchLauncherConnectionsSnapshot,
  fetchLauncherModelProfiles,
  isLauncherActiveProfileContext,
  isLauncherConnectionsSnapshot,
  isLauncherModelProfileList,
  isLauncherProviderEffectStatus,
  saveLauncherModelProfile,
  validateLauncherModelProfileInput,
  validateLauncherProviderSetup,
  type LauncherContractInvoker,
} from './launcherConnections';

const scope = {
  profile_id: 'defaults',
  profile_revision: `sha256:${'a'.repeat(64)}`,
  catalog_revision: `sha256:${'b'.repeat(64)}`,
  plan_digest: `sha256:${'c'.repeat(64)}`,
  activation_id: 'activation:testactivation1',
};

function providerEntry(overrides: Record<string, unknown> = {}) {
  return {
    provider_instance_id: 'provider.openai.default',
    display_name: 'openai.default',
    enabled: true,
    credential_status: 'configured',
    health_status: 'verified',
    reachability: 'available',
    observed_at: 1700000000,
    ...overrides,
  };
}

function connectionsSnapshot(overrides: Record<string, unknown> = {}) {
  return {revision: 3, providers: [providerEntry()], ...overrides};
}

function modelList(overrides: Record<string, unknown> = {}) {
  return {
    profiles: [
      {
        profile_id: 'main-chat',
        display_name: 'Main chat',
        model_id: 'gpt-4o-mini',
        provider_id: 'provider.openai.default',
        route_configured: true,
      },
    ],
    count: 1,
    registry_revision: 7,
    ...overrides,
  };
}

function recordingInvoker(
  responses: Record<string, unknown>,
): {invoker: LauncherContractInvoker; calls: Array<{method: string; target: string; payload?: Record<string, unknown>}>} {
  const calls: Array<{method: string; target: string; payload?: Record<string, unknown>}> = [];
  const invoker: LauncherContractInvoker = async <T>(
    method: string,
    target: string,
    payload?: Record<string, unknown>,
  ): Promise<T> => {
    calls.push({method, target, payload});
    const key = `${method} ${target}`;
    if (!(key in responses)) throw new Error(`unexpected call ${key}`);
    return responses[key] as T;
  };
  return {invoker, calls};
}

test('active Profile context requires the verified activation id shape', () => {
  assert.equal(isLauncherActiveProfileContext(scope), true);
  assert.equal(isLauncherActiveProfileContext({...scope, activation_id: 'nope'}), false);
  assert.equal(isLauncherActiveProfileContext({...scope, profile_id: ''}), false);
  assert.equal(isLauncherActiveProfileContext(null), false);
  assert.equal(isLauncherActiveProfileContext('defaults'), false);
});

test('connections snapshot requires the exact published shape', () => {
  assert.equal(isLauncherConnectionsSnapshot(connectionsSnapshot()), true);
  assert.equal(
    isLauncherConnectionsSnapshot(connectionsSnapshot({extra: true})),
    false,
  );
  assert.equal(
    isLauncherConnectionsSnapshot(connectionsSnapshot({revision: -1})),
    false,
  );
  assert.equal(
    isLauncherConnectionsSnapshot(
      connectionsSnapshot({providers: [providerEntry({extra: 'x'})]}),
    ),
    false,
  );
  assert.equal(
    isLauncherConnectionsSnapshot(
      connectionsSnapshot({providers: [providerEntry({credential_status: 'odd'})]}),
    ),
    false,
  );
  assert.equal(
    isLauncherConnectionsSnapshot(
      connectionsSnapshot({providers: [providerEntry({observed_at: 'soon'})]}),
    ),
    false,
  );
});

test('model profile list requires registry_revision and per-profile ids', () => {
  assert.equal(isLauncherModelProfileList(modelList()), true);
  assert.equal(
    isLauncherModelProfileList(modelList({registry_revision: '7'})),
    false,
  );
  assert.equal(
    isLauncherModelProfileList(modelList({profiles: [{profile_id: '', display_name: 'x'}]})),
    false,
  );
  assert.equal(
    isLauncherModelProfileList(modelList({profiles: [{profile_id: 'x', display_name: 'x', route_configured: 'yes'}]})),
    false,
  );
});

test('effect status accepts a null approval id and rejects empty effect ids', () => {
  assert.equal(
    isLauncherProviderEffectStatus({
      effect_id: 'effect-1',
      approval_request_id: null,
      state: 'prepared',
    }),
    true,
  );
  assert.equal(
    isLauncherProviderEffectStatus({effect_id: '', approval_request_id: null, state: 'prepared'}),
    false,
  );
});

test('provider setup validation builds the exact request for a hosted preset', () => {
  const result = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'default',
    key: 'sk-synthetic-test-key',
  });
  assert.ok('request' in result);
  assert.deepEqual(result.request, {
    connection_name: 'openai.default',
    protocol: 'openai-compatible',
    endpoint: 'https://api.openai.com/v1',
    key_value: 'sk-synthetic-test-key',
  });
  assert.equal('capabilities' in result.request, false);
});

test('provider setup validation enforces the loopback-only keyless shape', () => {
  const ok = validateLauncherProviderSetup({
    providerId: 'local',
    apiId: 'default',
    endpoint: 'http://127.0.0.1:8080/v1',
    protocol: 'local-openai-compatible',
    key: '',
  });
  assert.ok('request' in ok);
  assert.equal(ok.request.endpoint, 'http://127.0.0.1:8080/v1');

  const badPort = validateLauncherProviderSetup({
    providerId: 'local',
    apiId: 'default',
    endpoint: 'http://127.0.0.1:80/v1',
    protocol: 'local-openai-compatible',
    key: '',
  });
  assert.ok('error' in badPort);

  const withKey = validateLauncherProviderSetup({
    providerId: 'local',
    apiId: 'default',
    endpoint: 'http://127.0.0.1:8080/v1',
    protocol: 'local-openai-compatible',
    key: 'not-empty',
  });
  assert.ok('error' in withKey);

  const nonLoopback = validateLauncherProviderSetup({
    providerId: 'local',
    apiId: 'default',
    endpoint: 'http://192.168.1.5:8080/v1',
    protocol: 'local-openai-compatible',
    key: '',
  });
  assert.ok('error' in nonLoopback);
});

test('provider setup validation rejects unsafe hosted inputs', () => {
  const httpOnly = validateLauncherProviderSetup({
    providerId: 'openai_compatible',
    apiId: 'default',
    endpoint: 'http://example.com/v1',
    protocol: 'openai-compatible',
    key: 'sk-x',
  });
  assert.ok('error' in httpOnly);

  const emptyKey = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'default',
    key: '',
  });
  assert.ok('error' in emptyKey);

  const keyInEndpoint = validateLauncherProviderSetup({
    providerId: 'openai_compatible',
    apiId: 'default',
    endpoint: 'https://sk-x.example.com/v1',
    protocol: 'openai-compatible',
    key: 'sk-x',
  });
  assert.ok('error' in keyInEndpoint);

  const badName = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'bad suffix!',
    key: 'sk-x',
  });
  assert.ok('error' in badName);
});

test('audio capabilities are explicit, ordered, and hosted openai-compatible only', () => {
  const granted = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'default',
    key: 'sk-synthetic',
    capabilities: ['ai.audio.speech', 'ai.stream', 'ai.generate', 'ai.audio.transcribe'],
  });
  assert.ok('request' in granted);
  assert.deepEqual(granted.request.capabilities, [
    'ai.generate',
    'ai.stream',
    'ai.audio.transcribe',
    'ai.audio.speech',
  ]);

  const anthropic = validateLauncherProviderSetup({
    providerId: 'anthropic',
    apiId: 'default',
    key: 'sk-synthetic',
    capabilities: ['ai.generate', 'ai.stream', 'ai.audio.transcribe'],
  });
  assert.ok('error' in anthropic);

  const missingText = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'default',
    key: 'sk-synthetic',
    capabilities: ['ai.audio.transcribe'],
  });
  assert.ok('error' in missingText);

  const empty = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'default',
    key: 'sk-synthetic',
    capabilities: [],
  });
  assert.ok('error' in empty);

  const unknown = validateLauncherProviderSetup({
    providerId: 'openai',
    apiId: 'default',
    key: 'sk-synthetic',
    capabilities: ['ai.generate', 'ai.stream', 'ai.magic'] as never,
  });
  assert.ok('error' in unknown);

  const localAudio = validateLauncherProviderSetup({
    providerId: 'local',
    apiId: 'default',
    endpoint: 'http://127.0.0.1:8080/v1',
    protocol: 'local-openai-compatible',
    key: '',
    capabilities: ['ai.generate', 'ai.stream', 'ai.audio.transcribe'],
  });
  assert.ok('error' in localAudio);
});

test('model profile input validation mirrors the backend normalizer', () => {
  const input = {
    model_profile_id: 'main-chat',
    model_id: 'gpt-4o-mini',
    provider_instance_id: 'provider.openai.default',
    display_name: 'Main chat',
  };
  assert.equal(validateLauncherModelProfileInput(input), null);
  assert.ok(validateLauncherModelProfileInput({...input, model_profile_id: 'bad id!'}));
  assert.ok(validateLauncherModelProfileInput({...input, display_name: '   '}));
  assert.ok(validateLauncherModelProfileInput({...input, display_name: ' padded '}));
});

test('model profile save reads current revisions and posts the six allowed keys', async () => {
  const saved = modelList({
    profiles: [
      {
        profile_id: 'alt-chat',
        display_name: 'Alt chat',
        model_id: 'gpt-4o-mini',
        provider_id: 'provider.openai.default',
        route_configured: true,
      },
    ],
    count: 1,
    registry_revision: 8,
  });
  const {invoker, calls} = recordingInvoker({
    'GET /api/connections/status': connectionsSnapshot(),
    'GET /api/ai/profiles': modelList(),
    'POST /api/ai/profiles': saved,
  });
  const result = await saveLauncherModelProfile(
    {
      model_profile_id: 'alt-chat',
      model_id: 'gpt-4o-mini',
      provider_instance_id: 'provider.openai.default',
      display_name: 'Alt chat',
    },
    invoker,
  );
  assert.equal(result.profile_id, 'alt-chat');
  const post = calls.find((call) => call.method === 'POST');
  assert.ok(post);
  assert.deepEqual(Object.keys(post.payload ?? {}).sort(), [
    'display_name',
    'expected_revision',
    'model_id',
    'model_profile_id',
    'provider_instance_id',
    'provider_registry_revision',
  ]);
  assert.equal(post.payload?.expected_revision, 7);
  assert.equal(post.payload?.provider_registry_revision, 3);
});

test('model profile save refuses a missing or disabled provider connection', async () => {
  const {invoker} = recordingInvoker({
    'GET /api/connections/status': connectionsSnapshot({
      providers: [providerEntry({enabled: false})],
    }),
    'GET /api/ai/profiles': modelList(),
  });
  await assert.rejects(
    saveLauncherModelProfile(
      {
        model_profile_id: 'alt-chat',
        model_id: 'gpt-4o-mini',
        provider_instance_id: 'provider.openai.default',
        display_name: 'Alt chat',
      },
      invoker,
    ),
    /disabled/,
  );
});

test('model profile save refuses an id collision with different details', async () => {
  const {invoker} = recordingInvoker({
    'GET /api/connections/status': connectionsSnapshot(),
    'GET /api/ai/profiles': modelList(),
  });
  await assert.rejects(
    saveLauncherModelProfile(
      {
        model_profile_id: 'main-chat',
        model_id: 'different-model',
        provider_instance_id: 'provider.openai.default',
        display_name: 'Main chat',
      },
      invoker,
    ),
    /already exists/,
  );
});

test('model profile save fails closed when the saved record does not match', async () => {
  const mismatched = modelList({
    profiles: [
      {
        profile_id: 'alt-chat',
        display_name: 'Alt chat',
        model_id: 'other-model',
        provider_id: 'provider.openai.default',
        route_configured: true,
      },
    ],
  });
  const {invoker} = recordingInvoker({
    'GET /api/connections/status': connectionsSnapshot(),
    'GET /api/ai/profiles': modelList(),
    'POST /api/ai/profiles': mismatched,
  });
  await assert.rejects(
    saveLauncherModelProfile(
      {
        model_profile_id: 'alt-chat',
        model_id: 'gpt-4o-mini',
        provider_instance_id: 'provider.openai.default',
        display_name: 'Alt chat',
      },
      invoker,
    ),
    /did not match/,
  );
});

test('read helpers reject malformed contract responses', async () => {
  const {invoker: badConnections} = recordingInvoker({
    'GET /api/connections/status': {revision: 'x'},
  });
  await assert.rejects(fetchLauncherConnectionsSnapshot(badConnections), /invalid/);

  const {invoker: badModels} = recordingInvoker({
    'GET /api/ai/profiles': {profiles: [], count: 0},
  });
  await assert.rejects(fetchLauncherModelProfiles(badModels), /invalid/);

  const {invoker: badApproval} = recordingInvoker({
    'POST /api/interactive-approval/v1/get': {state: 'pending'},
  });
  await assert.rejects(fetchLauncherApprovalStatus('req-1', badApproval), /invalid/);

  const {invoker: goodApproval, calls} = recordingInvoker({
    'POST /api/interactive-approval/v1/get': {request_id: 'req-1', state: 'pending'},
  });
  const status = await fetchLauncherApprovalStatus('req-1', goodApproval);
  assert.equal(status.request_id, 'req-1');
  assert.deepEqual(calls[0].payload, {request_id: 'req-1'});
});
