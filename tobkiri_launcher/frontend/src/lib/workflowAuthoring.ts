import {assertWorkflowCanonicalInput} from './workflowCanonicalInput';
import {parseWorkflowAudio, type WorkflowAudioContent} from './workflowAudio';
import type {ApiDynamicFrontendCatalog, ApiFrontendContribution} from './apiTypes';
import {fetchFrontendCatalog, invokeFrontendCapability} from './defaultspackClient';
import {
  beginMutation,
  completeMutation,
  isMutationResultUnknown,
  listMutationJournal,
  markMutationUnknown,
  MutationResultUnknownError,
  type MutationJournalRecord,
} from './mutationJournal';
import {
  operationStatusBindingFromRecord,
  reconcileMutationStatus,
  type OperationStatusFetcher,
  type ReconciledMutationStatus,
} from './operationStatus';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from './generatedFrontendContractMap';
import {isVerifiedDynamicFrontendCatalog} from './presentation';

export const WORKFLOW_AUTHORING_PACK_ID = 'tobkiri_workflow_pack';
export const WORKFLOW_AUTHORING_CONTRACT_ID = 'tobkiri.workflow.v4';
export const WORKFLOW_AUTHORING_PROVIDER_ID = 'tobkiri.workflow.provider';
export const WORKFLOW_DOCUMENT_API_VERSION = 'io.tobkiri.workflow.v4';

const SHA256_DIGEST = /^sha256:[0-9a-f]{64}$/;

export const WORKFLOW_AUTHORING_OPERATIONS = [
  'definition.list',
  'definition.get',
  'definition.create',
  'definition.update',
  'definition.delete',
  'definition.validate',
  'definition.publish',
  'operation.palette',
  'run.create', 'run.get', 'run.observe', 'run.advance', 'run.cancel', 'run.stop', 'run.resume', 'run.step.resume',
] as const;

export type WorkflowAuthoringOperation =
  (typeof WORKFLOW_AUTHORING_OPERATIONS)[number];
export type WorkflowDefinitionState = 'draft' | 'published' | 'archived';

export interface WorkflowDefinition {
  definition_id: string;
  revision: number;
  revision_digest: string;
  etag: string;
  state: WorkflowDefinitionState;
  document: Record<string, unknown>;
  compiled?: Record<string, unknown>;
}

export interface WorkflowPaletteOperation {
  contract_id: string;
  contract_revision_digest: string;
  operation_id: string;
  function_principal_id: string;
  provider_id: string;
  input_schema_digest: string;
  effect_ceiling: string[];
  /** Display projections only; execution validates the captured canonical schema. */
  input_schema?: Record<string, unknown> | null;
  output_schema?: Record<string, unknown> | null;
  output_schema_digest?: string | null;
  whole_value_bindings?: boolean;
  json_pointer_bindings?: boolean;
}

export interface WorkflowPalette {
  catalog_digest: string;
  security_epoch: number;
  operations: WorkflowPaletteOperation[];
}

export interface WorkflowValidation {
  valid: boolean;
  errors: string[];
}

export interface WorkflowAuthoringDependencies {
  fetchCatalog: () => Promise<ApiDynamicFrontendCatalog>;
  invoke: typeof invokeFrontendCapability;
  /** Test seam for the authenticated, server-owned operation-status read. */
  fetchStatus?: OperationStatusFetcher;
}

const defaultDependencies: WorkflowAuthoringDependencies = {
  fetchCatalog: fetchFrontendCatalog,
  invoke: invokeFrontendCapability,
};

