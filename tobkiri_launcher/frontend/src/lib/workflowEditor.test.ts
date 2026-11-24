import assert from 'node:assert/strict';
import test from 'node:test';

import {
  dependencyInputValue,
  insertPaletteWorkflowStep,
  moveWorkflowStep,
  parseDependencyInput,
  parseVisualWorkflowDocument,
  removeWorkflowStep,
  setWorkflowStepDependencies,
  setWorkflowStepId,
  visualWorkflowSteps,
} from './workflowEditor';
import type {WorkflowPaletteOperation} from './workflowAuthoring';

const digest = (character: string): string => `sha256:${character.repeat(64)}`;

const paletteOperation: WorkflowPaletteOperation = {
  contract_id: 'example.echo.v1',
  contract_revision_digest: digest('a'),
  operation_id: 'echo',
  function_principal_id: 'example.echo.provider',
  provider_id: 'example.echo.provider',
  input_schema_digest: digest('b'),
  effect_ceiling: ['read'],
};

function document(): Record<string, unknown> {
  return {
    workflow_api_version: 'io.tobkiri.workflow.v4',
    steps: [{
      id: 'first',
      request: {
        contract_id: 'example.echo.v1',
        contract_revision_digest: digest('a'),
        operation_id: 'echo',
        function_principal_id: 'example.echo.provider',
        input: {},
      },
      retry: {max_attempts: 1, backoff_ms: 0},
      depends_on: [],
    }],
  };
}

test('visual Workflow editor is canonical JSON only; it does not accept YAML', () => {
  assert.equal(parseVisualWorkflowDocument('workflow_api_version: io.tobkiri.workflow.v4\nsteps: []'), null);
  assert.deepEqual(
    parseVisualWorkflowDocument(JSON.stringify(document())),
    document(),
  );
});

test('palette insertion uses the full exact v4 operation identity', () => {
  const inserted = insertPaletteWorkflowStep(document(), paletteOperation);
  const steps = inserted.steps as Array<Record<string, unknown>>;
  assert.equal(steps.length, 2);
  assert.deepEqual(steps[1], {
    id: 'echo-1',
    request: {
      contract_id: 'example.echo.v1',
      contract_revision_digest: digest('a'),
      operation_id: 'echo',
      function_principal_id: 'example.echo.provider',
      input: {},
    },
    retry: {max_attempts: 1, backoff_ms: 0},
    depends_on: [],
  });
});

test('visual editor preserves dependency errors for the authoritative v4 validator', () => {
  const inserted = insertPaletteWorkflowStep(document(), paletteOperation);
  const withId = setWorkflowStepId(inserted, 1, 'second');
  const withDependencies = setWorkflowStepDependencies(
    withId,
    1,
    parseDependencyInput('first, missing, second'),
  );
  const steps = visualWorkflowSteps(withDependencies);
  assert.deepEqual(steps[1]?.dependsOn, ['first', 'missing', 'second']);
  assert.equal(dependencyInputValue(steps[1]?.dependsOn ?? []), 'first, missing, second');

  const moved = moveWorkflowStep(withDependencies, 1, -1);
  assert.deepEqual(visualWorkflowSteps(moved).map((step) => step.id), ['second', 'first']);
  const removed = removeWorkflowStep(moved, 0);
  assert.deepEqual(visualWorkflowSteps(removed).map((step) => step.id), ['first']);
});
