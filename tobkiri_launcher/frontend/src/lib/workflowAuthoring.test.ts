import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {test} from 'node:test';

import {ApiRequestTimeoutError} from './apiTransport';
import type {ApiDynamicFrontendCatalog} from './apiTypes';
import {
  completeMutation,
  listMutationJournal,
  MutationBlockedError,
  MutationResultUnknownError,
} from './mutationJournal';
import {OPERATION_STATUS_API_VERSION} from './operationStatus';
import {
  createWorkflowDefinition,
  hasUnknownWorkflowMutation,
  invokeWorkflowAuthoringOperation,
  parseWorkflowDefinitionList,
  parseWorkflowDefinitionText,
  parseWorkflowPalette,
  reconcileUnknownWorkflowMutations,
  resolveWorkflowAuthoringCapability,
  type WorkflowAuthoringDependencies,
} from './workflowAuthoring';

const digest = (character: string) => `sha256:${character.repeat(64)}`;

function workflowCatalog(): ApiDynamicFrontendCatalog {
  const operationId = 'definition.list';
  return {
    version: 'rumi.ui.contribution.v1',
    profile_id: 'defaults',
    profile_revision: digest('1'),
    activation_id: 'activation:defaults-test',
    plan_hash: digest('2'),
    catalog_hash: digest('3'),
    diagnostics: [],
    quarantined_pack_ids: [],
    contributions: [{
      contribution_id: `pack.tobkiri_workflow_pack.${operationId}`,
      owner_pack_id: 'tobkiri_workflow_pack',
      owner_pack_hash: digest('4'),
      label: operationId,
      action_contract: 'tobkiri.workflow.v4',
      operation_id: operationId,
      provider_id: 'tobkiri.workflow.provider',
      function_id: 'tobkiri.workflow.provider',
      build_identity: 'tobkiri.workflow.provider',
      descriptor_hash: digest('5'),
      kind: 'action',
      mode: 'declarative',
      resolved_profile_id: 'defaults',
      resolved_profile_revision: digest('1'),
      resolved_activation_id: 'activation:defaults-test',
      resolved_plan_hash: digest('2'),
    }],
  };
}

const definition = (definitionId = 'daily-summary') => ({
  definition_id: definitionId,
  revision: 1,
  revision_digest: digest('6'),
  etag: '"definition-etag"',
  state: 'draft',
  document: {
    workflow_api_version: 'io.tobkiri.workflow.v4',
    steps: [],
  },
  created_at: 1,
  updated_at: 1,
});

function status(
  requestId: string,
  operationId: string,
  state: 'pending' | 'succeeded' | 'failed' | 'indeterminate',
  safeErrorCode: string | null = null,
): Record<string, unknown> {
  const result = state === 'succeeded' ? {ok: true} : null;
  return {
    runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
    operation_status_api_version: OPERATION_STATUS_API_VERSION,
    request_id: requestId,
    operation_id: operationId,
    contract_id: 'tobkiri.workflow.v4',
    request_digest: digest('a'),
    state,
    result,
    result_digest: result === null
      ? null
      : `sha256:${createHash('sha256').update(JSON.stringify(result)).digest('hex')}`,
    record_refs: [],
    safe_error_code: safeErrorCode,
    created_at: 1,
    updated_at: 2,
  };
}

test('Workflow authoring resolves only its exact current dynamic action', () => {
  const catalog = workflowCatalog();
  assert.equal(
    resolveWorkflowAuthoringCapability(catalog, 'definition.list')?.contribution_id,
    'pack.tobkiri_workflow_pack.definition.list',
  );
  assert.equal(resolveWorkflowAuthoringCapability(catalog, 'definition.get'), null);
  assert.equal(
    resolveWorkflowAuthoringCapability({
      ...catalog,
      quarantined_pack_ids: ['tobkiri_workflow_pack'],
    }, 'definition.list'),
    null,
  );
});

