import assert from 'node:assert/strict';
import test from 'node:test';

import {exactFlowInvokableOperations} from './Flow';
import {authoritativeOperationKey} from '@/src/lib/advancedSurfaces';
import type {RuntimeFlowDescriptor, RuntimeOperationDescriptor} from '@/src/lib/runtimeSurface';

function operation(id: string, invokable = true): RuntimeOperationDescriptor {
  return {
    action: 'contract_invoke',
    operation_id: id,
    contract_id: `${id}.contract`,
    owner_pack_id: 'pack-a',
    contribution_id: `${id}.contribution`,
    target_provider_id: 'provider-a',
    artifact_digest: `sha256:${'a'.repeat(64)}`,
    invocation_contribution_id: `${id}.invoke`,
    invocation_owner_pack_id: 'pack-a',
    invocation_catalog_hash: `sha256:${'b'.repeat(64)}`,
    invocation_reason: null,
    invokable,
    catalog_digest: `sha256:${'b'.repeat(64)}`,
    activation_id: 'activation:flow-one',
    function_id: `${id}.function`,
    function_principal_id: `${id}.principal`,
    caller_function_id: `${id}.caller`,
    authority_reference: `authority://${id}`,
    schema: {input_schema: {type: 'object', properties: {}}},
    input_schema: {type: 'object', properties: {}},
    route: {
      contract_id: `${id}.contract`,
      operation_id: id,
      function_id: `${id}.function`,
      provider_pack_id: 'pack-a',
    },
  };
}

const flow = (operationIds: string[]): RuntimeFlowDescriptor => ({
  flow_id: 'flow-a',
  label: 'Declared flow',
  state: 'ready',
  operation_ids: operationIds,
});
test('Flow exposes only operations declared by a non-empty Pack composition', () => {
  const operations = [operation('declared'), operation('inventory-only'), operation('not-invokable', false)];
  const authoritative = new Set([authoritativeOperationKey('declared.contract', 'declared')]);
  assert.deepEqual(
    exactFlowInvokableOperations([flow(['declared'])], operations, authoritative).map((item) => item.operation_id),
    ['declared'],
  );
});

test('Flow keeps missing and empty compositions read-only instead of wildcarding operations', () => {
  const operations = [operation('inventory-only')];
  const authoritative = new Set([authoritativeOperationKey('inventory-only.contract', 'inventory-only')]);
  assert.deepEqual(exactFlowInvokableOperations([], operations, authoritative), []);
  assert.deepEqual(exactFlowInvokableOperations(null, operations, authoritative), []);
  assert.deepEqual(exactFlowInvokableOperations([flow(['inventory-only'])], operations), []);
});
