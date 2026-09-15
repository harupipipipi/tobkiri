import type {ApiResponse} from './apiTypes';
import {
  GetRequestCoordinator,
  RequestInvalidatedError,
  type GetRequestSnapshot,
} from './getRequestCoordinator';
import {recordClientDiagnostic} from './clientDiagnostics';
import {assertRuntimeDispatchAllowed} from './runtimeDispatchGate';
import {
  getBrowserStorage,
  readSafeStorageValue,
  removeSafeStorageValue,
  writeSafeStorageValue,
} from './safeStorage';
import {loadTauriInvoke} from './desktopHost';

const API_BASE_URL =
  (import.meta as ImportMeta & {env?: Record<string, string>}).env?.VITE_API_BASE_URL ?? '';
const PANEL_CSRF_STORAGE_KEY = 'rumi-panel-csrf';
const PANEL_AUTH_EXCHANGE_PATH = '/api/panel/auth/exchange';

let panelBootstrapPromise: Promise<void> | null = null;
let panelBootstrapCodeInFlight: string | null = null;
let panelSessionRecoveryPromise: Promise<boolean> | null = null;
const getRequestCoordinator = new GetRequestCoordinator();
const FOREGROUND_GET_TIMEOUT_MS = 10_000;
const MUTATION_TIMEOUT_MS = 10_000;

export class ApiRequestTimeoutError extends Error {
  constructor(method: string, path: string, timeoutMs: number) {
    super(`${method} request timed out after ${timeoutMs}ms: ${path}`);
    this.name = 'ApiRequestTimeoutError';
  }
}

export class ApiContractError extends Error {
  readonly data: unknown;

  constructor(message: string, data: unknown) {
    super(message);
    this.name = 'ApiContractError';
    this.data = data;
  }
}

function getStoredPanelCsrfToken(): string {
  return readSafeStorageValue(
    getBrowserStorage('session'),
    PANEL_CSRF_STORAGE_KEY,
  ) || '';
}

function setStoredPanelCsrfToken(token: string): void {
  if (!token) {
    removeSafeStorageValue(getBrowserStorage('session'), PANEL_CSRF_STORAGE_KEY);
    return;
  }
  writeSafeStorageValue(getBrowserStorage('session'), PANEL_CSRF_STORAGE_KEY, token);
}

function isUnsafeMethod(method: string): boolean {
  return method === 'POST' || method === 'PUT' || method === 'DELETE' || method === 'PATCH';
}

export function hasPendingPanelBootstrapCode(href = window.location.href): boolean {
  return new URL(href).searchParams.has('code');
}

