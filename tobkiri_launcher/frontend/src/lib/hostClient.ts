import type {
  ApiPackVMConsent, ApiPackVMDoctor, ApiPackVMOperation, ApiPackVMProvisioningPlan,
  HealthResponseData, RuntimeStatus,
} from './apiTypes';
import {
  normalizePackVMConsent,
  normalizePackVMDoctor,
  normalizePackVMOperation,
  normalizePackVMPlan,
} from './packvmLifecycle';
import {
  parseNamedProfileRegistry, validateNamedProfileMutation,
  type CreateNamedProfileInput, type DeleteNamedProfileInput,
  type DuplicateNamedProfileInput, type NamedProfileMutationInput,
  type NamedProfileRecord, type NamedProfileRegistry, type UpdateNamedProfileInput,
} from './profileRegistry';
export type {
  CreateNamedProfileInput, DeleteNamedProfileInput, DuplicateNamedProfileInput,
  NamedProfileMutationInput, NamedProfileRecord, NamedProfileRegistry, UpdateNamedProfileInput,
} from './profileRegistry';
import {createApiClient, type ApiRequestPolicy} from './apiTransport';

const PANEL_AUTH_EXCHANGE_PATH = '/api/panel/auth/exchange';
const EXACT_HOST_API_ROUTES = [
  {method: 'POST', path: PANEL_AUTH_EXCHANGE_PATH},
  {method: 'GET', path: '/api/setup/packs'},
  {method: 'POST', path: '/api/setup/packs/install'},
  {method: 'GET', path: '/api/setup/packs?include_source_additions=true'},
  {method: 'POST', path: '/api/setup/packs/install?include_source_additions=true'},
  {method: 'POST', path: '/api/setup/runtime/reconcile'},
  {method: 'GET', path: '/api/v4/profiles'},
  {method: 'POST', path: '/api/v4/profiles/create'},
  {method: 'POST', path: '/api/v4/profiles/update'},
  {method: 'POST', path: '/api/v4/profiles/duplicate'},
  {method: 'POST', path: '/api/v4/profiles/delete'},
  {method: 'POST', path: '/api/v4/packvm/prepare'},
  {method: 'POST', path: '/api/v4/packvm/consent'},
  {method: 'POST', path: '/api/v4/packvm/provision'},
  {method: 'POST', path: '/api/v4/packvm/cancel'},
  {method: 'GET', path: '/api/v4/packvm/doctor'},
  {method: 'POST', path: '/api/v4/packvm/stop'},
  {method: 'POST', path: '/api/v4/packvm/cleanup'},
  {method: 'GET', path: '/health'},
] as const satisfies ReadonlyArray<{
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  path: string;
}>;
const EXACT_PACKVM_LIFECYCLE_PATHS = new Set([
  '/api/v4/packvm/prepare',
  '/api/v4/packvm/consent',
  '/api/v4/packvm/provision',
  '/api/v4/packvm/cancel',
  '/api/v4/packvm/doctor',
  '/api/v4/packvm/stop',
  '/api/v4/packvm/cleanup',
]);

