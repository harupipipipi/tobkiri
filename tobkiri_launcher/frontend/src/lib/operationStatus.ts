import {fetchRuntimeOperationStatus} from './api.ts';
import {
  bindMutationStatusDigest,
  completeMutation,
  listMutationJournal,
  type MutationJournalRecord,
} from './mutationJournal.ts';
import {PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST} from './generatedFrontendContractMap.ts';

export const OPERATION_STATUS_API_VERSION = 'io.tobkiri.control-operation-status.v1' as const;
export const OPERATION_STATUS_STATES = ['pending', 'succeeded', 'failed', 'indeterminate'] as const;
export type OperationStatusState = typeof OPERATION_STATUS_STATES[number];

export interface OperationStatus {
  runtime_surface_api_version: 'io.tobkiri.launcher.runtime-surface.v4';
  operation_status_api_version: typeof OPERATION_STATUS_API_VERSION;
  request_id: string;
  operation_id: string;
  contract_id: string;
  request_digest: string;
  state: OperationStatusState;
  result: unknown;
  result_digest: string | null;
  record_refs: Array<{kind: string; id: string; digest?: string}>;
  safe_error_code: string | null;
  created_at: number;
  updated_at: number;
}

export interface OperationStatusBinding {
  requestId: string;
  operationId: string;
  contractId: string;
  mapArtifactDigest: string;
  requestDigest?: string;
}

export type OperationStatusFetcher = (
  requestId: string,
  signal?: AbortSignal,
) => Promise<unknown>;

export class OperationStatusValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'OperationStatusValidationError';
  }
}

export interface ReconciledMutationStatus {
  state: OperationStatusState | 'stale';
  status: OperationStatus;
  reconciled: boolean;
}

interface ReconcileMutationStatusOptions {
  record: MutationJournalRecord;
  binding: OperationStatusBinding;
  statusPhase?: string;
  refresh: () => Promise<void>;
  verifySuccess?: (status: OperationStatus) => boolean;
  isCurrent?: () => boolean;
  fetcher?: OperationStatusFetcher;
  completeOnTerminal?: boolean;
  signal?: AbortSignal;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DIGEST_PATTERN = /^sha256:[0-9a-f]{64}$/;
const RUNTIME_SURFACE_API_VERSION = 'io.tobkiri.launcher.runtime-surface.v4';
const OPERATION_STATUS_KEYS = [
  'runtime_surface_api_version',
  'operation_status_api_version',
  'request_id',
  'operation_id',
  'contract_id',
  'request_digest',
  'state',
  'result',
  'result_digest',
  'record_refs',
  'safe_error_code',
  'created_at',
  'updated_at',
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length
    && keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function isDigest(value: unknown): value is string {
  return typeof value === 'string' && DIGEST_PATTERN.test(value);
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new OperationStatusValidationError(`Operation status field ${field} is invalid.`);
  }
  return value;
}

function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isInteger(value) || !Number.isSafeInteger(value)) {
      throw new OperationStatusValidationError('Operation status contains a non-canonical number.');
    }
    return String(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (!isRecord(value)) {
    throw new OperationStatusValidationError('Operation status contains a non-canonical value.');
  }
  return `{${Object.keys(value).sort().map((key) => (
    `${JSON.stringify(key)}:${canonicalJson(value[key])}`
  )).join(',')}}`;
}

async function canonicalDigest(value: unknown): Promise<string> {
  let bytes: Uint8Array;
  try {
    bytes = new TextEncoder().encode(canonicalJson(value));
    const subtle = globalThis.crypto?.subtle;
    if (!subtle) throw new Error('Web Crypto is unavailable.');
    const digest = await subtle.digest('SHA-256', bytes);
    return `sha256:${[...new Uint8Array(digest)]
      .map((item) => item.toString(16).padStart(2, '0'))
      .join('')}`;
  } catch (error) {
    if (error instanceof OperationStatusValidationError) throw error;
    throw new OperationStatusValidationError('Operation status digest could not be verified.');
  }
}

function validateRecordRefs(value: unknown): OperationStatus['record_refs'] {
  if (!Array.isArray(value)) {
    throw new OperationStatusValidationError('Operation status record references are invalid.');
  }
  return value.map((item) => {
    if (!isRecord(item)) {
      throw new OperationStatusValidationError('Operation status record reference is invalid.');
    }
    const hasDigest = Object.prototype.hasOwnProperty.call(item, 'digest');
    const keys = hasDigest ? ['kind', 'id', 'digest'] : ['kind', 'id'];
    if (!exactKeys(item, keys) || typeof item.kind !== 'string' || !item.kind
      || typeof item.id !== 'string' || !item.id
      || (hasDigest && !isDigest(item.digest))) {
      throw new OperationStatusValidationError('Operation status record reference is invalid.');
    }
    return hasDigest
      ? {kind: item.kind, id: item.id, digest: item.digest as string}
      : {kind: item.kind, id: item.id};
  });
}