const inputKeys: Record<WorkflowAuthoringOperation, readonly string[]> = {
  'definition.list': [],
  'definition.get': ['definition_id'],
  'definition.create': ['definition_id', 'document'],
  'definition.update': ['definition_id', 'document', 'if_match'],
  'definition.delete': ['definition_id', 'if_match'],
  'definition.validate': ['document'],
  'definition.publish': ['definition_id', 'if_match'],
  'operation.palette': [],
  'run.create': ['definition_id', 'revision_digest', 'run_id', 'inputs'],
  'run.get': ['run_id'],
  'run.observe': ['run_id'],
  'run.advance': ['run_id'],
  'run.cancel': ['run_id'],
  'run.stop': ['run_id'],
  'run.resume': ['run_id'],
  'run.step.resume': ['run_id', 'step_id'],
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonEmpty(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function isDigest(value: unknown): value is string {
  return typeof value === 'string' && SHA256_DIGEST.test(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length
    && actual.every((key) => keys.includes(key));
}

function isSafeTimestamp(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= 0;
}

function isWorkflowCapability(
  contribution: ApiFrontendContribution,
  catalog: ApiDynamicFrontendCatalog,
  operationId: WorkflowAuthoringOperation,
): boolean {
  const contractId = operationId === 'run.stop' ? 'tobkiri.workflow.stop.v4' : WORKFLOW_AUTHORING_CONTRACT_ID;
  const providerId = operationId === 'run.stop' ? 'tobkiri.workflow.stop.provider' : WORKFLOW_AUTHORING_PROVIDER_ID;
  return contribution.contribution_id === `pack.${WORKFLOW_AUTHORING_PACK_ID}.${operationId}`
    && contribution.owner_pack_id === WORKFLOW_AUTHORING_PACK_ID
    && contribution.kind === 'action'
    && contribution.mode === 'declarative'
    && contribution.label === operationId
    && contribution.action_contract === contractId
    && contribution.operation_id === operationId
    && contribution.provider_id === providerId
    && contribution.function_id === providerId
    && contribution.build_identity === providerId
    && isDigest(contribution.owner_pack_hash)
    && isDigest(contribution.descriptor_hash)
    && contribution.resolved_profile_id === catalog.profile_id
    && contribution.resolved_profile_revision === catalog.profile_revision
    && contribution.resolved_activation_id === catalog.activation_id
    && contribution.resolved_plan_hash === catalog.plan_hash
    && !catalog.quarantined_pack_ids.includes(WORKFLOW_AUTHORING_PACK_ID);
}

/** Resolve one exact v4 authoring action from the current dynamic catalog. */
export function resolveWorkflowAuthoringCapability(
  catalog: ApiDynamicFrontendCatalog | null,
  operationId: WorkflowAuthoringOperation,
): ApiFrontendContribution | null {
  if (!isVerifiedDynamicFrontendCatalog(catalog)) return null;
  const matches = catalog.contributions.filter((contribution) => (
    isWorkflowCapability(contribution, catalog, operationId)
  ));
  return matches.length === 1 ? matches[0] : null;
}

interface ResolvedWorkflowAuthoringCapability {
  catalog: ApiDynamicFrontendCatalog;
  capability: ApiFrontendContribution;
}

async function resolveWorkflowAuthoringInvocation(
  operationId: WorkflowAuthoringOperation,
  dependencies: WorkflowAuthoringDependencies,
): Promise<ResolvedWorkflowAuthoringCapability> {
  const catalog = await dependencies.fetchCatalog();
  const capability = resolveWorkflowAuthoringCapability(catalog, operationId);
  if (!capability) {
    throw new WorkflowAuthoringCapabilityError(
      `The active Profile does not admit ${operationId} for Workflow v4 authoring.`,
    );
  }
  return {catalog, capability};
}

function invokeResolvedWorkflowAuthoringCapability(
  resolved: ResolvedWorkflowAuthoringCapability,
  payload: Record<string, unknown>,
  dependencies: WorkflowAuthoringDependencies,
  requestId?: string,
): Promise<unknown> {
  const {catalog, capability} = resolved;
  return dependencies.invoke({
    profileId: catalog.profile_id,
    profileRevision: catalog.profile_revision,
    activationId: catalog.activation_id,
    planHash: catalog.plan_hash,
    catalogHash: catalog.catalog_hash,
    contributionId: capability.contribution_id,
    ownerPackId: capability.owner_pack_id,
    contractId: capability.action_contract,
    payload,
  }, requestId ? {requestId} : {});
}

function assertExactPayload(
  operationId: WorkflowAuthoringOperation,
  payload: Record<string, unknown>,
): void {
  assertWorkflowCanonicalInput(payload);
  if (!hasExactKeys(payload, inputKeys[operationId])) {
    throw new WorkflowAuthoringResponseError(
      `Workflow authoring payload is not exact for ${operationId}.`,
    );
  }
}

/** Invoke an exact, currently admitted v4 capability. Never select a fallback. */
export async function invokeWorkflowAuthoringOperation(
  operationId: WorkflowAuthoringOperation,
  payload: Record<string, unknown>,
  dependencies: WorkflowAuthoringDependencies = defaultDependencies,
  requestId?: string,
): Promise<unknown> {
  assertExactPayload(operationId, payload);
  return invokeResolvedWorkflowAuthoringCapability(
    await resolveWorkflowAuthoringInvocation(operationId, dependencies),
    payload,
    dependencies,
    requestId,
  );
}

function parseDefinition(value: unknown): WorkflowDefinition | null {
  if (!isRecord(value)) return null;
  const definitionId = value.definition_id;
  const revision = value.revision;
  const revisionDigest = value.revision_digest;
  const etag = value.etag;
  const state = value.state;
  const document = value.document;
  const compiled = value.compiled;
  const compiledRecord = isRecord(compiled) ? compiled : undefined;
  const expectedKeys = compiled === undefined
    ? [
      'definition_id',
      'revision',
      'revision_digest',
      'etag',
      'state',
      'document',
      'created_at',
      'updated_at',
    ]
    : [
      'definition_id',
      'revision',
      'revision_digest',
      'etag',
      'state',
      'document',
      'compiled',
      'created_at',
      'updated_at',
    ];
  if (
    !hasExactKeys(value, expectedKeys)
    || !isNonEmpty(definitionId)
    || typeof revision !== 'number'
    || !Number.isSafeInteger(revision)
    || revision < 1
    || !isDigest(revisionDigest)
    || !isNonEmpty(etag)
    || (state !== 'draft' && state !== 'published' && state !== 'archived')
    || !isRecord(document)
    || (compiled !== undefined && !compiledRecord)
    || !isSafeTimestamp(value.created_at)
    || !isSafeTimestamp(value.updated_at)
    || value.updated_at < value.created_at
  ) return null;
  return {
    definition_id: definitionId,
    revision,
    revision_digest: revisionDigest,
    etag,
    state,
    document,
    ...(compiledRecord ? {compiled: compiledRecord} : {}),
  };
}

/** Parse a server-owned Definition or reject a malformed capability result. */
export function parseWorkflowDefinition(value: unknown): WorkflowDefinition | null {
  return parseDefinition(value);
}

/** Reject malformed or duplicated Definition records before presenting them. */
export function parseWorkflowDefinitionList(value: unknown): WorkflowDefinition[] | null {
  if (!isRecord(value) || !hasExactKeys(value, ['definitions']) || !Array.isArray(value.definitions)) return null;
  const definitions = value.definitions.map(parseDefinition);
  if (definitions.some((definition) => definition === null)) return null;
  const verified = definitions as WorkflowDefinition[];
  return new Set(verified.map((definition) => definition.definition_id)).size === verified.length
    ? verified
    : null;
}

function boundedSchema(value: unknown): value is Record<string, unknown> | null {
  if (value === null) return true;
  if (!isRecord(value)) return false;
  try {return JSON.stringify(value).length <= 65536;} catch {return false;}
}
function paletteKeys(value: Record<string, unknown>): boolean {
  const required = ['contract_id', 'contract_revision_digest', 'operation_id', 'function_principal_id', 'provider_id', 'input_schema_digest', 'effect_ceiling'];
  const optional = ['schemas', 'projection', 'output_schema_digest', 'input_ports', 'output_ports'];
  return hasExactKeys(value, required) || hasExactKeys(value, [...required, ...optional]);
}
function parsePaletteOperation(value: unknown): WorkflowPaletteOperation | null {
  if (
    !isRecord(value)
    || !paletteKeys(value)
    || !Array.isArray(value.effect_ceiling)
  ) return null;
  if ('schemas' in value && (value.projection !== 'display-reduced' || !isRecord(value.schemas) || !hasExactKeys(value.schemas, ['input', 'output']) || !boundedSchema(value.schemas.input) || !boundedSchema(value.schemas.output))) return null;
  if ('output_schema_digest' in value && value.output_schema_digest !== null && !isDigest(value.output_schema_digest)) return null;
  for (const key of ['input_ports', 'output_ports']) {
    if (!(key in value)) continue;
    const ports = value[key];
    if (!Array.isArray(ports) || ports.length > 128 || ports.some((port) => !isRecord(port) || !hasExactKeys(port, ['name', 'required', 'schema', ...('path' in port ? ['path'] : [])]) || ('path' in port && (!Array.isArray(port.path) || port.path.length !== 0 || port.name !== '$')) || !isNonEmpty(port.name) || typeof port.required !== 'boolean' || !boundedSchema(port.schema))) return null;
  }
  const effectCeiling = value.effect_ceiling;
  if (
    !isNonEmpty(value.contract_id)
    || !isDigest(value.contract_revision_digest)
    || !isNonEmpty(value.operation_id)
    || !isNonEmpty(value.function_principal_id)
    || !isNonEmpty(value.provider_id)
    || !isDigest(value.input_schema_digest)
    || !effectCeiling.every(isNonEmpty)
  ) return null;
  return {
    contract_id: value.contract_id,
    contract_revision_digest: value.contract_revision_digest,
    operation_id: value.operation_id,
    function_principal_id: value.function_principal_id,
    provider_id: value.provider_id,
    input_schema_digest: value.input_schema_digest,
    effect_ceiling: [...effectCeiling],
    ...('schemas' in value ? {input_schema: (value.schemas as Record<string, unknown>).input as Record<string, unknown> | null} : {}),
    ...('schemas' in value ? {output_schema: (value.schemas as Record<string, unknown>).output as Record<string, unknown> | null} : {}),
    ...('output_schema_digest' in value ? {output_schema_digest: value.output_schema_digest as string | null} : {}),
  };
}

/** Parse exact palette identities, rejecting a duplicated full operation identity. */
export function parseWorkflowPalette(value: unknown): WorkflowPalette | null {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['catalog_digest', 'security_epoch', 'operations', ...('port_binding' in value ? ['port_binding'] : [])])
    || !isDigest(value.catalog_digest)
    || typeof value.security_epoch !== 'number'
    || !Number.isSafeInteger(value.security_epoch)
    || value.security_epoch < 0
    || !Array.isArray(value.operations)
  ) return null;
  if ('port_binding' in value) {
    const binding = value.port_binding;
    if (!isRecord(binding) || !hasExactKeys(binding, ['reference_format', 'requires_depends_on', ...('whole_value_reference_format' in binding ? ['whole_value_reference_format'] : []), ...('json_pointer_reference_format' in binding ? ['json_pointer_reference_format'] : [])]) || binding.reference_format !== '${steps.<step_id>.output.<path>}' || binding.requires_depends_on !== true) return null;
    if ('whole_value_reference_format' in binding && binding.whole_value_reference_format !== '${steps.<step_id>.output}') return null;
    if ('json_pointer_reference_format' in binding && binding.json_pointer_reference_format !== '${steps.<step_id>.output@<json_pointer>}') return null;
  }
  const operations = value.operations.map(parsePaletteOperation);
  if (operations.some((operation) => operation === null)) return null;
  const whole = isRecord(value.port_binding) && value.port_binding.whole_value_reference_format === '${steps.<step_id>.output}';
  const verified = (operations as WorkflowPaletteOperation[]).map(operation => ({...operation,...(whole?{whole_value_bindings:true}:{}),...(isRecord(value.port_binding)&&value.port_binding.json_pointer_reference_format==='${steps.<step_id>.output@<json_pointer>}'?{json_pointer_bindings:true}:{})}));
  if (verified.some((operation) => 'input_schema' in operation) && !('port_binding' in value)) return null;
  const identities = verified.map((operation) => [
    operation.function_principal_id,
    operation.provider_id,
    operation.contract_id,
    operation.operation_id,
  ].map((part) => JSON.stringify(part)).join('\u0000'));
  if (new Set(identities).size !== identities.length) return null;
  return {
    catalog_digest: value.catalog_digest,
    security_epoch: value.security_epoch,
    operations: verified,
  };
}

export function parseWorkflowValidation(value: unknown): WorkflowValidation | null {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ['valid', 'errors'])
    || typeof value.valid !== 'boolean'
    || !Array.isArray(value.errors)
  ) {
    return null;
  }
  if (!value.errors.every(isNonEmpty)) return null;
  return {valid: value.valid, errors: [...value.errors]};
}