async function exchangePanelBootstrapCode(
  code: string,
  currentRequestSignal?: AbortSignal,
): Promise<void> {
  const url = new URL(window.location.href);
  if (!code) return;

  const response = await fetch(`${API_BASE_URL}${PANEL_AUTH_EXCHANGE_PATH}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code}),
  });

  if (!response.ok) {
    let errorMessage = `Panel bootstrap failed: ${response.status} ${response.statusText}`;
    try {
      const errorBody: ApiResponse<unknown> = await response.json();
      if (errorBody.error) errorMessage = errorBody.error;
    } catch (error) {
      recordClientDiagnostic({
        code: 'panel.bootstrap.error_envelope_unavailable',
        operation: 'panel.bootstrap.exchange',
        error,
      });
    }
    throw new Error(errorMessage);
  }

  const envelope: ApiResponse<{csrf_token: string}> = await response.json();
  if (!envelope.success || !envelope.data?.csrf_token) {
    throw new Error(envelope.error || 'Panel bootstrap failed');
  }

  setStoredPanelCsrfToken(envelope.data.csrf_token);
  getRequestCoordinator.invalidate({preserveSignal: currentRequestSignal});
  url.searchParams.delete('code');
  window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
}

async function requestDesktopPanelBootstrapCode(): Promise<string | null> {
  const invoke = await loadTauriInvoke();
  return invoke ? invoke<string>('reauthorize_panel_session') : null;
}

function isRecoverablePanelAuthError(status: number, errorMessage: string): boolean {
  return status === 401 || /Unauthorized|Invalid or expired code/i.test(errorMessage);
}

async function recoverExpiredPanelSession(currentRequestSignal?: AbortSignal): Promise<boolean> {
  if (panelSessionRecoveryPromise) return panelSessionRecoveryPromise;

  panelSessionRecoveryPromise = (async () => {
    if (hasPendingPanelBootstrapCode()) {
      await bootstrapPanelSession(currentRequestSignal);
      return true;
    }

    const code = await requestDesktopPanelBootstrapCode();
    if (!code) return false;
    await exchangePanelBootstrapCode(code, currentRequestSignal);
    return true;
  })();

  try {
    return await panelSessionRecoveryPromise;
  } finally {
    panelSessionRecoveryPromise = null;
  }
}

export async function bootstrapPanelSession(currentRequestSignal?: AbortSignal): Promise<void> {
  const url = new URL(window.location.href);
  const code = url.searchParams.get('code');
  if (!code) return;

  if (panelBootstrapPromise && panelBootstrapCodeInFlight === code) {
    return panelBootstrapPromise;
  }

  panelBootstrapCodeInFlight = code;
  panelBootstrapPromise = exchangePanelBootstrapCode(code, currentRequestSignal);
  try {
    await panelBootstrapPromise;
  } finally {
    panelBootstrapPromise = null;
    panelBootstrapCodeInFlight = null;
  }
}

async function ensurePanelSessionForRequest(
  requiresPanelSession: boolean,
  method: string,
  currentRequestSignal?: AbortSignal,
): Promise<void> {
  if (!requiresPanelSession) return;
  if (!isUnsafeMethod(method) && !hasPendingPanelBootstrapCode() && !panelBootstrapPromise) return;
  if (panelBootstrapPromise || hasPendingPanelBootstrapCode()) {
    await bootstrapPanelSession(currentRequestSignal);
  }
}

export interface ApiRequestPolicy {
  mode?: 'foreground' | 'prefetch';
  timeoutMs?: number;
  /** Stable identity for one logical unsafe request. */
  requestId?: string;
}

/** Classification is supplied by a finite Host or Application route resolver. */
export interface ApiRoutePolicy {
  readonly panelSession: boolean;
  readonly runtimeDispatch: boolean;
  readonly requestIdentity: boolean;
}

/** Construct a client that rejects every request outside its route resolver. */
export function createApiClient(
  classifyRequest: (path: string, method: string) => ApiRoutePolicy | null,
) {
  return async function apiFetch<T>(
    path: string,
    options: RequestInit = {},
    requestPolicy: ApiRequestPolicy = {},
  ): Promise<T> {
    const url = `${API_BASE_URL}${path}`;
    const method = (options.method || 'GET').toUpperCase();
    const routePolicy = classifyRequest(path, method);
    if (!routePolicy) {
      throw new Error(`The frontend request is not in the exact method/path allowlist: ${method} ${path}`);
    }
    if (routePolicy.runtimeDispatch) {
      assertRuntimeDispatchAllowed(method, path);
    }

    const fetchRequest = async (
      allowPanelRecovery = true,
      signal?: AbortSignal,
    ): Promise<T> => {
      await ensurePanelSessionForRequest(routePolicy.panelSession, method, signal);

      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(options.headers as Record<string, string> | undefined),
      };
      if (routePolicy.requestIdentity) {
        headers['X-Tobkiri-Request-ID'] = requestPolicy.requestId ?? crypto.randomUUID();
      }
      if (isUnsafeMethod(method)) {
        const csrfToken = getStoredPanelCsrfToken();
        if (csrfToken) headers['X-Rumi-CSRF'] = csrfToken;
      }

      const response = await fetch(url, {
        ...options,
        method,
        credentials: 'same-origin',
        headers,
        signal: signal ?? options.signal,
      });

      if (!response.ok) {
        let errorMessage = response.status === 429
          ? 'Too many requests reached the local panel. Please wait a moment and try again.'
          : `API Error: ${response.status} ${response.statusText}`;
        let errorData: unknown = null;
        try {
          const errorBody: ApiResponse<unknown> = await response.json();
          if (errorBody.error) errorMessage = errorBody.error;
          errorData = errorBody.data;
        } catch (error) {
          recordClientDiagnostic({
            code: 'api.error_envelope_unavailable',
            operation: `${method} ${path}`,
            error,
          });
        }
        if (
          allowPanelRecovery &&
          routePolicy.panelSession &&
          isRecoverablePanelAuthError(response.status, errorMessage) &&
          await recoverExpiredPanelSession(signal)
        ) {
          return fetchRequest(false, signal);
        }
        throw new ApiContractError(errorMessage, errorData);
      }

      const envelope: ApiResponse<T> = await response.json();
      if (!envelope.success) {
        const errorMessage = envelope.error || 'Unknown API error';
        if (
          allowPanelRecovery &&
          routePolicy.panelSession &&
          isRecoverablePanelAuthError(response.status, errorMessage) &&
          await recoverExpiredPanelSession(signal)
        ) {
          return fetchRequest(false, signal);
        }
        throw new ApiContractError(errorMessage, envelope.data);
      }
      return envelope.data as T;
    };

    if (method === 'GET') {
      const mode = requestPolicy.mode ?? 'foreground';
      const timeoutMs = requestPolicy.timeoutMs ?? FOREGROUND_GET_TIMEOUT_MS;
      const deadline = Number.isFinite(timeoutMs) && timeoutMs > 0 ? Date.now() + timeoutMs : null;
      const executeGet = (allowInvalidationRetry: boolean): Promise<T> => {
        const remaining = deadline === null ? timeoutMs : deadline - Date.now();
        if (deadline !== null && remaining <= 0) {
          return Promise.reject(new ApiRequestTimeoutError(method, path, timeoutMs));
        }
        return getRequestCoordinator.request({
          key: `${method}:${url}`,
          mode,
          timeoutMs: remaining,
          factory: (signal) => fetchRequest(true, signal),
        }).catch((error) => {
          if (
            allowInvalidationRetry &&
            mode === 'foreground' &&
            error instanceof RequestInvalidatedError
          ) {
            return executeGet(false);
          }
          throw error;
        });
      };
      return executeGet(true);
    }

    try {
      const timeoutMs = requestPolicy.timeoutMs ?? MUTATION_TIMEOUT_MS;
      const controller = new AbortController();
      let timeout: ReturnType<typeof globalThis.setTimeout> | undefined;
      const externalSignal = options.signal;
      const abortFromExternalSignal = () => {
        controller.abort(externalSignal?.reason);
      };

      if (externalSignal) {
        if (externalSignal.aborted) {
          abortFromExternalSignal();
        } else {
          externalSignal.addEventListener('abort', abortFromExternalSignal, {once: true});
        }
      }

      const request = fetchRequest(true, controller.signal);
      const timeoutPromise = Number.isFinite(timeoutMs) && timeoutMs > 0
        ? new Promise<never>((_resolve, reject) => {
          timeout = globalThis.setTimeout(() => {
            const error = new ApiRequestTimeoutError(method, path, timeoutMs);
            controller.abort(error);
            reject(error);
          }, timeoutMs);
        })
        : null;

      try {
        return await (timeoutPromise ? Promise.race([request, timeoutPromise]) : request);
      } finally {
        if (timeout) globalThis.clearTimeout(timeout);
        externalSignal?.removeEventListener('abort', abortFromExternalSignal);
      }
    } finally {
      getRequestCoordinator.invalidate();
    }
  };
}

export function clearApiPrefetchCache(): void {
  getRequestCoordinator.invalidate();
}

export function invalidateApiGetCache(): void {
  getRequestCoordinator.invalidate();
}

export function getApiRequestCacheSnapshot(): GetRequestSnapshot {
  return getRequestCoordinator.snapshot();
}
