import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ADVANCED_VIEW_ORDER,
  LAUNCHER_ADVANCED_VIEWS,
  authoritativeOperationKey,
  advancedDescriptorMetadataParity,
  selectAdvancedContractInvokableOperations,
} from './advancedSurfaces';
import {
  RUNTIME_CONTRACT_INVOKE_ACTION,
  RUNTIME_SURFACE_API_VERSION,
  type RuntimeOperationDescriptor,
  type RuntimeSurfaceEnvelope,
} from './runtimeSurface';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function operation(id: string, overrides: Partial<RuntimeOperationDescriptor> = {}): RuntimeOperationDescriptor {
  return {
    action: RUNTIME_CONTRACT_INVOKE_ACTION,
    operation_id: id,
    contract_id: `${id}.contract`,
    owner_pack_id: 'provider-pack',
    contribution_id: `${id}.catalog`,
    target_provider_id: `${id}.provider`,
    artifact_digest: digest('a'),
    invocation_contribution_id: `${id}.invoke`,
    invocation_owner_pack_id: 'provider-pack',
    invocation_catalog_hash: digest('c'),
    invocation_reason: null,
    invokable: true,
    catalog_digest: digest('c'),
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
      provider_pack_id: 'provider-pack',
    },
    ...overrides,
  };
}

function envelope(
  operations: RuntimeOperationDescriptor[],
  authoritativeKeys: string[],
): RuntimeSurfaceEnvelope<unknown> {
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: 'operations',
    state: 'ready',
    profile_id: 'profile-a',
    profile_revision: digest('a'),
    catalog_revision: digest('c'),
    plan_digest: digest('b'),
    records: {
      profile_lock: {digest: digest('d'), source_ref: 'profile-lock-v4://profile-a/lock'},
      resolved_plan: {digest: digest('e'), source_ref: 'resolved-plan-v1://profile-a/plan'},
      activation_record: {digest: digest('f'), source_ref: 'activation-record-v1://profile-a/activation'},
      authority_snapshot: {digest: digest('1'), source_ref: 'authority-snapshot-v4://profile-a/snapshot'},
    },
    data: {
      operations,
      packs: [{
        pack_id: 'provider-pack',
        role: 'provider',
        kind: 'normal',
        version: '1.0.0',
        display_name: 'Provider Pack',
        artifact_digest: digest('a'),
        artifact_ref: 'pack-v4://provider-pack@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        installed: true,
        enabled: true,
        approved: true,
        required: false,
        invokable_operations: authoritativeKeys,
      }],
    },
  };
}

test('every Advanced descriptor has capability/action metadata parity', () => {
  assert.equal(ADVANCED_VIEW_ORDER.length, 9);
  for (const id of ADVANCED_VIEW_ORDER) {
    const descriptor = LAUNCHER_ADVANCED_VIEWS[id];
    assert.equal(advancedDescriptorMetadataParity(descriptor), true, id);
  }
  assert.equal(LAUNCHER_ADVANCED_VIEWS.flow.actions, 'contract_invoke');
  assert.equal(LAUNCHER_ADVANCED_VIEWS.aiInput.actions, 'contract_invoke');
  assert.equal(LAUNCHER_ADVANCED_VIEWS.profileWiring.actions, 'read_only');
  assert.equal(LAUNCHER_ADVANCED_VIEWS.graph.actions, 'read_only');
  assert.equal(LAUNCHER_ADVANCED_VIEWS.apiMap.actions, 'read_only');
});

test('read-only descriptors cannot expose an invoke operation even with authoritative evidence', () => {
  for (const id of ADVANCED_VIEW_ORDER) {
    const descriptor = LAUNCHER_ADVANCED_VIEWS[id];
    if (descriptor.actions !== 'read_only') continue;
    const selected = selectAdvancedContractInvokableOperations(
      descriptor,
      {status: 'ready', stale: false, error: null},
      envelope(
        [operation(`${id}-operation`)],
        [authoritativeOperationKey(`${id}-operation.contract`, `${id}-operation`)],
      ),
      [operation(`${id}-operation`)],
    );
    assert.deepEqual(selected, [], id);
  }
});

test('contract_invoke descriptor requires the authoritative invokable operation and exact binding', () => {
  const selectedOperation = operation('selected');
  const inventoryOnly = operation('inventory-only');
  const notInvokable = operation('revoked', {invokable: false, invocation_contribution_id: null, invocation_owner_pack_id: null, invocation_catalog_hash: null});
  const selected = selectAdvancedContractInvokableOperations(
    LAUNCHER_ADVANCED_VIEWS.aiInput,
    {status: 'ready', stale: false, error: null},
    envelope(
      [selectedOperation, inventoryOnly, notInvokable],
      [authoritativeOperationKey('selected.contract', 'selected')],
    ),
    [selectedOperation, inventoryOnly, notInvokable],
  );
  assert.deepEqual(selected.map((item) => item.operation_id), ['selected']);
});

test('contract invocation selection fails closed on revoked, stale, and digest-mismatched state', () => {
  const selectedOperation = operation('selected');
  const validEnvelope = envelope(
    [selectedOperation],
    [authoritativeOperationKey('selected.contract', 'selected')],
  );
  const descriptor = LAUNCHER_ADVANCED_VIEWS.flow;
  const state = {status: 'ready', stale: false, error: null};
  assert.equal(selectAdvancedContractInvokableOperations(descriptor, state, validEnvelope, [selectedOperation]).length, 1);
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(descriptor, {...state, stale: true}, validEnvelope, [selectedOperation]),
    [],
  );
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(descriptor, {...state, status: 'timeout'}, validEnvelope, [selectedOperation]),
    [],
  );
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(
      descriptor,
      state,
      validEnvelope,
      [operation('selected', {catalog_digest: digest('d'), invocation_catalog_hash: digest('d')})],
    ),
    [],
  );
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(
      descriptor,
      state,
      validEnvelope,
      [operation('selected', {artifact_digest: digest('d')})],
    ),
    [],
  );
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(
      descriptor,
      state,
      validEnvelope,
      [operation('selected', {
        owner_pack_id: 'different-pack',
        invocation_owner_pack_id: 'different-pack',
        route: {
          contract_id: 'selected.contract',
          operation_id: 'selected',
          function_id: 'selected.function',
          provider_pack_id: 'different-pack',
        },
      })],
    ),
    [],
  );
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(
      descriptor,
      {...state, error: {code: 'APPROVAL_DENIED'}},
      validEnvelope,
      [selectedOperation],
    ),
    [],
  );
  const revokedEnvelope = structuredClone(validEnvelope);
  (revokedEnvelope.data as {packs: Array<{approved: boolean}>}).packs[0].approved = false;
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(descriptor, state, revokedEnvelope, [selectedOperation]),
    [],
  );
  const disabledEnvelope = structuredClone(validEnvelope);
  (disabledEnvelope.data as {packs: Array<{enabled: boolean}>}).packs[0].enabled = false;
  assert.deepEqual(
    selectAdvancedContractInvokableOperations(descriptor, state, disabledEnvelope, [selectedOperation]),
    [],
  );
});
