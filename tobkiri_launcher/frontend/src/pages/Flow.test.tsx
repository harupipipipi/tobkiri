import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {
  exactWorkflowStepFromPaletteOperation,
  Flow,
  flowOperationSelectionKey,
  operationMatchesFlowEdge,
  readyFlowCompositions,
  WorkflowAuthoringPanel,
} from './Flow';
import {
  RUNTIME_SURFACE_API_VERSION,
  validateRuntimeSurfaceEnvelope,
  type RuntimeFlowDescriptor,
  type RuntimeFlowEdgeDescriptor,
  type RuntimeSurfaceEnvelope,
} from '@/src/lib/runtimeSurface';
import type {ApiDynamicFrontendCatalog} from '@/src/lib/apiTypes';
import {
  createWorkflowDefinition,
  hasUnknownWorkflowMutation,
  type WorkflowPaletteOperation,
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

function workflowCatalog(operations = [
    'definition.list',
    'definition.create',
    'operation.palette',
]): ApiDynamicFrontendCatalog {
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

const paletteOperation = (): WorkflowPaletteOperation => ({
  contract_id: 'example.echo.v1',
  contract_revision_digest: digest('b'),
  operation_id: 'echo',
  function_principal_id: 'example.echo.provider',
  provider_id: 'example.echo',
  input_schema_digest: digest('c'),
  effect_ceiling: ['capability:echo'],
});

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

function operationsEnvelope(): RuntimeSurfaceEnvelope<unknown> {
  const artifactDigest = digest('8');
  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    surface: 'operations',
    state: 'ready',
    profile_id: 'defaults',
    profile_revision: digest('1'),
    plan_digest: digest('2'),
    catalog_revision: digest('3'),
    records: {
      profile_lock: {digest: digest('4'), source_ref: 'profile-lock-v4://defaults/lock'},
      resolved_plan: {digest: digest('2'), source_ref: 'resolved-plan-v1://defaults/plan'},
      activation_record: {digest: digest('5'), source_ref: 'activation-record-v1://defaults/active'},
      authority_snapshot: {digest: digest('6'), source_ref: 'authority-snapshot-v4://defaults/current'},
    },
    data: {
      packs: [{
        pack_id: 'published-flow-pack',
        role: 'provider',
        kind: 'normal',
        version: '1.0.0',
        display_name: 'Published Flow Pack',
        artifact_digest: artifactDigest,
        artifact_ref: `pack-v4://published-flow-pack@${artifactDigest}`,
        installed: true,
        enabled: true,
        approved: true,
        required: false,
        invokable_operations: ['example.echo.v1::echo'],
      }],
      operations: [{
        action: 'contract_invoke',
        operation_id: 'echo',
        contract_id: 'example.echo.v1',
        owner_pack_id: 'published-flow-pack',
        contribution_id: 'pack.published-flow-pack.echo',
        target_provider_id: 'example.echo.provider',
        artifact_digest: artifactDigest,
        invocation_contribution_id: 'pack.published-flow-pack.echo.invoke',
        invocation_owner_pack_id: 'published-flow-pack',
        invocation_catalog_hash: digest('3'),
        invocation_reason: null,
        invokable: true,
        catalog_digest: digest('3'),
        activation_id: 'activation:published-flow',
        function_id: 'example.echo.function',
        function_principal_id: 'example.echo.provider',
        caller_function_id: 'published.workflow',
        authority_reference: 'authority-ref:published-flow',
        schema: {input_schema: {type: 'object', properties: {}}},
        label: 'Echo published workflow',
        route: {
          contract_id: 'example.echo.v1',
          operation_id: 'echo',
          function_id: 'example.echo.function',
          provider_pack_id: 'published-flow-pack',
        },
      }],
      flows: [{
        flow_id: 'published.workflow',
        label: 'Published workflow',
        state: 'ready',
        operation_ids: ['echo'],
        edges: [{
          caller_function_id: 'published.workflow',
          target_provider_id: 'example.echo.provider',
          contract_id: 'example.echo.v1',
          operation_id: 'echo',
        }],
      }],
    },
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

test('Workflow authoring inserts an exact palette-bound step without schema bypasses', () => {
  const operation = paletteOperation();
  const first = exactWorkflowStepFromPaletteOperation(operation, []);
  const second = exactWorkflowStepFromPaletteOperation(operation, [first]);

  assert.deepEqual(first, {
    id: 'step-1',
    request: {
      contract_id: operation.contract_id,
      contract_revision_digest: operation.contract_revision_digest,
      operation_id: operation.operation_id,
      function_principal_id: operation.function_principal_id,
      input: {},
    },
  });
  assert.equal(second.id, 'step-2');
  assert.equal('provider_id' in (first.request as Record<string, unknown>), false);
  assert.equal('input_schema_digest' in (first.request as Record<string, unknown>), false);
});

test('Workflow authoring palette is usable from a fresh draft and selection reloads the exact Definition', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  const operation = paletteOperation();
  const authoritativeDocument = {
    workflow_api_version: 'io.tobkiri.workflow.v4',
    name: 'Authoritative definition',
    steps: [exactWorkflowStepFromPaletteOperation(operation, [])],
  };
  let validationDocument: Record<string, unknown> | null = null;
  let getCalls = 0;
  let releaseGet: (() => void) | null = null;
  const getGate = new Promise<void>((resolve) => {
    releaseGet = resolve;
  });
  const dependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => workflowCatalog([
      'definition.list',
      'definition.get',
      'definition.validate',
      'operation.palette',
    ]),
    invoke: async (request) => {
      if (request.contributionId.endsWith('.definition.list')) {
        return {definitions: [workflowDefinition('saved-definition')]};
      }
      if (request.contributionId.endsWith('.operation.palette')) {
        return {
          catalog_digest: digest('7'),
          security_epoch: 1,
          operations: [operation],
        };
      }
      if (request.contributionId.endsWith('.definition.get')) {
        getCalls += 1;
        await getGate;
        return {
          ...workflowDefinition('saved-definition'),
          revision: 2,
          etag: '"authoritative-etag"',
          document: authoritativeDocument,
        };
      }
      if (request.contributionId.endsWith('.definition.validate')) {
        validationDocument = request.payload.document as Record<string, unknown>;
        return {valid: true, errors: []};
      }
      throw new Error(`Unexpected Workflow operation: ${request.contributionId}`);
    },
  };
  try {
    await act(async () => {
      root.render(<WorkflowAuthoringPanel dependencies={dependencies} />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });

    assert.match(container.textContent ?? '', new RegExp(operation.provider_id));
    assert.match(container.textContent ?? '', new RegExp(operation.contract_revision_digest));
    assert.match(container.textContent ?? '', new RegExp(operation.input_schema_digest));
    const insert = container.querySelector<HTMLButtonElement>(
      `[aria-label="Insert exact Workflow step for ${operation.operation_id}"]`,
    );
    assert.ok(insert);
    await act(async () => {
      insert.click();
    });
    const editor = container.querySelector<HTMLTextAreaElement>('#workflow-definition-json');
    assert.ok(editor);
    const inserted = JSON.parse(editor.value) as Record<string, unknown>;
    assert.deepEqual(inserted.steps, [exactWorkflowStepFromPaletteOperation(operation, [])]);

    const validate = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent === 'Validate'
    ));
    assert.ok(validate);
    await act(async () => {
      validate.click();
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    assert.deepEqual(validationDocument, inserted);
    assert.match(container.textContent ?? '', /valid against the active Workflow v4 palette/i);

    const saved = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent?.includes('saved-definition')
    ));
    assert.ok(saved);
    await act(async () => {
      saved.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    assert.equal(getCalls, 1);
    const create = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent === 'Create draft'
    ));
    assert.ok(create);
    assert.equal(create.disabled, true);
    assert.equal(saved.disabled, true);
    assert.ok(releaseGet);
    await act(async () => {
      releaseGet();
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    assert.equal(create.disabled, false);
    assert.deepEqual(JSON.parse(editor.value), authoritativeDocument);
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

test('Workflow visual editor inserts an exact palette operation and retains an explicit JSON source mode', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  const operation = paletteOperation();
  const dependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => workflowCatalog(),
    invoke: async (request) => {
      if (request.contributionId.endsWith('.definition.list')) return {definitions: []};
      if (request.contributionId.endsWith('.operation.palette')) {
        return {
          catalog_digest: digest('7'),
          security_epoch: 1,
          operations: [operation],
        };
      }
      throw new Error(`Unexpected Workflow operation: ${request.contributionId}`);
    },
  };
  try {
    await act(async () => {
      root.render(<WorkflowAuthoringPanel dependencies={dependencies} />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    const visual = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent?.includes('Visual steps')
    ));
    assert.ok(visual);
    await act(async () => visual.click());
    const add = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent?.includes('Add step')
    ));
    assert.ok(add);
    await act(async () => add.click());
    assert.match(container.textContent ?? '', /example\.echo\.v1 \/ echo/);

    const source = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent?.includes('Source JSON')
    ));
    assert.ok(source);
    await act(async () => source.click());
    assert.match(container.textContent ?? '', /YAML is intentionally unsupported/);
    const textarea = container.querySelector<HTMLTextAreaElement>('#workflow-definition-json');
    assert.ok(textarea);
    const sourceDocument = JSON.parse(textarea.value) as {
      steps: Array<{request: Record<string, unknown>}>
    };
    assert.deepEqual(sourceDocument.steps[0]?.request, {
      contract_id: operation.contract_id,
      contract_revision_digest: operation.contract_revision_digest,
      operation_id: operation.operation_id,
      function_principal_id: operation.function_principal_id,
      input: {},
    });
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

test('a published Profile-declared flow renders its exact backend operation as invokable', async () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  const envelope = operationsEnvelope();
  const authoringDependencies: WorkflowAuthoringDependencies = {
    fetchCatalog: async () => workflowCatalog(),
    invoke: async (request) => {
      if (request.contributionId.endsWith('.definition.list')) return {definitions: []};
      if (request.contributionId.endsWith('.operation.palette')) {
        return {catalog_digest: digest('7'), security_epoch: 1, operations: []};
      }
      throw new Error(`Unexpected authoring operation: ${request.contributionId}`);
    },
  };
  try {
    assert.equal(validateRuntimeSurfaceEnvelope('operations', envelope).surface, 'operations');
    await act(async () => {
      root.render(
        <Flow
          operationsClient={{read: async <T,>() => envelope as RuntimeSurfaceEnvelope<T>}}
          authoringDependencies={authoringDependencies}
        />,
      );
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    const profileTab = Array.from(container.querySelectorAll('button')).find((button) => (
      button.textContent?.includes('Profile-declared operations')
    ));
    assert.ok(profileTab);
    await act(async () => {
      profileTab.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    assert.match(container.textContent ?? '', /Published workflow/);
    assert.match(container.textContent ?? '', /Echo published workflow/);
    const invoke = container.querySelector<HTMLButtonElement>(
      '[aria-label="Invoke declared contract operation"]',
    );
    assert.ok(invoke);
    assert.equal(invoke.disabled, false);
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