test('Workflow authoring invokes through the generic capability envelope with no fallback', async () => {
  const catalog = workflowCatalog();
  let received: unknown = null;
  const dependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => catalog,
    invoke: async (request) => {
      received = request;
      return {definitions: []};
    },
  };

  await assert.doesNotReject(() => invokeWorkflowAuthoringOperation(
    'definition.list', {}, dependencies,
  ));
  assert.deepEqual(received, {
    profileId: 'defaults',
    profileRevision: digest('1'),
    activationId: 'activation:defaults-test',
    planHash: digest('2'),
    catalogHash: digest('3'),
    contributionId: 'pack.tobkiri_workflow_pack.definition.list',
    ownerPackId: 'tobkiri_workflow_pack',
    contractId: 'tobkiri.workflow.v4',
    payload: {},
  });
  await assert.rejects(
    () => invokeWorkflowAuthoringOperation('definition.list', {unexpected: true}, dependencies),
    /payload is not exact/,
  );
});

test('Workflow parsers reject malformed definition, palette, and text identities', () => {
  assert.equal(parseWorkflowDefinitionText('{not JSON'), null);
  assert.equal(parseWorkflowDefinitionText(JSON.stringify({steps: []})), null);
  assert.deepEqual(parseWorkflowDefinitionText(JSON.stringify(definition().document)), definition().document);
  assert.equal(parseWorkflowDefinitionList({definitions: [definition(), definition()]}), null);
  assert.equal(parseWorkflowDefinitionList({definitions: [
    {...definition(), unexpected: true},
  ]}), null);
  assert.equal(parseWorkflowPalette({
    catalog_digest: digest('7'),
    security_epoch: 1,
    operations: [{
      contract_id: 'contract.a',
      contract_revision_digest: digest('8'),
      operation_id: 'read',
      function_principal_id: 'caller.a',
      provider_id: 'provider.a',
      input_schema_digest: digest('9'),
      effect_ceiling: ['read'],
    }, {
      contract_id: 'contract.a',
      contract_revision_digest: digest('8'),
      operation_id: 'read',
      function_principal_id: 'caller.a',
      provider_id: 'provider.a',
      input_schema_digest: digest('9'),
      effect_ceiling: ['read'],
    }],
  }), null);
});

test('a timed-out Workflow write becomes unknown and is never replayed', async () => {
  const operationId = 'definition.create';
  const catalog = workflowCatalog();
  catalog.contributions[0] = {
    ...catalog.contributions[0],
    contribution_id: `pack.tobkiri_workflow_pack.${operationId}`,
    label: operationId,
    operation_id: operationId,
  };
  let calls = 0;
  const dependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => catalog,
    invoke: async () => {
      calls += 1;
      throw new ApiRequestTimeoutError('POST', '/api/ui/capability/invoke', 1);
    },
  };
  const definitionId = `unknown-write-${Date.now()}-${Math.random()}`;
  const document = definition(definitionId).document;

  await assert.rejects(
    () => createWorkflowDefinition(definitionId, document, dependencies),
    (error: unknown) => error instanceof MutationResultUnknownError,
  );
  await assert.rejects(
    () => createWorkflowDefinition(definitionId, document, dependencies),
    (error: unknown) => error instanceof MutationBlockedError,
  );
  assert.equal(calls, 1);
  const durable = listMutationJournal().find((record) => record.key.includes(definitionId));
  assert.ok(durable);
  completeMutation(durable.key, durable.requestId);
});