const RUNTIME_STATUSES = new Set<RuntimeStatus>([
  'starting',
  'panel_ready',
  'runtime_ready',
  'profile_reconfirmation_required',
  'error',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredBoolean(value: Record<string, unknown>, field: string): boolean {
  if (typeof value[field] !== 'boolean') {
    throw new Error(`Health response field ${field} is invalid.`);
  }
  return value[field] as boolean;
}

function requiredRuntimeError(value: Record<string, unknown>): string | null {
  if (value.runtime_error !== null && typeof value.runtime_error !== 'string') {
    throw new Error('Health response field runtime_error is invalid.');
  }
  if (typeof value.runtime_error === 'string' && !value.runtime_error.trim()) {
    throw new Error('Health response field runtime_error is empty.');
  }
  return value.runtime_error as string | null;
}

function assertCoherentHealth(health: HealthResponseData): void {
  const hasRuntimeError = health.runtime_error !== null;
  const coherent = (() => {
    switch (health.runtime_status) {
      case 'starting':
        return health.status === 'ok'
          && !health.panel_ready
          && !health.runtime_ready
          && !hasRuntimeError;
      case 'panel_ready':
        return health.status === 'ok'
          && health.panel_ready
          && !health.runtime_ready
          && !hasRuntimeError;
      case 'profile_reconfirmation_required':
        return health.status === 'ok'
          && health.needs_setup
          && health.panel_ready
          && !health.runtime_ready
          && hasRuntimeError;
      case 'runtime_ready':
        return health.status === 'ok'
          && !health.needs_setup
          && health.panel_ready
          && health.runtime_ready
          && !hasRuntimeError;
      case 'error':
        return health.status === 'error'
          && health.panel_ready
          && !health.runtime_ready
          && hasRuntimeError;
      default:
        return false;
    }
  })();
  if (!coherent) throw new Error('Health response contains contradictory readiness state.');
}

/** Parse the finite Host health contract before it reaches runtime state. */
export function parseHealthResponse(value: unknown): HealthResponseData {
  if (!isRecord(value)) throw new Error('Health response is not an object.');
  if (value.status !== 'ok' && value.status !== 'error') {
    throw new Error('Health response status is invalid.');
  }
  if (
    typeof value.runtime_status !== 'string'
    || !RUNTIME_STATUSES.has(value.runtime_status as RuntimeStatus)
  ) {
    throw new Error('Health response runtime_status is invalid.');
  }
  const health: HealthResponseData = {
    status: value.status,
    needs_setup: requiredBoolean(value, 'needs_setup'),
    panel_ready: requiredBoolean(value, 'panel_ready'),
    runtime_ready: requiredBoolean(value, 'runtime_ready'),
    runtime_status: value.runtime_status as RuntimeStatus,
    runtime_error: requiredRuntimeError(value),
    host_catalog_verified: requiredBoolean(value, 'host_catalog_verified'),
    profile_ceremony_available: requiredBoolean(value, 'profile_ceremony_available'),
    active_profile_ready: requiredBoolean(value, 'active_profile_ready'),
    launch_ready: requiredBoolean(value, 'launch_ready'),
    defaults_bootstrap_required: requiredBoolean(value, 'defaults_bootstrap_required'),
  };
  assertCoherentHealth(health);
  return health;
}

function isSetupApiPath(path: string): boolean {
  return path === '/api/setup/packs' || path === '/api/setup/packs/install'
    || path === '/api/setup/packs?include_source_additions=true'
    || path === '/api/setup/packs/install?include_source_additions=true';
}

function exactNonMapMethodForPath(path: string): 'GET' | 'POST' | 'PUT' | 'DELETE' | null {
  const route = EXACT_HOST_API_ROUTES.find((candidate) => candidate.path === path);
  return route?.method ?? null;
}

function isPackVMProgressPath(path: string): boolean {
  const separator = path.indexOf('?');
  if (separator <= 0 || path.slice(0, separator) !== '/api/v4/packvm/progress') {
    return false;
  }
  const query = path.slice(separator + 1);
  const match = /^operation_id=([^&#=]+)$/.exec(query);
  if (!match) return false;
  let operationId: string;
  try {
    operationId = decodeURIComponent(match[1]);
  } catch {
    return false;
  }
  return operationId.length > 0 && encodeURIComponent(operationId) === match[1];
}

function isPackVMLifecyclePath(path: string): boolean {
  return EXACT_PACKVM_LIFECYCLE_PATHS.has(path) || isPackVMProgressPath(path);
}

/** Host operations remain usable without loading any Application contract map. */
export const hostApiFetch = createApiClient((path, method) => {
  const expectedMethod = exactNonMapMethodForPath(path) ?? (isPackVMProgressPath(path) ? 'GET' : null);
  if (method !== expectedMethod) return null;
  const lifecycle = isPackVMLifecyclePath(path);
  return {
    panelSession: isSetupApiPath(path) || path === '/api/v4/profiles'
      || path.startsWith('/api/v4/profiles/') || lifecycle,
    runtimeDispatch: lifecycle,
    requestIdentity: lifecycle && method !== 'GET',
  };
});

export function fetchNamedProfiles(): Promise<NamedProfileRegistry> {
  return hostApiFetch<unknown>('/api/v4/profiles', {cache: 'no-store'}).then(parseNamedProfileRegistry);
}

function mutateNamedProfile(
  action: 'create' | 'update' | 'duplicate' | 'delete',
  payload: NamedProfileMutationInput,
): Promise<NamedProfileRegistry> {
  const validated = validateNamedProfileMutation(action, payload);
  return hostApiFetch<unknown>(`/api/v4/profiles/${action}`, {
    method: 'POST',
    body: JSON.stringify(validated),
  }).then(parseNamedProfileRegistry);
}

export const createNamedProfile = (payload: CreateNamedProfileInput) => (
  mutateNamedProfile('create', payload)
);
export const updateNamedProfile = (payload: UpdateNamedProfileInput) => (
  mutateNamedProfile('update', payload)
);
export const duplicateNamedProfile = (payload: DuplicateNamedProfileInput) => (
  mutateNamedProfile('duplicate', payload)
);
export const deleteNamedProfile = (payload: DeleteNamedProfileInput) => (
  mutateNamedProfile('delete', payload)
);

const PACKVM_API_ROOT = '/api/v4/packvm';

function packVMLifecyclePost<T>(
  operation: string,
  payload: Record<string, unknown>,
): Promise<T> {
  return hostApiFetch<T>(`${PACKVM_API_ROOT}/${operation}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function preparePackVM(): Promise<ApiPackVMProvisioningPlan> {
  return packVMLifecyclePost<unknown>('prepare', {}).then(normalizePackVMPlan);
}

export function consentPackVM(
  payload: {
    plan_digest: string;
    ceremony_nonce: string;
    confirmation: string;
    approve_image_download: boolean;
    previous_attestation_digest?: string;
    storage_rebind_digest?: string;
  },
): Promise<ApiPackVMConsent> {
  return packVMLifecyclePost<unknown>('consent', payload).then(normalizePackVMConsent);
}

export function provisionPackVM(
  payload: {consent_id: string; operation_id: string},
): Promise<ApiPackVMOperation> {
  return packVMLifecyclePost<unknown>('provision', payload).then(normalizePackVMOperation);
}

export function fetchPackVMProgress(operationId: string): Promise<ApiPackVMOperation> {
  return hostApiFetch<unknown>(
    `${PACKVM_API_ROOT}/progress?operation_id=${encodeURIComponent(operationId)}`,
  ).then(normalizePackVMOperation);
}

export function cancelPackVM(operationId: string): Promise<ApiPackVMOperation> {
  return packVMLifecyclePost<unknown>('cancel', {operation_id: operationId})
    .then(normalizePackVMOperation);
}

export function fetchPackVMDoctor(): Promise<ApiPackVMDoctor> {
  // Verifying the pinned multi-GiB image can exceed the ordinary UI GET budget.
  // Keep a bounded, read-only deadline; never replay provisioning on timeout.
  return hostApiFetch<unknown>(`${PACKVM_API_ROOT}/doctor`, {}, {timeoutMs: 60_000})
    .then(normalizePackVMDoctor);
}

export function stopPackVM(confirmation: string): Promise<ApiPackVMDoctor> {
  return packVMLifecyclePost<unknown>('stop', {confirmation}).then(normalizePackVMDoctor);
}

export function cleanupPackVM(
  confirmation: string,
  operationId: string,
  sourceOperationId: string | null,
): Promise<ApiPackVMOperation> {
  return packVMLifecyclePost<unknown>('cleanup', {
    confirmation,
    operation_id: operationId,
    source_operation_id: sourceOperationId,
  }).then(normalizePackVMOperation);
}

export function checkHealth(): Promise<HealthResponseData> {
  return hostApiFetch<unknown>('/health').then(parseHealthResponse);
}

export async function reconcileDefaultsRuntime(): Promise<void> {
  await hostApiFetch<unknown>('/api/setup/runtime/reconcile', {method: 'POST'});
}