/** Validate the exact server projection before any journal transition. */
export async function validateOperationStatus(
  value: unknown,
  binding: OperationStatusBinding,
): Promise<OperationStatus> {
  if (!isRecord(value) || !exactKeys(value, OPERATION_STATUS_KEYS)) {
    throw new OperationStatusValidationError('Operation status projection is stale or tampered.');
  }
  const requestId = value.request_id;
  const operationId = value.operation_id;
  const contractId = value.contract_id;
  const requestDigest = value.request_digest;
  const state = value.state;
  const resultDigest = value.result_digest;
  const safeErrorCode = value.safe_error_code;
  const createdAt = value.created_at;
  const updatedAt = value.updated_at;
  if (
    binding.mapArtifactDigest !== PINNED_FRONTEND_CONTRACT_MAP_ARTIFACT_DIGEST
    || value.runtime_surface_api_version !== RUNTIME_SURFACE_API_VERSION
    || value.operation_status_api_version !== OPERATION_STATUS_API_VERSION
    || requestId !== binding.requestId
    || !UUID_PATTERN.test(binding.requestId)
    || operationId !== binding.operationId
    || contractId !== binding.contractId
    || !isDigest(requestDigest)
    || !OPERATION_STATUS_STATES.includes(state as OperationStatusState)
    || (resultDigest !== null && !isDigest(resultDigest))
    || (safeErrorCode !== null
      && (typeof safeErrorCode !== 'string' || safeErrorCode.length === 0))
    || typeof createdAt !== 'number'
    || !Number.isFinite(createdAt)
    || createdAt < 0
    || typeof updatedAt !== 'number'
    || !Number.isFinite(updatedAt)
    || updatedAt < createdAt
  ) {
    throw new OperationStatusValidationError('Operation status identity or digest is invalid.');
  }
  if (binding.requestDigest !== undefined && requestDigest !== binding.requestDigest) {
    throw new OperationStatusValidationError('Operation status belongs to a stale request digest.');
  }

  const recordRefs = validateRecordRefs(value.record_refs);
  const normalizedState = state as OperationStatusState;
  if (normalizedState === 'pending') {
    if (value.result !== null || resultDigest !== null || recordRefs.length > 0 || safeErrorCode !== null) {
      throw new OperationStatusValidationError('Pending operation status contains a terminal result.');
    }
  } else if (value.result === null) {
    if (resultDigest !== null || normalizedState === 'succeeded') {
      throw new OperationStatusValidationError('Terminal operation status has no exact result.');
    }
  } else {
    if (!isRecord(value.result) || !resultDigest
      || resultDigest !== await canonicalDigest(value.result)) {
      throw new OperationStatusValidationError('Terminal operation result digest is invalid.');
    }
  }

  return {
    runtime_surface_api_version: RUNTIME_SURFACE_API_VERSION,
    operation_status_api_version: OPERATION_STATUS_API_VERSION,
    request_id: requestId as string,
    operation_id: operationId as string,
    contract_id: contractId as string,
    request_digest: requestDigest as string,
    state: normalizedState,
    result: value.result,
    result_digest: resultDigest as string | null,
    record_refs: recordRefs,
    safe_error_code: safeErrorCode as string | null,
    created_at: createdAt as number,
    updated_at: updatedAt as number,
  };
}

const statusRequests = new Map<string, Promise<OperationStatus>>();

function cancellableStatusFetch(
  fetcher: OperationStatusFetcher,
  requestId: string,
  signal?: AbortSignal,
): Promise<unknown> {
  if (!signal) return fetcher(requestId);
  if (signal.aborted) {
    return Promise.reject(new OperationStatusValidationError('Operation status reconciliation was cancelled.'));
  }
  return new Promise((resolve, reject) => {
    const finish = (callback: (value: unknown) => void, value: unknown): void => {
      signal.removeEventListener('abort', abort);
      callback(value);
    };
    const abort = (): void => {
      finish(reject, new OperationStatusValidationError('Operation status reconciliation was cancelled.'));
    };
    signal.addEventListener('abort', abort, {once: true});
    try {
      const response = fetcher(requestId, signal);
      response.then(
        (value) => finish(resolve, value),
        (error) => finish(reject, error),
      );
    } catch (error) {
      finish(reject, error);
    }
  });
}

/** Fetch one status projection at a time for each exact request binding. */
export function fetchOperationStatus(
  binding: OperationStatusBinding,
  fetcher: OperationStatusFetcher = fetchRuntimeOperationStatus,
  signal?: AbortSignal,
): Promise<OperationStatus> {
  const key = [
    binding.requestId,
    binding.operationId,
    binding.contractId,
    binding.mapArtifactDigest,
    binding.requestDigest ?? '',
  ].join('\u0000');
  const existing = statusRequests.get(key);
  if (existing) return existing;
  const request = cancellableStatusFetch(fetcher, binding.requestId, signal)
    .then((value) => validateOperationStatus(value, binding))
    .finally(() => {
      if (statusRequests.get(key) === request) statusRequests.delete(key);
    });
  statusRequests.set(key, request);
  return request;
}