/** Parse JSON-only v4 authoring input before it can cross the capability boundary. */
export function parseWorkflowDefinitionText(text: string): Record<string, unknown> | null {
  try {
    const document: unknown = JSON.parse(text);
    if (
      !isRecord(document)
      || document.workflow_api_version !== WORKFLOW_DOCUMENT_API_VERSION
      || !Array.isArray(document.steps)
    ) return null;
    return document;
  } catch {
    return null;
  }
}

export class WorkflowAuthoringCapabilityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'WorkflowAuthoringCapabilityError';
  }
}

export class WorkflowAuthoringResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'WorkflowAuthoringResponseError';
  }
}

type WorkflowMutationOperation = Extract<
  WorkflowAuthoringOperation,
  'definition.create' | 'definition.update' | 'definition.delete' | 'definition.publish' | 'run.create' | 'run.advance' | 'run.cancel' | 'run.stop' | 'run.resume' | 'run.step.resume'
>;

export interface WorkflowMutationRecovery {
  requestId: string;
  operationId: WorkflowMutationOperation | null;
  state: ReconciledMutationStatus['state'] | 'blocked';
  safeErrorCode: string | null;
}

function isWorkflowMutationOperation(
  value: unknown,
): value is WorkflowMutationOperation {
  return value === 'definition.create'
    || value === 'definition.update'
    || value === 'definition.delete'
    || value === 'definition.publish'
    || value === 'run.create' || value === 'run.advance' || value === 'run.cancel' || value === 'run.stop' || value === 'run.resume' || value === 'run.step.resume';
}

