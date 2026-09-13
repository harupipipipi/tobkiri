import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {
  flowOperationSelectionKey,
  operationMatchesFlowEdge,
  readyFlowCompositions,
  WorkflowAuthoringPanel,
} from './Flow';
import type {RuntimeFlowDescriptor, RuntimeFlowEdgeDescriptor} from '@/src/lib/runtimeSurface';
import type {ApiDynamicFrontendCatalog} from '@/src/lib/apiTypes';
import {
  createWorkflowDefinition,
  hasUnknownWorkflowMutation,
  type WorkflowAuthoringDependencies,
} from '@/src/lib/workflowAuthoring';
import {MutationResultUnknownError} from '@/src/lib/mutationJournal';
import {OPERATION_STATUS_API_VERSION} from '@/src/lib/operationStatus';

const edge = (
  callerFunctionId: string,
  targetProviderId: string,
  contractId: string,
  operationId: string,
): RuntimeFlowEdgeDescriptor => ({
  caller_function_id: callerFunctionId,
  target_provider_id: targetProviderId,
  contract_id: contractId,
  operation_id: operationId,
});

const flow = (
  flowId: string,
  edges: RuntimeFlowEdgeDescriptor[],
): RuntimeFlowDescriptor => ({
  flow_id: flowId,
  label: 'Declared flow',
  state: 'ready',
  operation_ids: edges.map((item) => item.operation_id),
  edges,
});

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="root"></div></body></html>',
    {url: 'https://launcher.test/'},
  );
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

function workflowCatalog(): ApiDynamicFrontendCatalog {
  const operations = [
    'definition.list',
    'definition.create',
    'operation.palette',
  ];
  return {
    version: 'rumi.ui.contribution.v1',
    profile_id: 'defaults',
    profile_revision: digest('1'),
    activation_id: 'activation:defaults-test',
    plan_hash: digest('2'),
    catalog_hash: digest('3'),
    diagnostics: [],
    quarantined_pack_ids: [],
    contributions: operations.map((operationId) => ({
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
      kind: 'action' as const,
      mode: 'declarative' as const,
      resolved_profile_id: 'defaults',
      resolved_profile_revision: digest('1'),
      resolved_activation_id: 'activation:defaults-test',
      resolved_plan_hash: digest('2'),
    })),
  };
}

function workflowDefinition(definitionId: string): Record<string, unknown> {
  return {
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
  };
}

function succeededStatus(requestId: string): Record<string, unknown> {
  const result = {ok: true};
  return {
    runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4',
    operation_status_api_version: OPERATION_STATUS_API_VERSION,
    request_id: requestId,
    operation_id: 'definition.create',
    contract_id: 'tobkiri.workflow.v4',
    request_digest: digest('a'),
    state: 'succeeded',
    result,
    result_digest: `sha256:${createHash('sha256').update(JSON.stringify(result)).digest('hex')}`,
    record_refs: [],
    safe_error_code: null,
    created_at: 1,
    updated_at: 2,
  };
}
test('Flow admits only ready canonical compositions', () => {
  const ready = flow('flow-a', [
    edge('flow-a', 'provider-a', 'contract-a', 'declared'),
  ]);
  const unavailable: RuntimeFlowDescriptor = {
    ...flow('flow-b', [
      edge('flow-b', 'provider-b', 'contract-b', 'unavailable'),
    ]),
    state: 'unavailable',
  };

  assert.deepEqual(
    readyFlowCompositions([ready, unavailable]).map((item) => item.flow_id),
    ['flow-a'],
  );
});

test('Flow keeps missing and empty compositions read-only instead of wildcarding operations', () => {
  assert.deepEqual(readyFlowCompositions([]), []);
  assert.deepEqual(readyFlowCompositions(null), []);
});

test('Flow selection keeps a shared operation id bound to its full caller/provider edge', () => {
  const selected = edge('caller-a', 'provider-a', 'contract-a', 'shared-operation');
  const sameOperationIdElsewhere = edge(
    'caller-b',
    'provider-b',
    'contract-a',
    'shared-operation',
  );

  assert.equal(operationMatchesFlowEdge(selected, selected), true);
  assert.equal(operationMatchesFlowEdge(sameOperationIdElsewhere, selected), false);
  assert.notEqual(
    flowOperationSelectionKey(selected),
    flowOperationSelectionKey(sameOperationIdElsewhere),
  );
});

test('Workflow authoring Reload resolves a malformed successful write by its original request ID', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  const definitionId = `recovery-ui-${Date.now()}-${Math.random()}`;
  const statusRequestIds: string[] = [];
  let writes = 0;
  const dependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => workflowCatalog(),
    invoke: async (request) => {
      if (request.contributionId.endsWith('.definition.list')) return {definitions: []};
      if (request.contributionId.endsWith('.operation.palette')) {
        return {catalog_digest: digest('7'), security_epoch: 1, operations: []};
      }
      if (request.contributionId.endsWith('.definition.create')) {
        writes += 1;
        return {...workflowDefinition(definitionId), unexpected: true};
      }
      throw new Error(`Unexpected Workflow operation: ${request.contributionId}`);
    },
    fetchStatus: async (requestId) => {
      statusRequestIds.push(requestId);
      return succeededStatus(requestId);
    },
  };
  try {
    await assert.rejects(
      () => createWorkflowDefinition(
        definitionId,
        workflowDefinition(definitionId).document as Record<string, unknown>,
        dependencies,
      ),
      MutationResultUnknownError,
    );
    await act(async () => {
      root.render(<WorkflowAuthoringPanel dependencies={dependencies} />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    assert.equal(writes, 1);
    assert.equal(statusRequestIds.length, 1);
    assert.equal(hasUnknownWorkflowMutation(), false);
    assert.match(container.textContent ?? '', /Workflow definitions \(v4\)/);
    assert.doesNotMatch(container.textContent ?? '', /still unresolved|result is unknown/i);
    const create = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent?.includes('Create draft')
    ));
    assert.ok(create);
    assert.equal(create.disabled, false);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
