import {
  WORKFLOW_DOCUMENT_API_VERSION,
  type WorkflowPaletteOperation,
} from './workflowAuthoring';

export interface WorkflowVisualStep {
  id: string;
  dependsOn: string[];
  operationLabel: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function cloneJsonDocument(document: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(document)) as Record<string, unknown>;
}

function editableSteps(document: Record<string, unknown>): Record<string, unknown>[] {
  const steps = document.steps;
  if (!Array.isArray(steps) || !steps.every(isRecord)) {
    throw new Error('Workflow steps are not editable as canonical v4 JSON.');
  }
  return steps;
}

/**
 * Accept only the canonical JSON document used by the v4 Contract. YAML is
 * intentionally unsupported: no YAML parser is bundled or trusted here.
 */
export function parseVisualWorkflowDocument(text: string): Record<string, unknown> | null {
  try {
    const document: unknown = JSON.parse(text);
    if (
      !isRecord(document)
      || document.workflow_api_version !== WORKFLOW_DOCUMENT_API_VERSION
      || !Array.isArray(document.steps)
      || !document.steps.every(isRecord)
    ) return null;
    return document;
  } catch {
    return null;
  }
}

/** Render permissive local step details; the v4 validator owns correctness. */
export function visualWorkflowSteps(
  document: Record<string, unknown> | null,
): WorkflowVisualStep[] {
  if (!document) return [];
  try {
    return editableSteps(document).map((step, index) => {
      const request = isRecord(step.request) ? step.request : null;
      const operationId = request?.operation_id;
      const contractId = request?.contract_id;
      const dependsOn = Array.isArray(step.depends_on)
        ? step.depends_on.filter((item): item is string => typeof item === 'string')
        : [];
      return {
        id: typeof step.id === 'string' ? step.id : `step-${index + 1}`,
        dependsOn,
        operationLabel: typeof operationId === 'string' && typeof contractId === 'string'
          ? `${contractId} / ${operationId}`
          : 'Unbound request (validate before saving)',
      };
    });
  } catch {
    return [];
  }
}

function nextStepId(
  steps: readonly Record<string, unknown>[],
  operationId: string,
): string {
  const used = new Set(steps.flatMap((step) => (
    typeof step.id === 'string' ? [step.id] : []
  )));
  const base = operationId.replace(/[^a-zA-Z0-9._-]/g, '-');
  let suffix = 1;
  let candidate = `${base}-${suffix}`;
  while (used.has(candidate)) {
    suffix += 1;
    candidate = `${base}-${suffix}`;
  }
  return candidate;
}

/** Insert one exact palette identity, never a guessed provider or contract. */
export function insertPaletteWorkflowStep(
  document: Record<string, unknown>,
  operation: WorkflowPaletteOperation,
): Record<string, unknown> {
  const next = cloneJsonDocument(document);
  const steps = editableSteps(next);
  steps.push({
    id: nextStepId(steps, operation.operation_id),
    request: {
      contract_id: operation.contract_id,
      contract_revision_digest: operation.contract_revision_digest,
      operation_id: operation.operation_id,
      function_principal_id: operation.function_principal_id,
      input: {},
    },
    retry: {max_attempts: 1, backoff_ms: 0},
    depends_on: [],
  });
  return next;
}

/** Update an ID without pre-empting duplicate or dependency validation. */
export function setWorkflowStepId(
  document: Record<string, unknown>,
  index: number,
  id: string,
): Record<string, unknown> {
  const next = cloneJsonDocument(document);
  const steps = editableSteps(next);
  if (!steps[index]) return next;
  steps[index] = {...steps[index], id};
  return next;
}

/** Preserve unknown, self, and cyclic dependencies for the authoritative validator. */
export function setWorkflowStepDependencies(
  document: Record<string, unknown>,
  index: number,
  dependsOn: string[],
): Record<string, unknown> {
  const next = cloneJsonDocument(document);
  const steps = editableSteps(next);
  if (!steps[index]) return next;
  steps[index] = {...steps[index], depends_on: dependsOn};
  return next;
}

export function moveWorkflowStep(
  document: Record<string, unknown>,
  index: number,
  direction: -1 | 1,
): Record<string, unknown> {
  const next = cloneJsonDocument(document);
  const steps = editableSteps(next);
  const target = index + direction;
  if (!steps[index] || !steps[target]) return next;
  [steps[index], steps[target]] = [steps[target], steps[index]];
  return next;
}

export function removeWorkflowStep(
  document: Record<string, unknown>,
  index: number,
): Record<string, unknown> {
  const next = cloneJsonDocument(document);
  const steps = editableSteps(next);
  if (steps[index]) steps.splice(index, 1);
  return next;
}

export function workflowDocumentJson(document: Record<string, unknown>): string {
  return JSON.stringify(document, null, 2);
}

export function dependencyInputValue(dependencies: readonly string[]): string {
  return dependencies.join(', ');
}

export function parseDependencyInput(value: string): string[] {
  return value.trim() === '' ? [] : value.split(',').map((item) => item.trim());
}