function workflowMutationMetadata(
  operationId: WorkflowMutationOperation,
  resolved: ResolvedWorkflowAuthoringCapability,
): Record<string, unknown> {
  const {catalog, capability} = resolved;
  // Persist the original, exact dynamic route so a reload cannot silently
  // reconcile this request through a replacement Profile or capability.
  return {
    kind: 'workflow-authoring',
    operation_id: operationId,
    contract_id: capability.action_contract,
    contract_map_digest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
    workflow_profile_id: catalog.profile_id,
    workflow_profile_revision: catalog.profile_revision,
    workflow_activation_id: catalog.activation_id,
    workflow_plan_hash: catalog.plan_hash,
    workflow_catalog_hash: catalog.catalog_hash,
    workflow_contribution_id: capability.contribution_id,
    workflow_owner_pack_id: capability.owner_pack_id,
    workflow_owner_pack_hash: capability.owner_pack_hash,
    workflow_provider_id: capability.provider_id,
    workflow_function_id: capability.function_id,
    workflow_build_identity: capability.build_identity,
    workflow_descriptor_hash: capability.descriptor_hash,
  };
}

function workflowRecordOperation(
  record: MutationJournalRecord,
): WorkflowMutationOperation | null {
  return record.metadata.kind === 'workflow-authoring'
    && isWorkflowMutationOperation(record.metadata.operation_id)
    ? record.metadata.operation_id
    : null;
}