test('a malformed 2xx Workflow write is durable and reload reconciles its original request only', async () => {
  const operationId = 'definition.create';
  const catalog = workflowCatalog();
  catalog.contributions[0] = {
    ...catalog.contributions[0],
    contribution_id: `pack.tobkiri_workflow_pack.${operationId}`,
    label: operationId,
    operation_id: operationId,
  };
  const definitionId = `malformed-write-${Date.now()}-${Math.random()}`;
  let writes = 0;
  let statusCalls = 0;
  let refreshes = 0;
  let nextStatus: 'pending' | 'succeeded' = 'pending';
  const submittedDocument = {
    ...definition(definitionId).document,
    prompt: 'private workflow text must not enter the mutation journal',
  };
  const dependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => catalog,
    invoke: async () => {
      writes += 1;
      return {...definition(definitionId), unexpected: true};
    },
    fetchStatus: async (requestId) => {
      statusCalls += 1;
      return status(requestId, operationId, nextStatus);
    },
  };

  await assert.rejects(
    () => createWorkflowDefinition(definitionId, submittedDocument, dependencies),
    (error: unknown) => error instanceof MutationResultUnknownError,
  );
  const record = listMutationJournal().find((item) => (
    item.metadata.kind === 'workflow-authoring'
      && item.metadata.operation_id === operationId
      && item.key.endsWith(definitionId)
  ));
  assert.ok(record);
  assert.equal(record.requestId.length, 36);
  assert.equal(record.metadata.contract_id, 'tobkiri.workflow.v4');
  assert.equal(
    typeof record.metadata.contract_map_digest === 'string'
      ? record.metadata.contract_map_digest.length
      : 0,
    71,
  );
  assert.equal(record.metadata.workflow_contribution_id, `pack.tobkiri_workflow_pack.${operationId}`);
  assert.equal(record.metadata.workflow_catalog_hash, digest('3'));
  assert.equal('workflow_payload' in record.metadata, false);
  assert.doesNotMatch(JSON.stringify(record), /private workflow text/);

  const pending = await reconcileUnknownWorkflowMutations(async () => {
    refreshes += 1;
  }, dependencies);
  assert.deepEqual(pending.map((item) => item.state), ['pending']);
  assert.equal(writes, 1);
  assert.equal(statusCalls, 1);
  assert.equal(refreshes, 0);
  assert.equal(hasUnknownWorkflowMutation(), true);

  nextStatus = 'succeeded';
  const succeeded = await reconcileUnknownWorkflowMutations(async () => {
    refreshes += 1;
  }, dependencies);
  assert.deepEqual(succeeded.map((item) => item.state), ['succeeded']);
  assert.equal(writes, 1);
  assert.equal(statusCalls, 2);
  assert.equal(refreshes, 1);
  assert.equal(hasUnknownWorkflowMutation(), false);
});

test('Workflow recovery releases authoritative failure and fails closed for a changed target', async () => {
  const operationId = 'definition.create';
  const catalog = workflowCatalog();
  catalog.contributions[0] = {
    ...catalog.contributions[0],
    contribution_id: `pack.tobkiri_workflow_pack.${operationId}`,
    label: operationId,
    operation_id: operationId,
  };
  let writes = 0;
  const unknown = async (definitionId: string): Promise<void> => {
    const dependencies: WorkflowAuthoringDependencies = {
      fetchCatalog: async () => catalog,
      invoke: async () => {
        writes += 1;
        return {...definition(definitionId), unexpected: true};
      },
    };
    await assert.rejects(
      () => createWorkflowDefinition(definitionId, definition(definitionId).document, dependencies),
      MutationResultUnknownError,
    );
  };

  const failedId = `failed-write-${Date.now()}-${Math.random()}`;
  await unknown(failedId);
  const failed = await reconcileUnknownWorkflowMutations(async () => undefined, {
    fetchCatalog: async () => catalog,
    invoke: async () => { throw new Error('No recovery write is permitted.'); },
    fetchStatus: async (requestId) => status(requestId, operationId, 'failed', 'CONFLICT'),
  });
  assert.deepEqual(failed.map((item) => item.state), ['failed']);
  assert.equal(failed[0]?.safeErrorCode, 'CONFLICT');
  assert.equal(hasUnknownWorkflowMutation(), false);

  const tamperedId = `tampered-write-${Date.now()}-${Math.random()}`;
  await unknown(tamperedId);
  const tamperedCatalog = {
    ...catalog,
    catalog_hash: digest('f'),
  };
  const blocked = await reconcileUnknownWorkflowMutations(async () => undefined, {
    fetchCatalog: async () => tamperedCatalog,
    invoke: async () => { throw new Error('No recovery write is permitted.'); },
    fetchStatus: async () => { throw new Error('A changed target must not read status.'); },
  });
  assert.deepEqual(blocked.map((item) => item.state), ['blocked']);
  assert.equal(writes, 2);
  assert.equal(hasUnknownWorkflowMutation(), true);
  const durable = listMutationJournal().find((record) => record.key.includes(tamperedId));
  assert.ok(durable);
  completeMutation(durable.key, durable.requestId);
});
