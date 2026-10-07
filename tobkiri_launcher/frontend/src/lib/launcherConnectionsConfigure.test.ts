import assert from 'node:assert/strict';
import test from 'node:test';

import type {SafeStorage} from './safeStorage';
import type {
  LauncherActiveProfileContext,
  LauncherApprovalStatus,
  LauncherProviderConfigureRequest,
  LauncherProviderEffectStatus,
} from './launcherConnections';
import {
  configureLauncherProviderConnection,
  readLauncherProviderPending,
  type LauncherProviderConfigurePorts,
} from './launcherConnectionsConfigure';

const scope: LauncherActiveProfileContext = {
  profile_id: 'defaults',
  profile_revision: `sha256:${'a'.repeat(64)}`,
  catalog_revision: `sha256:${'b'.repeat(64)}`,
  plan_digest: `sha256:${'c'.repeat(64)}`,
  activation_id: 'activation:testactivation1',
};

const otherScope: LauncherActiveProfileContext = {
  ...scope,
  activation_id: 'activation:testactivation2',
};

const request: LauncherProviderConfigureRequest = {
  connection_name: 'openai.default',
  protocol: 'openai-compatible',
  endpoint: 'https://api.openai.com/v1',
  key_value: 'sk-synthetic-test-key',
};

function memoryStorage(): SafeStorage & {map: Map<string, string>} {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (key) => (map.has(key) ? map.get(key)! : null),
    setItem: (key, value) => {
      map.set(key, String(value));
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}

function status(
  state: string,
  effect = 'effect-1',
  approval: string | null = 'approval-1',
): LauncherProviderEffectStatus {
  return {effect_id: effect, approval_request_id: approval, state};
}

interface ScriptedPorts {
  ports: LauncherProviderConfigurePorts;
  calls: Array<{phase: string; detail: unknown}>;
  statusQueue: LauncherProviderEffectStatus[];
}

function scriptedPorts(
  storage: SafeStorage,
  overrides: Partial<LauncherProviderConfigurePorts> = {},
): ScriptedPorts {
  const calls: Array<{phase: string; detail: unknown}> = [];
  const statusQueue: LauncherProviderEffectStatus[] = [];
  const ports: LauncherProviderConfigurePorts = {
    storage,
    prepare: async (req, correlation) => {
      calls.push({phase: 'prepare', detail: {request: req, correlation}});
      return status('approval_pending');
    },
    lookup: async (correlation) => {
      calls.push({phase: 'lookup', detail: {correlation}});
      return status('approval_pending', 'effect-lookup');
    },
    status: async (effect) => {
      calls.push({phase: 'status', detail: {effect}});
      return statusQueue.length > 0 ? statusQueue.shift()! : status('succeeded', effect, null);
    },
    resume: async (effect) => {
      calls.push({phase: 'resume', detail: {effect}});
      return status('dispatched', effect, null);
    },
    cancel: async (effect) => {
      calls.push({phase: 'cancel', detail: {effect}});
      return status('cancelled', effect, null);
    },
    approval: async (id): Promise<LauncherApprovalStatus> => {
      calls.push({phase: 'approval', detail: {id}});
      return {request_id: id, state: 'approved'};
    },
    openApproval: async (id) => {
      calls.push({phase: 'openApproval', detail: {id}});
      return true;
    },
    pause: async () => {},
    ...overrides,
  };
  return {ports, calls, statusQueue};
}

test('prepare is preceded by a pending marker that never contains the key', async () => {
  const storage = memoryStorage();
  let capturedPending: string | null = null;
  let capturedCorrelation: string | null = null;
  // The loop: prepare → prepared → approval approved → resume → dispatched
  // → pause → status → succeeded.
  const {ports} = scriptedPorts(storage, {
    prepare: async (_req, correlation) => {
      capturedCorrelation = correlation;
      const key = [...storage.map.keys()].find((item) => item.includes('pending'));
      assert.ok(key, 'pending marker must exist before prepare is sent');
      capturedPending = storage.map.get(key)!;
      return status('prepared');
    },
    approval: async (id) => ({request_id: id, state: 'approved'}),
    resume: async (effect) => status('dispatched', effect, null),
    status: async (effect) => status('succeeded', effect, null),
  });
  await configureLauncherProviderConnection(request, scope, ports);

  assert.ok(capturedPending);
  const parsed = JSON.parse(capturedPending);
  assert.equal(parsed.effect, null);
  assert.equal(parsed.connection, request.connection_name);
  assert.equal(parsed.correlation, capturedCorrelation);
  assert.equal(parsed.digest.length, 64);
  assert.ok(!capturedPending.includes(request.key_value), 'key must never be persisted');
  assert.equal(storage.map.size, 0, 'pending marker cleared on success');
});

test('a lost prepare response is reconciled by correlation lookup, never replayed', async () => {
  const storage = memoryStorage();
  const prepareCalls: string[] = [];
  const first = scriptedPorts(storage, {
    prepare: async (_req, correlation) => {
      prepareCalls.push(correlation);
      throw new Error('synthetic transport failure');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, first.ports),
    /could not be confirmed/,
  );
  assert.equal(prepareCalls.length, 1);

  const second = scriptedPorts(storage, {
    prepare: async () => {
      throw new Error('prepare must never be replayed');
    },
    lookup: async (correlation) => {
      assert.equal(correlation, prepareCalls[0]);
      return status('approval_pending', 'effect-9');
    },
    status: async () => status('succeeded', 'effect-9', null),
  });
  second.calls.length = 0;
  await configureLauncherProviderConnection(request, scope, second.ports);
  assert.equal(prepareCalls.length, 1, 'the key is never resent');
  assert.equal(storage.map.size, 0);
});

test('a lookup that returns no effect id fails without resending', async () => {
  const storage = memoryStorage();
  const first = scriptedPorts(storage, {
    prepare: async () => {
      throw new Error('synthetic transport failure');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, first.ports),
  );

  const second = scriptedPorts(storage, {
    lookup: async () => ({effect_id: '', approval_request_id: null, state: ''}),
    prepare: async () => {
      throw new Error('prepare must never be replayed');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, second.ports),
    /receipt could not be confirmed/,
  );
  assert.equal(storage.map.size, 1, 'pending marker is preserved for inspection');
});

test('changed input cannot displace an unresolved pending attempt', async () => {
  const storage = memoryStorage();
  const first = scriptedPorts(storage, {
    prepare: async () => {
      throw new Error('synthetic transport failure');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, first.ports),
  );

  const changed: LauncherProviderConfigureRequest = {...request, key_value: 'sk-other'};
  const second = scriptedPorts(storage, {
    lookup: async () => status('dispatched', 'effect-7'),
    status: async () => status('dispatched', 'effect-7', null),
    prepare: async () => {
      throw new Error('prepare must never be replayed');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(changed, scope, second.ports),
    /still unconfirmed|identical input/,
  );
});

test('changed input after a confirmed previous outcome clears the marker but saves nothing', async () => {
  const storage = memoryStorage();
  const first = scriptedPorts(storage, {
    prepare: async () => {
      throw new Error('synthetic transport failure');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, first.ports),
  );

  const changed: LauncherProviderConfigureRequest = {...request, key_value: 'sk-other'};
  const second = scriptedPorts(storage, {
    lookup: async () => status('x', 'effect-7'),
    status: async () => status('succeeded', 'effect-7', null),
    prepare: async () => {
      throw new Error('changed input must not be sent');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(changed, scope, second.ports),
    /was not saved/,
  );
  assert.equal(storage.map.size, 0, 'resolved previous attempt is cleared');
});

test('denied approval cancels the effect and clears the marker', async () => {
  const storage = memoryStorage();
  const {ports, calls} = scriptedPorts(storage, {
    approval: async (id) => ({request_id: id, state: 'denied'}),
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, ports),
    /not approved/,
  );
  assert.ok(calls.some((call) => call.phase === 'cancel'));
  assert.equal(storage.map.size, 0);
});

test('expired approval follows the same cancel path', async () => {
  const storage = memoryStorage();
  const {ports, calls} = scriptedPorts(storage, {
    approval: async (id) => ({request_id: id, state: 'expired'}),
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, ports),
    /not approved/,
  );
  assert.ok(calls.some((call) => call.phase === 'cancel'));
});

test('a terminal effect failure keeps the marker and reports the state', async () => {
  const storage = memoryStorage();
  const {ports} = scriptedPorts(storage, {
    prepare: async () => status('failed', 'effect-2', null),
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, ports),
    /did not complete \(failed\).*effect-2/,
  );
  assert.equal(storage.map.size, 1, 'failed effects keep the marker for inspection');
});

test('concurrent runs for the same Profile scope are rejected', async () => {
  const storage = memoryStorage();
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const {ports, statusQueue} = scriptedPorts(storage, {
    pause: () => gate,
  });
  statusQueue.push(
    status('dispatched', 'effect-1', null),
    status('succeeded', 'effect-1', null),
  );
  const first = configureLauncherProviderConnection(request, scope, ports);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, scriptedPorts(storage).ports),
    /already in progress/,
  );
  release();
  await first;
});

test('a pending marker bound to one activation is invisible to another', async () => {
  const storage = memoryStorage();
  const first = scriptedPorts(storage, {
    prepare: async () => {
      throw new Error('synthetic transport failure');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, first.ports),
  );
  assert.ok(readLauncherProviderPending(storage, scope));
  assert.equal(
    readLauncherProviderPending(storage, otherScope),
    null,
    'pending state is bound to the exact Profile activation',
  );

  const {ports, calls} = scriptedPorts(storage);
  await configureLauncherProviderConnection(request, otherScope, ports);
  assert.ok(
    calls.some((call) => call.phase === 'prepare'),
    'a different activation starts its own attempt',
  );
  assert.ok(
    readLauncherProviderPending(storage, scope),
    'the original pending marker is untouched by the other activation',
  );
});

test('capability grants are bound into the pending digest', async () => {
  const storage = memoryStorage();
  const first = scriptedPorts(storage, {
    prepare: async () => {
      throw new Error('synthetic transport failure');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, first.ports),
  );

  const withAudio: LauncherProviderConfigureRequest = {
    ...request,
    capabilities: ['ai.generate', 'ai.stream', 'ai.audio.transcribe'],
  };
  const second = scriptedPorts(storage, {
    lookup: async () => status('dispatched', 'effect-4'),
    status: async () => status('dispatched', 'effect-4', null),
    prepare: async () => {
      throw new Error('a changed capability grant must not reuse the pending attempt');
    },
  });
  await assert.rejects(
    configureLauncherProviderConnection(withAudio, scope, second.ports),
    /still unconfirmed|identical input/,
  );
});

test('a malformed stored marker fails closed without sending anything', async () => {
  const storage = memoryStorage();
  const probe = scriptedPorts(storage, {
    prepare: async () => status('succeeded', 'effect-1', null),
  });
  await configureLauncherProviderConnection(request, scope, probe.ports);
  const key = [...storage.map.keys()][0];
  assert.ok(key === undefined, 'nothing stored after success');
  storage.map.set(
    `tobkiri-launcher-provider-configuration-pending-v1:${scope.profile_id}:${scope.activation_id}`,
    'not json',
  );
  const second = scriptedPorts(storage);
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, second.ports),
    /unreadable/,
  );
  assert.equal(
    second.calls.length,
    0,
    'no network call is made for an unreadable marker',
  );
});

test('a pre-aborted signal stops before any storage or network touch', async () => {
  const storage = memoryStorage();
  const {ports, calls} = scriptedPorts(storage);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    configureLauncherProviderConnection(request, scope, ports, {
      signal: controller.signal,
    }),
    /interrupted/,
  );
  assert.equal(calls.length, 0);
  assert.equal(storage.map.size, 0);
});