function hasExactWorkflowTarget(
  record: MutationJournalRecord,
  resolved: ResolvedWorkflowAuthoringCapability,
): boolean {
  const {catalog, capability} = resolved;
  const metadata = record.metadata;
  return metadata.contract_id === capability.action_contract
    && metadata.contract_map_digest === PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST
    && metadata.workflow_profile_id === catalog.profile_id
    && metadata.workflow_profile_revision === catalog.profile_revision
    && metadata.workflow_activation_id === catalog.activation_id
    && metadata.workflow_plan_hash === catalog.plan_hash
    && metadata.workflow_catalog_hash === catalog.catalog_hash
    && metadata.workflow_contribution_id === capability.contribution_id
    && metadata.workflow_owner_pack_id === capability.owner_pack_id
    && metadata.workflow_owner_pack_hash === capability.owner_pack_hash
    && metadata.workflow_provider_id === capability.provider_id
    && metadata.workflow_function_id === capability.function_id
    && metadata.workflow_build_identity === capability.build_identity
    && metadata.workflow_descriptor_hash === capability.descriptor_hash;
}

function unknownWorkflowMutationRecords(): MutationJournalRecord[] {
  return listMutationJournal().filter((record) => (
    record.state === 'unknown' && record.metadata.kind === 'workflow-authoring'
  ));
}

