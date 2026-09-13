import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {test} from 'node:test';

import {
  OPERATION_STATUS_API_VERSION,
  reconcileMutationStatus,
  type OperationStatusBinding,
} from './operationStatus.ts';
import {
  beginMutation,
  listMutationJournal,
  markMutationUnknown,
  MutationBlockedError,
} from './mutationJournal.ts';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from './generatedFrontendContractMap.ts';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;
const requestIds = [
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  '33333333-3333-4333-8333-333333333333',
  '44444444-4444-4444-8444-444444444444',
  '55555555-5555-4555-8555-555555555555',
  '66666666-6666-4666-8666-666666666666',
  '77777777-7777-4777-8777-777777777777',
  '88888888-8888-4888-8888-888888888888',
  '99999999-9999-4999-8999-999999999999',
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
];
let requestIndex = 0;

function binding(requestId = requestIds[requestIndex++] ?? requestIds[0]): OperationStatusBinding {
  return {
    requestId,
    operationId: 'profile.change.activate',
    contractId: 'tobkiri.host.control-presentation.v4',
    mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
  };
}

function status(
  requestId: string,
  state: 'pending' | 'succeeded' | 'failed' | 'indeterminate',
  result: Record<string, unknown> | null = null,
): Record<string, unknown> {
  const resultJson = result === null ? null : JSON.stringify(result);
  const resultDigest = resultJson === null
    ? null
    : `sha256:${createHash('sha256').update(resultJson).digest('hex')}`;
  return {
    runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
    operation_status_api_version: OPERATION_STATUS_API_VERSION,
    request_id: requestId,
    operation_id: 'profile.change.activate',
    contract_id: 'tobkiri.host.control-presentation.v4',
    request_digest: digest('a'),
    state,
    result,
    result_digest: resultDigest,
    record_refs: [],
    safe_error_code: state === 'failed' ? 'UNAPPROVED' : state === 'indeterminate' ? 'PROCESS_RESTART' : null,
    created_at: 1,
    updated_at: 2,
  };
}

function unknownRecord(requestId: string) {
  const record = beginMutation(
    `status-test:${requestId}`,
    {
      kind: 'profile.ceremony',
      operation_id: 'profile.change.activate',
      contract_id: 'tobkiri.host.control-presentation.v4',
      contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    },
    {primary: requestId},
  );
  return markMutationUnknown(record.key, record.requestId);
}

test('operation status reconciles success only after an authoritative refresh', async () => {
  const requestId = requestIds[0];
  const record = unknownRecord(requestId);
  let refreshes = 0;
  const result = await reconcileMutationStatus({
    record,
    binding: binding(requestId),
    refresh: async () => { refreshes += 1; },
    verifySuccess: (value) => value.state === 'succeeded',
    fetcher: async () => status(requestId, 'succeeded', {state: 'approved'}),
  });
  assert.equal(result.state, 'succeeded');
  assert.equal(refreshes, 1);
  assert.equal(listMutationJournal().some((item) => item.key === record.key), false);
});

test('a terminal result cleans only its exact journal when the UI binding is stale', async () => {
  const requestId = requestIds[9];
  const record = unknownRecord(requestId);
  let refreshes = 0;
  const result = await reconcileMutationStatus({
    record,
    binding: binding(requestId),
    refresh: async () => { refreshes += 1; },
    verifySuccess: (value) => value.state === 'succeeded',
    isCurrent: () => false,
    fetcher: async () => status(requestId, 'succeeded', {state: 'approved'}),
  });
  assert.equal(result.state, 'stale');
  assert.equal(result.reconciled, true);
  assert.equal(refreshes, 1);
  assert.equal(listMutationJournal().some((item) => item.key === record.key), false);
});

test('pending stays blocked, then the same stable request can reconcile success', async () => {
  const requestId = requestIds[1];
  const record = unknownRecord(requestId);
  let pending = true;
  const fetcher = async () => {
    const value = pending ? status(requestId, 'pending') : status(requestId, 'succeeded', {state: 'approved'});
    pending = false;
    return value;
  };
  const first = await reconcileMutationStatus({
    record,
    binding: binding(requestId),
    refresh: async () => undefined,
    fetcher,
  });
  assert.equal(first.state, 'pending');
  assert.throws(() => beginMutation(record.key), MutationBlockedError);
  const second = await reconcileMutationStatus({
    record: listMutationJournal().find((item) => item.key === record.key)!,
    binding: binding(requestId),
    refresh: async () => undefined,
    fetcher,
  });
  assert.equal(second.state, 'succeeded');
});