/**
 * Reconcile a journaled unknown mutation without ever creating a replacement
 * request. Terminal success/failure is released only after authoritative
 * refresh and a current-binding check.
 */
export async function reconcileMutationStatus({
  record,
  binding,
  statusPhase = 'primary',
  refresh,
  verifySuccess,
  isCurrent = () => true,
  fetcher,
  completeOnTerminal = true,
  signal,
}: ReconcileMutationStatusOptions): Promise<ReconciledMutationStatus> {
  if (signal?.aborted) {
    throw new OperationStatusValidationError('Operation status reconciliation was cancelled.');
  }
  if (record.state !== 'unknown') {
    throw new OperationStatusValidationError('Only an unknown mutation may be status-reconciled.');
  }
  const metadataOperationId = record.metadata.operation_id;
  const metadataContractId = record.metadata.contract_id;
  const metadataMapArtifactDigest = record.metadata.contract_map_digest;
  const approvalStatusPhase = statusPhase === 'approval';
  if (
    (metadataOperationId !== binding.operationId
      && !(approvalStatusPhase && metadataOperationId === 'approval.candidate'))
    || metadataContractId !== binding.contractId
    || metadataMapArtifactDigest !== binding.mapArtifactDigest
  ) {
    throw new OperationStatusValidationError('The journaled operation binding is stale or tampered.');
  }
  const statusRequestIds = record.metadata.status_request_ids;
  if (statusRequestIds !== undefined && !isRecord(statusRequestIds)) {
    throw new OperationStatusValidationError('The journaled operation status identities are invalid.');
  }
  const statusRequestId = binding.requestId;
  const storedRequestId = isRecord(statusRequestIds)
    ? statusRequestIds[statusPhase]
    : undefined;
  if (storedRequestId !== undefined && storedRequestId !== statusRequestId) {
    throw new OperationStatusValidationError('The journaled operation status identity changed.');
  }
  const statusDigests = record.metadata.status_request_digests;
  if (statusDigests !== undefined && !isRecord(statusDigests)) {
    throw new OperationStatusValidationError('The journaled operation status digests are invalid.');
  }
  const storedDigestValue = isRecord(statusDigests)
    ? statusDigests[statusPhase]
    : statusPhase === 'primary'
      ? record.metadata.status_request_digest
      : undefined;
  if (storedDigestValue !== undefined && typeof storedDigestValue !== 'string') {
    throw new OperationStatusValidationError('The journaled operation digest is invalid.');
  }
  const storedDigest = typeof storedDigestValue === 'string' ? storedDigestValue : undefined;
  const status = await fetchOperationStatus(
    {
      ...binding,
      requestDigest: storedDigest ?? binding.requestDigest,
    },
    fetcher,
    signal,
  );
  if (signal?.aborted) {
    throw new OperationStatusValidationError('Operation status reconciliation was cancelled.');
  }
  bindMutationStatusDigest(
    record.key,
    record.requestId,
    status.request_digest,
    statusRequestId,
    statusPhase,
  );

  if (status.state === 'pending') {
    return {state: 'pending', status, reconciled: false};
  }

  await refresh();
  if (signal?.aborted) {
    throw new OperationStatusValidationError('Operation status reconciliation was cancelled.');
  }
  if (status.state === 'succeeded' && verifySuccess && !verifySuccess(status)) {
    throw new OperationStatusValidationError('The authoritative projection did not reconcile the operation.');
  }
  const reconciled = completeOnTerminal && status.state !== 'indeterminate';
  if (reconciled) {
    completeMutation(record.key, record.requestId);
  }
  return {
    // A status may finish after the user has selected another operation or
    // after an authoritative refresh advanced the surface binding. The exact
    // journal can still be released, but callers must not apply its result to
    // the newer UI binding.
    state: isCurrent() ? status.state : 'stale',
    status,
    reconciled,
  };
}

export function operationStatusBindingFromRecord(
  record: MutationJournalRecord,
  fallback: Omit<OperationStatusBinding, 'requestId'>,
): OperationStatusBinding {
  const operationId = record.metadata.operation_id;
  const contractId = record.metadata.contract_id;
  const mapArtifactDigest = record.metadata.contract_map_digest;
  if (
    typeof operationId !== 'string'
    || typeof contractId !== 'string'
    || typeof mapArtifactDigest !== 'string'
  ) {
    throw new OperationStatusValidationError('The journaled operation binding is incomplete.');
  }
  if (
    operationId !== fallback.operationId
    || contractId !== fallback.contractId
    || mapArtifactDigest !== fallback.mapArtifactDigest
  ) {
    throw new OperationStatusValidationError('The journaled operation binding changed.');
  }
  return {
    requestId: record.requestId,
    operationId,
    contractId,
    mapArtifactDigest,
    requestDigest: typeof record.metadata.status_request_digest === 'string'
      ? record.metadata.status_request_digest
      : undefined,
  };
}
