import type {
  ApiDashboard, ApiDynamicFrontendCatalog, FrontendCapabilityInvocation,
  KernelRestartResponseData, PackApprovalResponseData, PackInstallResponseData,
  PackToggleResponseData, PacksResponseData,
} from './apiTypes';
import {
  generatedRouteFor,
  VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP,
} from './generatedFrontendContractMap';
import {parsePacksResponse} from './packScope';
import {createApiClient, type ApiRequestPolicy} from './apiTransport';

export type FrontendContractMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

function isFrontendContractMethod(method: string): method is FrontendContractMethod {
  return method === 'GET' || method === 'POST' || method === 'PUT' || method === 'DELETE';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

interface ParsedFrontendContractPath {
  method: FrontendContractMethod;
  route: ReturnType<typeof generatedRouteFor>;
}

function parseFrontendContractPath(path: string): ParsedFrontendContractPath | null {
  const match = /^\/api\/contracts\/defaultspack\/([^/?#]+)(?:\?([^#]*))?$/.exec(path);
  if (!match) return null;
  let operation: string;
  try {
    operation = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const separator = operation.indexOf(' ');
  if (separator <= 0) return null;
  const method = operation.slice(0, separator);
  const target = operation.slice(separator + 1);
  if (!isFrontendContractMethod(method)) return null;
  let route;
  try {
    route = generatedRouteFor(
      VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP,
      method,
      target,
    );
  } catch {
    return null;
  }
  const query = match[2];
  if (query !== undefined) {
    if (!query || method !== 'GET' || route.targets.length !== 1) return null;
    const allowedKeys = new Set(route.targets[0].allowed_payload_keys);
    const params = new URLSearchParams(query);
    const seen = new Set<string>();
    for (const [key, value] of params.entries()) {
      if (!allowedKeys.has(key) || seen.has(key) || !value) return null;
      seen.add(key);
    }
    if (seen.size === 0) return null;
  }
  return {method, route};
}

/** This optional Application client admits only its digest-pinned operation map. */
export const defaultspackApiFetch = createApiClient((path, method) => {
  const contract = parseFrontendContractPath(path);
  if (!contract || method !== contract.method) return null;
  return {panelSession: true, runtimeDispatch: true, requestIdentity: true};
});

function frontendContractPath(method: FrontendContractMethod, target: string): string {
  if (!isFrontendContractMethod(method)) {
    throw new Error('The generated v4 contract method is unsupported.');
  }
  try {
    generatedRouteFor(
      VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP,
      method,
      target,
    );
  } catch {
    throw new Error(`The logical target is not declared by the verified frontend Contract Map: ${method} ${target}`);
  }
  const operation = `${method.toUpperCase()} ${target}`;
  return `/api/contracts/defaultspack/${encodeURIComponent(operation)}`;
}

function assertLogicalContractTarget(method: FrontendContractMethod, target: string): void {
  if (
    !target.startsWith('/api/')
    || target.startsWith('/api/contracts/')
    || target.includes('..')
    || target.includes('//')
    || target.includes('\\')
    || target.includes('?')
    || target.includes('#')
  ) {
    throw new Error('The generated v4 contract target is invalid.');
  }
  if (!isFrontendContractMethod(method)) {
    throw new Error('The generated v4 contract method is unsupported.');
  }
}

/**
 * Call one target supplied by the digest-pinned v4 frontend contract map.
 *
 * The target is intentionally injected by the generated map; this helper
 * does not discover routes, synthesize function paths, or provide a legacy
 * HTTP fallback.
 */
export function fetchFrontendContractOperation<T>(
  method: FrontendContractMethod,
  target: string,
  payload?: Record<string, unknown>,
  requestPolicy: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<T> {
  assertLogicalContractTarget(method, target);
  const route = generatedRouteFor(
    VERIFIED_GENERATED_FRONTEND_CONTRACT_MAP,
    method,
    target,
  );
  if (route.targets.length !== 1) {
    throw new Error('The generated v4 target has multiple operations; select an exact operation binding first.');
  }
  const allowedKeys = new Set(route.targets[0].allowed_payload_keys);
  if (payload && Object.keys(payload).some((key) => !allowedKeys.has(key))) {
    throw new Error('The generated v4 contract payload contains an unknown key.');
  }
  const query = method === 'GET' && payload && Object.keys(payload).length > 0
    ? `?${new URLSearchParams(
      Object.entries(payload).map(([key, value]) => [key, String(value)]),
    ).toString()}`
    : '';
  const path = frontendContractPath(method, target);
  return defaultspackApiFetch<T>(query ? `${path}${query}` : path, method !== 'GET'
    ? {method, body: JSON.stringify(payload ?? {})}
    : {}, requestPolicy);
}

/** Read one authenticated, server-owned mutation outcome by stable request ID. */
export function fetchRuntimeOperationStatus(requestId: string): Promise<unknown> {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(requestId)) {
    throw new Error('The operation status request identity is invalid.');
  }
  return fetchFrontendContractOperation<unknown>(
    'GET',
    '/api/runtime-surface/operation-status',
    {request_id: requestId},
  );
}

export function fetchDashboard(): Promise<ApiDashboard> {
  return defaultspackApiFetch<ApiDashboard>(frontendContractPath('GET', '/api/home/dashboard'));
}

export async function fetchFrontendCatalog(): Promise<ApiDynamicFrontendCatalog> {
  const data = await defaultspackApiFetch<{dynamic_host?: ApiDynamicFrontendCatalog | null}>(
    frontendContractPath('GET', '/api/ui/catalog'),
    {cache: 'no-store'},
  );
  if (!data.dynamic_host) {
    throw new Error('Tobkiri dynamic frontend catalog is unavailable.');
  }
  return data.dynamic_host;
}

export function invokeFrontendCapability(
  request: FrontendCapabilityInvocation,
  requestOptions: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<unknown> {
  const requestId = requestOptions.requestId ?? crypto.randomUUID();
  return defaultspackApiFetch<unknown>(frontendContractPath('POST', '/api/ui/capability/invoke'), {
    method: 'POST',
    body: JSON.stringify({
      request_id: requestId,
      expires_at: Date.now() / 1000 + 30,
      profile_id: request.profileId,
      profile_revision: request.profileRevision,
      activation_id: request.activationId,
      plan_hash: request.planHash,
      catalog_hash: request.catalogHash,
      contribution_id: request.contributionId,
      owner_pack_id: request.ownerPackId,
      contract_id: request.contractId,
      payload: request.payload,
    }),
  }, {...requestOptions, requestId});
}

function dispatchPackControl<T>(
  operationId: string,
  payload: Record<string, unknown> = {},
  requestOptions: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<T> {
  const targets: Record<string, string> = {
    'approval.approve': '/api/pack-control/approval-approve',
    'approval.candidate': '/api/pack-control/approval-candidate',
    'approval.revoke': '/api/pack-control/approval-revoke',
    'pack.disable': '/api/pack-control/disable',
    'pack.enable': '/api/pack-control/enable',
    'pack.install': '/api/pack-control/install',
    'runtime.restart': '/api/pack-control/restart',
  };
  if (operationId === 'catalog.read') {
    return defaultspackApiFetch<T>(frontendContractPath('GET', '/api/pack-control/catalog'));
  }
  const target = targets[operationId];
  if (!target) throw new Error(`Unselected Pack control operation: ${operationId}`);
  return defaultspackApiFetch<T>(frontendContractPath('POST', target), {
    method: 'POST',
    body: JSON.stringify(payload),
  }, requestOptions);
}

function packControlString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Tobkiri returned an invalid Pack approval ${field}.`);
  }
  return value;
}

function validatePackApprovalCandidate(value: unknown, packId: string): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Tobkiri returned an invalid Pack approval candidate.');
  }
  const candidate = value as Record<string, unknown>;
  const candidateId = packControlString(candidate.candidate_id, 'candidate id');
  if (candidate.pack_id !== packId) {
    throw new Error('Tobkiri returned a Pack approval candidate for a different Pack.');
  }
  if (
    typeof candidate.snapshot_digest !== 'string'
    || !/^sha256:[0-9a-f]{64}$/.test(candidate.snapshot_digest)
  ) {
    throw new Error('Tobkiri returned a Pack approval candidate without an exact snapshot digest.');
  }
  return candidateId;
}

function validatePackApprovalResponse(value: unknown, packId: string): PackApprovalResponseData {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Tobkiri returned an invalid Pack approval response.');
  }
  const response = value as Record<string, unknown>;
  if (
    response.pack_id !== packId
    || response.approved !== true
    || response.approval_status !== 'approved'
    || (response.enabled !== undefined && typeof response.enabled !== 'boolean')
  ) {
    throw new Error('Tobkiri did not confirm approval for the requested Pack.');
  }
  for (const field of ['profile_id', 'workspace_id', 'profile_revision', 'plan_digest', 'catalog_revision']) {
    packControlString(response[field], field);
  }
  return response as unknown as PackApprovalResponseData;
}

export function fetchPacks(): Promise<PacksResponseData> {
  return dispatchPackControl<unknown>('catalog.read').then(parsePacksResponse);
}

export async function installPack(
  id: string,
  requestOptions: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<PackInstallResponseData> {
  return dispatchPackControl('pack.install', {pack_id: id}, requestOptions);
}

export interface PackApprovalRequestIds {
  candidateRequestId?: string;
  approvalRequestId?: string;
  timeoutMs?: number;
}

export async function approvePack(
  id: string,
  requestIds: PackApprovalRequestIds = {},
): Promise<PackApprovalResponseData> {
  const candidate = await dispatchPackControl<unknown>(
    'approval.candidate',
    {pack_id: id},
    {requestId: requestIds.candidateRequestId, timeoutMs: requestIds.timeoutMs},
  );
  const candidateId = validatePackApprovalCandidate(candidate, id);
  const response = await dispatchPackControl<unknown>('approval.approve', {
    pack_id: id,
    candidate_id: candidateId,
  }, {requestId: requestIds.approvalRequestId, timeoutMs: requestIds.timeoutMs});
  return validatePackApprovalResponse(response, id);
}

export function enablePack(
  id: string,
  requestOptions: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<PackToggleResponseData> {
  return dispatchPackControl<PackToggleResponseData>('pack.enable', {pack_id: id}, requestOptions);
}

export function disablePack(
  id: string,
  requestOptions: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<PackToggleResponseData> {
  return dispatchPackControl<PackToggleResponseData>('pack.disable', {pack_id: id}, requestOptions);
}

export function revokePackApproval(
  id: string,
  requestOptions: Pick<ApiRequestPolicy, 'requestId' | 'timeoutMs'> = {},
): Promise<PackApprovalResponseData> {
  return dispatchPackControl<PackApprovalResponseData>('approval.revoke', {pack_id: id}, requestOptions);
}

export function restartKernel(): Promise<KernelRestartResponseData> {
  return dispatchPackControl<KernelRestartResponseData>('runtime.restart');
}