test('failed and indeterminate statuses never become client success', async () => {
  const failedId = requestIds[2];
  const failed = unknownRecord(failedId);
  const failedResult = await reconcileMutationStatus({
    record: failed,
    binding: binding(failedId),
    refresh: async () => undefined,
    fetcher: async () => status(failedId, 'failed'),
  });
  assert.equal(failedResult.state, 'failed');
  assert.equal(listMutationJournal().some((item) => item.key === failed.key), false);

  const indeterminateId = requestIds[3];
  const indeterminate = unknownRecord(indeterminateId);
  const indeterminateResult = await reconcileMutationStatus({
    record: indeterminate,
    binding: binding(indeterminateId),
    refresh: async () => undefined,
    fetcher: async () => status(indeterminateId, 'indeterminate'),
  });
  assert.equal(indeterminateResult.state, 'indeterminate');
  assert.equal(listMutationJournal().some((item) => item.key === indeterminate.key), true);
});

test('unknown request, stale response, map tamper, and digest tamper fail closed', async () => {
  const unknownId = requestIds[4];
  const unknown = unknownRecord(unknownId);
  await assert.rejects(
    reconcileMutationStatus({
      record: unknown,
      binding: binding(unknownId),
      refresh: async () => undefined,
      fetcher: async () => { throw new Error('operation status is unavailable'); },
    }),
  );
  assert.equal(listMutationJournal().some((item) => item.key === unknown.key), true);

  const staleId = requestIds[5];
  const stale = unknownRecord(staleId);
  await assert.rejects(
    reconcileMutationStatus({
      record: stale,
      binding: binding(staleId),
      refresh: async () => undefined,
      fetcher: async () => ({...status(staleId, 'pending'), created_at: 5, updated_at: 4}),
    }),
    /stale|invalid/i,
  );

  const mapId = requestIds[6];
  const mapTampered = unknownRecord(mapId);
  await assert.rejects(
    reconcileMutationStatus({
      record: mapTampered,
      binding: {...binding(mapId), mapArtifactDigest: digest('f')},
      refresh: async () => undefined,
      fetcher: async () => status(mapId, 'pending'),
    }),
  );

  const digestId = requestIds[7];
  const digestTampered = unknownRecord(digestId);
  await assert.rejects(
    reconcileMutationStatus({
      record: digestTampered,
      binding: {...binding(digestId), requestDigest: digest('a')},
      refresh: async () => undefined,
      fetcher: async () => ({...status(digestId, 'pending'), request_digest: digest('b')}),
    }),
  );
});

test('double status reconciliation is keyed and makes one status request', async () => {
  const requestId = requestIds[8];
  const record = unknownRecord(requestId);
  let calls = 0;
  let release: (() => void) | undefined;
  const response = new Promise<Record<string, unknown>>((resolve) => {
    release = () => resolve(status(requestId, 'pending'));
  });
  const fetcher = async () => {
    calls += 1;
    return response;
  };
  const first = reconcileMutationStatus({
    record,
    binding: binding(requestId),
    refresh: async () => undefined,
    fetcher,
  });
  const second = reconcileMutationStatus({
    record,
    binding: binding(requestId),
    refresh: async () => undefined,
    fetcher,
  });
  release?.();
  const results = await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.deepEqual(results.map((item) => item.state), ['pending', 'pending']);
});

test('cancelled status reconciliation never refreshes or releases its durable journal', async () => {
  const requestId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  const record = unknownRecord(requestId);
  const controller = new AbortController();
  let release: (() => void) | undefined;
  let refreshes = 0;
  const response = new Promise<Record<string, unknown>>((resolve) => {
    release = () => resolve(status(requestId, 'indeterminate'));
  });
  const pending = reconcileMutationStatus({
    record,
    binding: binding(requestId),
    refresh: async () => { refreshes += 1; },
    signal: controller.signal,
    fetcher: async (_statusRequestId, signal) => {
      assert.equal(signal, controller.signal);
      return response;
    },
  });

  controller.abort();
  await assert.rejects(pending, /cancelled/);
  release?.();
  assert.equal(refreshes, 0);
  assert.equal(listMutationJournal().some((item) => item.key === record.key), true);
});