/** Whether a reload must keep Workflow writes disabled for a durable request. */
export function hasUnknownWorkflowMutation(): boolean {
  return unknownWorkflowMutationRecords().length > 0;
}

/**
 * Reconcile every durable Workflow authoring write using its original request
 * ID. This never invokes a write operation or accepts a changed dynamic
 * capability target.
 */
export async function reconcileUnknownWorkflowMutations(
  refresh: () => Promise<void>,
  dependencies: WorkflowAuthoringDependencies = defaultDependencies,
): Promise<WorkflowMutationRecovery[]> {
  const records = unknownWorkflowMutationRecords();
  const recoveries: WorkflowMutationRecovery[] = [];
  for (const record of records) {
    const operationId = workflowRecordOperation(record);
    if (!operationId) {
      recoveries.push({
        requestId: record.requestId,
        operationId: null,
        state: 'blocked',
        safeErrorCode: null,
      });
      continue;
    }
    try {
      const resolved = await resolveWorkflowAuthoringInvocation(operationId, dependencies);
      if (!hasExactWorkflowTarget(record, resolved)) {
        throw new WorkflowAuthoringResponseError(
          'The journaled Workflow capability target is stale or tampered.',
        );
      }
      const binding = operationStatusBindingFromRecord(record, {
        operationId,
        contractId: resolved.capability.action_contract,
        mapArtifactDigest: PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST,
      });
      const reconciled = await reconcileMutationStatus({
        record,
        binding,
        refresh,
        fetcher: dependencies.fetchStatus,
      });
      recoveries.push({
        requestId: record.requestId,
        operationId,
        state: reconciled.state,
        safeErrorCode: reconciled.status.safe_error_code,
      });
    } catch {
      // A failed status lookup or any changed local binding is deliberately
      // durable and blocked. A later authenticated reload may retry only the
      // same request ID.
      recoveries.push({
        requestId: record.requestId,
        operationId,
        state: 'blocked',
        safeErrorCode: null,
      });
    }
  }
  return recoveries;
}

async function mutateWorkflowAuthoring<T>(
  operationId: WorkflowMutationOperation,
  payload: Record<string, unknown>,
  parse: (value: unknown) => T | null,
  dependencies: WorkflowAuthoringDependencies = defaultDependencies,
): Promise<T> {
  assertExactPayload(operationId, payload);
  const entityId = operationId.startsWith('run.') ? payload.run_id : payload.definition_id;
  if (!isNonEmpty(entityId)) {
    throw new WorkflowAuthoringResponseError(
      `Workflow authoring payload is missing a definition ID for ${operationId}.`,
    );
  }
  // The durable key intentionally contains only the affected owner identity.
  // Definition JSON and other user-authored input are not needed for status
  // reconciliation and must not be duplicated into browser storage.
  const key = operationId.startsWith('run.') ? `workflow-v4:run:${entityId}:${operationId}` : `workflow-v4:definition:${entityId}`;
  const resolved = await resolveWorkflowAuthoringInvocation(operationId, dependencies);
  const mutation = beginMutation(
    key,
    workflowMutationMetadata(operationId, resolved),
  );
  try {
    const result = await invokeResolvedWorkflowAuthoringCapability(
      resolved,
      payload,
      dependencies,
      mutation.requestId,
    );
    const parsed = parse(result);
    if (parsed === null) {
      throw new WorkflowAuthoringResponseError(
        `Malformed response for ${operationId}; the mutation outcome is not known.`,
      );
    }
    completeMutation(key, mutation.requestId);
    return parsed;
  } catch (error) {
    if (isMutationResultUnknown(error) || error instanceof WorkflowAuthoringResponseError) {
      markMutationUnknown(key, mutation.requestId);
      throw new MutationResultUnknownError(key, mutation.requestId);
    }
    completeMutation(key, mutation.requestId);
    throw error;
  }
}

async function readWorkflowAuthoring<T>(
  operationId: Extract<WorkflowAuthoringOperation, 'definition.list' | 'definition.get' | 'definition.validate' | 'operation.palette' | 'run.get' | 'run.observe'>,
  payload: Record<string, unknown>,
  parse: (value: unknown) => T | null,
  dependencies: WorkflowAuthoringDependencies = defaultDependencies,
): Promise<T> {
  const result = await invokeWorkflowAuthoringOperation(operationId, payload, dependencies);
  const parsed = parse(result);
  if (parsed === null) {
    throw new WorkflowAuthoringResponseError(`Malformed response for ${operationId}.`);
  }
  return parsed;
}

export const loadWorkflowDefinitions = (
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowDefinition[]> => readWorkflowAuthoring(
  'definition.list', {}, parseWorkflowDefinitionList, dependencies,
);

export const getWorkflowDefinition = (
  definitionId: string,
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowDefinition> => readWorkflowAuthoring(
  'definition.get', {definition_id: definitionId}, parseWorkflowDefinition, dependencies,
);

export const loadWorkflowPalette = (
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowPalette> => readWorkflowAuthoring(
  'operation.palette', {}, parseWorkflowPalette, dependencies,
);

export const validateWorkflowDefinition = (
  document: Record<string, unknown>,
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowValidation> => readWorkflowAuthoring(
  'definition.validate', {document}, parseWorkflowValidation, dependencies,
);

export const createWorkflowDefinition = (
  definitionId: string,
  document: Record<string, unknown>,
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowDefinition> => mutateWorkflowAuthoring(
  'definition.create', {definition_id: definitionId, document}, parseWorkflowDefinition, dependencies,
);

export const updateWorkflowDefinition = (
  definitionId: string,
  document: Record<string, unknown>,
  ifMatch: string,
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowDefinition> => mutateWorkflowAuthoring(
  'definition.update',
  {definition_id: definitionId, document, if_match: ifMatch},
  parseWorkflowDefinition,
  dependencies,
);

export const deleteWorkflowDefinition = (
  definitionId: string,
  ifMatch: string,
  dependencies?: WorkflowAuthoringDependencies,
): Promise<{deleted: true}> => mutateWorkflowAuthoring(
  'definition.delete',
  {definition_id: definitionId, if_match: ifMatch},
  (value) => isRecord(value)
    && hasExactKeys(value, ['deleted'])
    && value.deleted === true
    ? {deleted: true}
    : null,
  dependencies,
);

export const publishWorkflowDefinition = (
  definitionId: string,
  ifMatch: string,
  dependencies?: WorkflowAuthoringDependencies,
): Promise<WorkflowDefinition> => mutateWorkflowAuthoring(
  'definition.publish',
  {definition_id: definitionId, if_match: ifMatch},
  parseWorkflowDefinition,
  dependencies,
);


export const WORKFLOW_RUN_STATES = ['queued', 'running', 'paused', 'waiting_approval', 'succeeded', 'failed', 'cancelled', 'timed_out', 'needs_reconciliation'] as const;
export interface WorkflowRunView {
  runId: string;
  definitionId: string;
  state: typeof WORKFLOW_RUN_STATES[number];
  revisionDigest: string;
  attempts: Array<{stepId: string; state: string; number: number; approvalRequestId?: string}>;
  previews?: Array<{stepId: string; text?: string; audio?: WorkflowAudioContent}>;
  detailsOmitted?: boolean;
}
/** Project status and bounded text/audio results, never inputs or arbitrary Provider bodies. */
export function parseWorkflowRun(value: unknown): WorkflowRunView | null {
  if (!isRecord(value)) return null;
  const run = isRecord(value.run) ? value.run : value;
  if (!isNonEmpty(run.run_id) || !isNonEmpty(run.definition_id) || !isDigest(run.revision_digest) || !WORKFLOW_RUN_STATES.includes(run.state as typeof WORKFLOW_RUN_STATES[number])) return null;
  const attempts: WorkflowRunView['attempts'] = [];
  const previews: NonNullable<WorkflowRunView['previews']> = [];
  if (value.attempts !== undefined) {
    if (!Array.isArray(value.attempts) || value.attempts.length > 10240) return null;
    const states = ['pending', 'dispatching', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled', 'timed_out', 'ambiguous_effect', 'needs_reconciliation'];
    for (const attempt of value.attempts) {
      if (!isRecord(attempt) || attempt.run_id !== run.run_id || !isNonEmpty(attempt.step_id) || !states.includes(String(attempt.state)) || !Number.isSafeInteger(attempt.attempt_number) || Number(attempt.attempt_number) < 1) return null;
      const approvalId = attempt.approval_request_id;
      if (approvalId !== undefined && (attempt.state !== 'waiting_approval' || typeof approvalId !== 'string' || !/^workflow-approval-sha256:[0-9a-f]{64}$/.test(approvalId))) return null;
      attempts.push({stepId: attempt.step_id, state: String(attempt.state), number: Number(attempt.attempt_number), ...(typeof approvalId === 'string' ? {approvalRequestId: approvalId} : {})});
      if (attempt.state === 'succeeded' && isRecord(attempt.outcome) && previews.length < 16) {
        const text = typeof attempt.outcome.text === 'string' && attempt.outcome.text.length <= 262144 ? attempt.outcome.text : undefined;
        const audio = parseWorkflowAudio(attempt.outcome.content);
        if (text !== undefined || audio) previews.push({stepId: attempt.step_id, ...(text !== undefined ? {text} : {}), ...(audio ? {audio} : {})});
      }
    }
  }
  return {runId: run.run_id, definitionId: run.definition_id, state: run.state as WorkflowRunView['state'], revisionDigest: run.revision_digest, attempts, ...(previews.length ? {previews} : {}), ...(value.details_omitted === true ? {detailsOmitted:true} : {})};
}
export const createWorkflowRun = (definitionId: string, revisionDigest: string, runId: string, inputs: Record<string, unknown>, dependencies?: WorkflowAuthoringDependencies): Promise<WorkflowRunView> => mutateWorkflowAuthoring(
  'run.create', {definition_id: definitionId, revision_digest: revisionDigest, run_id: runId, inputs}, (value) => {const run = parseWorkflowRun(value); return run?.runId === runId && run.definitionId === definitionId && run.revisionDigest === revisionDigest ? run : null;}, dependencies,
);
export const getWorkflowRun = (runId: string, dependencies?: WorkflowAuthoringDependencies): Promise<WorkflowRunView> => readWorkflowAuthoring('run.observe', {run_id: runId}, (value) => {const run = parseWorkflowRun(value); return run?.runId === runId ? run : null;}, dependencies);
export const controlWorkflowRun = (operation: 'run.advance' | 'run.cancel' | 'run.stop' | 'run.resume', runId: string, dependencies?: WorkflowAuthoringDependencies): Promise<WorkflowRunView> => mutateWorkflowAuthoring(operation, {run_id: runId}, (value) => {const run = parseWorkflowRun(value); return run?.runId === runId ? run : null;}, dependencies);

export const resumeWorkflowStep = (runId: string, stepId: string, dependencies?: WorkflowAuthoringDependencies): Promise<{runId: string; stepId: string; state: string}> => mutateWorkflowAuthoring(
  'run.step.resume', {run_id: runId, step_id: stepId}, (value) => {
    if (!isRecord(value) || value.run_id !== runId || value.step_id !== stepId || !['pending', 'dispatching', 'running', 'waiting_approval', 'succeeded', 'failed', 'cancelled', 'timed_out', 'ambiguous_effect', 'needs_reconciliation'].includes(String(value.state))) return null;
    return {runId, stepId, state: String(value.state)};
  }, dependencies,
);
