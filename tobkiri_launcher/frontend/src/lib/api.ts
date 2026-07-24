import type {
  ApiResponse,
  PacksResponseData,
  PackApprovalResponseData,
  PackToggleResponseData,
  StartupProfilesResponseData,
  ApiStartupProfile,
  StartupProfileGraphResponseData,
  StartupProfileGraphCompilePreviewResponseData,
  StartupProfileAiInputResponseData,
  StartupProfileAiInputTracesResponseData,
  ApiAiInputConfig,
  StartupProfileCompilePreviewResponseData,
  StartupProfileMutationResponseData,
  StartupProfileDeleteResponseData,
  FlowsResponseData,
  ApiFlowDetail,
  FlowCreateResponseData,
  FlowUpdateResponseData,
  FlowDeleteResponseData,
  ApiDashboard,
  ProfileResponseData,
  ApiVersion,
  ApiUpdateTarget,
  ApiUpdateSettings,
  UpdatesResponseData,
  UpdateApplyResponseData,
  KernelRestartResponseData,
  OAuthStartResponseData,
  SetupStatusResponseData,
  HealthResponseData,
  BackgroundControlStatus,
  DesktopSystemInfo,
  CapabilityGraphsResponseData,
  CapabilityGraphResponseData,
  CapabilityGraphCompileResponseData,
  CapabilityGraphSaveResponseData,
  CapabilityNodesResponseData,
  CapabilityProfileCloneResponseData,
  CapabilityProfileNodesResponseData,
  CapabilityProfilesResponseData,
  ApiMapResponseData,
} from './apiTypes';
import {
  GetRequestCoordinator,
  RequestInvalidatedError,
  type GetRequestSnapshot,
} from './getRequestCoordinator';

// Base URL: empty string means relative path (works with Vite proxy)
const API_BASE_URL =
  (import.meta as ImportMeta & {env?: Record<string, string>}).env?.VITE_API_BASE_URL ?? '';
const PANEL_CSRF_STORAGE_KEY = 'rumi-panel-csrf';
let panelBootstrapPromise: Promise<void> | null = null;
let panelBootstrapCodeInFlight: string | null = null;
let panelSessionRecoveryPromise: Promise<boolean> | null = null;
const getRequestCoordinator = new GetRequestCoordinator();
const FOREGROUND_GET_TIMEOUT_MS = 10_000;

type TauriInvoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
type TauriWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: {
    core?: {
      invoke?: TauriInvoke;
    };
  };
};

function getStoredPanelCsrfToken(): string {
  return sessionStorage.getItem(PANEL_CSRF_STORAGE_KEY) || '';
}

function setStoredPanelCsrfToken(token: string): void {
  if (!token) {
    sessionStorage.removeItem(PANEL_CSRF_STORAGE_KEY);
    return;
  }
  sessionStorage.setItem(PANEL_CSRF_STORAGE_KEY, token);
}

function isUnsafeMethod(method: string): boolean {
  return method === 'POST' || method === 'PUT' || method === 'DELETE' || method === 'PATCH';
}

function isPanelApiPath(path: string): boolean {
  return path === '/api/panel' || path.startsWith('/api/panel/');
}

function isSetupApiPath(path: string): boolean {
  return path === '/api/setup' || path.startsWith('/api/setup/');
}

function isPanelSessionApiPath(path: string): boolean {
  return isPanelApiPath(path) || isSetupApiPath(path);
}

export function hasPendingPanelBootstrapCode(href = window.location.href): boolean {
  return new URL(href).searchParams.has('code');
}

async function exchangePanelBootstrapCode(code: string): Promise<void> {
  const url = new URL(window.location.href);
  if (!code) {
    return;
  }

  const response = await fetch(`${API_BASE_URL}/api/panel/auth/exchange`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ code }),
  });

  if (!response.ok) {
    let errorMessage = `Panel bootstrap failed: ${response.status} ${response.statusText}`;
    try {
      const errorBody: ApiResponse<unknown> = await response.json();
      if (errorBody.error) {
        errorMessage = errorBody.error;
      }
    } catch {
      // fall back to default message
    }
    throw new Error(errorMessage);
  }

  const envelope: ApiResponse<{ csrf_token: string }> = await response.json();
  if (!envelope.success || !envelope.data?.csrf_token) {
    throw new Error(envelope.error || 'Panel bootstrap failed');
  }

  setStoredPanelCsrfToken(envelope.data.csrf_token);
  // The exchange can happen inside an already coordinated foreground GET.
  // Clear cached/prefetched data without making that recovered request retry a third time.
  getRequestCoordinator.invalidate({preserveForeground: true});
  url.searchParams.delete('code');
  window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
}

function getTauriInvoke(): TauriInvoke | null {
  const maybeWindow = window as TauriWindow;
  const invoke = maybeWindow.__TAURI__?.core?.invoke;
  return typeof invoke === 'function' ? invoke : null;
}

function isLikelyTauriShell(): boolean {
  const maybeWindow = window as TauriWindow;
  return Boolean(maybeWindow.__TAURI__ || maybeWindow.__TAURI_INTERNALS__);
}

async function loadTauriInvoke(): Promise<TauriInvoke | null> {
  const globalInvoke = getTauriInvoke();
  if (globalInvoke) {
    return globalInvoke;
  }
  if (!isLikelyTauriShell()) {
    return null;
  }
  try {
    const mod = await import('@tauri-apps/api/core');
    return mod.invoke as TauriInvoke;
  } catch {
    return null;
  }
}

export function isDesktopShellAvailable(): boolean {
  return getTauriInvoke() !== null || isLikelyTauriShell();
}

export async function openExternalUrl(url: string): Promise<void> {
  const invoke = await loadTauriInvoke();
  if (invoke) {
    await invoke('open_external_url', {url});
    return;
  }

  window.location.href = url;
}

export async function sendToBackground(): Promise<void> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    throw new Error('Background control is only available in Tobkiri Launcher.');
  }

  await invoke<void>('send_to_background');
}

export async function showAppWindow(): Promise<void> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    throw new Error('Window restore is only available in Tobkiri Launcher.');
  }

  await invoke<void>('show_app_window');
}

export async function fetchBackgroundControlStatus(): Promise<BackgroundControlStatus | null> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    return null;
  }

  return invoke<BackgroundControlStatus>('get_background_control_status');
}

export async function fetchDesktopSystemInfo(): Promise<DesktopSystemInfo | null> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    return null;
  }

  return invoke<DesktopSystemInfo>('get_desktop_system_info');
}

export async function launchDefaultspackDesktop(): Promise<string> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    throw new Error('Defaultspack desktop launch is only available in Tobkiri Launcher.');
  }

  try {
    return await invoke<string>('launch_defaultspack_desktop');
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error(String(error || 'Failed to launch Defaultspack'));
  }
}

async function requestDesktopPanelBootstrapCode(): Promise<string | null> {
  const invoke = await loadTauriInvoke();
  if (!invoke) {
    return null;
  }

  return invoke<string>('reauthorize_panel_session');
}

function isRecoverablePanelAuthError(status: number, errorMessage: string): boolean {
  return status === 401 || /Unauthorized|Invalid or expired code/i.test(errorMessage);
}

async function recoverExpiredPanelSession(): Promise<boolean> {
  if (panelSessionRecoveryPromise) {
    return panelSessionRecoveryPromise;
  }

  panelSessionRecoveryPromise = (async () => {
    if (hasPendingPanelBootstrapCode()) {
      await bootstrapPanelSession();
      return true;
    }

    const code = await requestDesktopPanelBootstrapCode();
    if (!code) {
      return false;
    }

    await exchangePanelBootstrapCode(code);
    return true;
  })();

  try {
    return await panelSessionRecoveryPromise;
  } finally {
    panelSessionRecoveryPromise = null;
  }
}

export async function bootstrapPanelSession(): Promise<void> {
  const url = new URL(window.location.href);
  const code = url.searchParams.get('code');
  if (!code) {
    return;
  }

  if (panelBootstrapPromise && panelBootstrapCodeInFlight === code) {
    return panelBootstrapPromise;
  }

  panelBootstrapCodeInFlight = code;
  panelBootstrapPromise = exchangePanelBootstrapCode(code);

  try {
    await panelBootstrapPromise;
  } finally {
    panelBootstrapPromise = null;
    panelBootstrapCodeInFlight = null;
  }
}

async function ensurePanelSessionForRequest(path: string, method: string): Promise<void> {
  if (!isPanelSessionApiPath(path)) {
    return;
  }

  if (!isUnsafeMethod(method) && !hasPendingPanelBootstrapCode() && !panelBootstrapPromise) {
    return;
  }

  if (panelBootstrapPromise || hasPendingPanelBootstrapCode()) {
    await bootstrapPanelSession();
  }
}

/**
 * Common fetch wrapper for API calls.
 * - Prepends API_BASE_URL
 * - Sets JSON headers
 * - Parses {success, data, error} envelope
 * - Throws on success===false or non-ok HTTP status
 * - Returns unwrapped `data`
 */
export interface ApiRequestPolicy {
  mode?: 'foreground' | 'prefetch';
  timeoutMs?: number;
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
  requestPolicy: ApiRequestPolicy = {},
): Promise<T> {
  const url = `${API_BASE_URL}${path}`;
  const method = (options.method || 'GET').toUpperCase();

  const fetchRequest = async (
    allowPanelRecovery = true,
    signal?: AbortSignal,
  ): Promise<T> => {
    await ensurePanelSessionForRequest(path, method);

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string> | undefined),
    };

    if (isUnsafeMethod(method)) {
      const csrfToken = getStoredPanelCsrfToken();
      if (csrfToken) {
        headers['X-Rumi-CSRF'] = csrfToken;
      }
    }

    const response = await fetch(url, {
      ...options,
      method,
      credentials: 'same-origin',
      headers,
      signal: signal ?? options.signal,
    });

    if (!response.ok) {
      // Try to parse error envelope even on non-ok status
      let errorMessage = response.status === 429
        ? 'Too many requests reached the local panel. Please wait a moment and try again.'
        : `API Error: ${response.status} ${response.statusText}`;
      try {
        const errorBody: ApiResponse<unknown> = await response.json();
        if (errorBody.error) {
          errorMessage = errorBody.error;
        }
      } catch {
        // If JSON parsing fails, use the default error message
      }

      if (
        allowPanelRecovery &&
        isPanelSessionApiPath(path) &&
        isRecoverablePanelAuthError(response.status, errorMessage) &&
        await recoverExpiredPanelSession()
      ) {
        return fetchRequest(false, signal);
      }

      throw new Error(errorMessage);
    }

    const envelope: ApiResponse<T> = await response.json();

    if (!envelope.success) {
      const errorMessage = envelope.error || 'Unknown API error';
      if (
        allowPanelRecovery &&
        isPanelSessionApiPath(path) &&
        isRecoverablePanelAuthError(response.status, errorMessage) &&
        await recoverExpiredPanelSession()
      ) {
        return fetchRequest(false, signal);
      }
      throw new Error(errorMessage);
    }

    return envelope.data as T;
  };

  if (method === 'GET') {
    const mode = requestPolicy.mode ?? 'foreground';
    const timeoutMs = requestPolicy.timeoutMs ?? FOREGROUND_GET_TIMEOUT_MS;
    const executeGet = (allowInvalidationRetry: boolean): Promise<T> => (
      getRequestCoordinator.request({
        key: `${method}:${url}`,
        mode,
        timeoutMs,
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
      })
    );
    return executeGet(true);
  }

  try {
    return await fetchRequest();
  } finally {
    getRequestCoordinator.invalidate();
  }
}

export function prefetchApiGet<T>(
  path: string,
  options: { timeoutMs?: number } = {},
): Promise<T> {
  return apiFetch<T>(path, {}, {
    mode: 'prefetch',
    timeoutMs: options.timeoutMs ?? 2_500,
  });
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

// ============================================================
// Dashboard
// ============================================================

export function fetchDashboard(): Promise<ApiDashboard> {
  return apiFetch<ApiDashboard>('/api/panel/dashboard');
}

// ============================================================
// Packs
// ============================================================

export function fetchPacks(): Promise<PacksResponseData> {
  return apiFetch<PacksResponseData>('/api/panel/packs');
}

export function approvePack(id: string): Promise<PackApprovalResponseData> {
  return apiFetch<PackApprovalResponseData>(
    `/api/panel/packs/${encodeURIComponent(id)}/approve`,
    { method: 'POST' },
  );
}

export function enablePack(id: string): Promise<PackToggleResponseData> {
  return apiFetch<PackToggleResponseData>(
    `/api/panel/packs/${encodeURIComponent(id)}/enable`,
    { method: 'POST' },
  );
}

export function disablePack(id: string): Promise<PackToggleResponseData> {
  return apiFetch<PackToggleResponseData>(
    `/api/panel/packs/${encodeURIComponent(id)}/disable`,
    { method: 'POST' },
  );
}

// ============================================================
// Startup Profiles
// ============================================================

export function fetchStartupProfiles(): Promise<StartupProfilesResponseData> {
  return apiFetch<StartupProfilesResponseData>('/api/panel/startup/profiles');
}

export function createStartupProfile(
  data: {
    name?: string;
    base_pack: string;
    graph_id?: string;
    packs?: string[];
    node_overrides?: Record<string, string>;
    icon?: string | null;
  },
): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>('/api/panel/startup/profiles', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export type StartupProfileUpdatePayload = Partial<Pick<
  ApiStartupProfile,
  | 'name'
  | 'base_pack'
  | 'graph_id'
  | 'packs'
  | 'node_overrides'
  | 'icon'
  | 'default_flow'
  | 'default_graph'
  | 'system_prompt_id'
  | 'default_prompt_id'
  | 'capability_profile_id'
  | 'launch_capability_graph'
  | 'surfaces'
  | 'policy'
  | 'permissions'
  | 'enabled_nodes'
  | 'disabled_nodes'
  | 'node_settings'
  | 'metadata'
>>;

export function updateStartupProfile(
  id: string,
  data: StartupProfileUpdatePayload,
): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}`,
    {
      method: 'PUT',
      body: JSON.stringify(data),
    },
  );
}

export function deleteStartupProfile(id: string): Promise<StartupProfileDeleteResponseData> {
  return apiFetch<StartupProfileDeleteResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  );
}

export function duplicateStartupProfile(id: string): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/duplicate`,
    { method: 'POST' },
  );
}

export function activateStartupProfile(id: string): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/activate`,
    { method: 'POST' },
  );
}

export function launchStartupProfile(id: string): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/launch`,
    { method: 'POST' },
  );
}

export function compileStartupProfilePreview(
  id: string,
  profile?: ApiStartupProfile,
): Promise<StartupProfileCompilePreviewResponseData> {
  return apiFetch<StartupProfileCompilePreviewResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/compile-preview`,
    {
      method: 'POST',
      body: JSON.stringify(profile ? { profile } : {}),
    },
  );
}

export function fetchStartupProfileGraph(id: string): Promise<StartupProfileGraphResponseData> {
  return apiFetch<StartupProfileGraphResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/graph`,
  );
}

export function updateStartupProfileGraph(
  id: string,
  payload: {
    graph?: Record<string, unknown>;
    selected?: Record<string, unknown>;
  },
): Promise<StartupProfileGraphResponseData> {
  return apiFetch<StartupProfileGraphResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/graph`,
    {
      method: 'PUT',
      body: JSON.stringify(payload),
    },
  );
}

export function compileStartupProfileGraphPreview(
  id: string,
  payload: {
    graph?: Record<string, unknown>;
    selected?: Record<string, unknown>;
  },
): Promise<StartupProfileGraphCompilePreviewResponseData> {
  return apiFetch<StartupProfileGraphCompilePreviewResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/graph/compile-preview`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  );
}

export function fetchApiMap(params?: { profile_id?: string; focus?: string }): Promise<ApiMapResponseData> {
  const search = new URLSearchParams();
  if (params?.profile_id) {
    search.set('profile_id', params.profile_id);
  }
  if (params?.focus) {
    search.set('focus', params.focus);
  }
  const query = search.toString();
  return apiFetch<ApiMapResponseData>(`/api/panel/api-map${query ? `?${query}` : ''}`);
}

export function fetchStartupProfileAiInput(
  id: string,
  options?: {include_text?: boolean},
): Promise<StartupProfileAiInputResponseData> {
  const search = new URLSearchParams();
  if (options?.include_text === false) {
    search.set('include_text', 'false');
  }
  const query = search.toString();
  return apiFetch<StartupProfileAiInputResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/ai-input${query ? `?${query}` : ''}`,
  );
}

export function updateStartupProfileAiInput(
  id: string,
  aiInput: Partial<ApiAiInputConfig>,
): Promise<StartupProfileAiInputResponseData> {
  return apiFetch<StartupProfileAiInputResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/ai-input`,
    {
      method: 'PUT',
      body: JSON.stringify({ai_input: aiInput}),
    },
  );
}

export function compileStartupProfileAiInputPreview(
  id: string,
  payload: {ai_input?: Partial<ApiAiInputConfig>; message?: string},
): Promise<StartupProfileAiInputResponseData> {
  return apiFetch<StartupProfileAiInputResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/ai-input/compile-preview`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  );
}

export function fetchStartupProfileAiInputTraces(
  id: string,
): Promise<StartupProfileAiInputTracesResponseData> {
  return apiFetch<StartupProfileAiInputTracesResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/ai-input/traces`,
  );
}

export function addPackToStartupProfile(id: string, packId: string): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/packs`,
    {
      method: 'POST',
      body: JSON.stringify({ pack_id: packId }),
    },
  );
}

export function removePackFromStartupProfile(id: string, packId: string): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/packs/${encodeURIComponent(packId)}`,
    { method: 'DELETE' },
  );
}

export function setStartupProfileNodeOverride(
  id: string,
  portKey: string,
  nodeId: string,
): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/overrides`,
    {
      method: 'PUT',
      body: JSON.stringify({ port_key: portKey, node_id: nodeId }),
    },
  );
}

export function clearStartupProfileNodeOverride(id: string, portKey: string): Promise<StartupProfileMutationResponseData> {
  return apiFetch<StartupProfileMutationResponseData>(
    `/api/panel/startup/profiles/${encodeURIComponent(id)}/overrides/${encodeURIComponent(portKey)}`,
    { method: 'DELETE' },
  );
}

// ============================================================
// Flows
// ============================================================

export function fetchFlows(): Promise<FlowsResponseData> {
  return apiFetch<FlowsResponseData>('/api/panel/flows');
}

export function fetchFlowDetail(id: string): Promise<ApiFlowDetail> {
  return apiFetch<ApiFlowDetail>(
    `/api/panel/flows/${encodeURIComponent(id)}`,
  );
}

export function createFlow(
  data: { flow_id: string; yaml_content: string; filename?: string },
): Promise<FlowCreateResponseData> {
  return apiFetch<FlowCreateResponseData>('/api/panel/flows', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export function updateFlow(
  id: string,
  data: { yaml_content: string },
): Promise<FlowUpdateResponseData> {
  return apiFetch<FlowUpdateResponseData>(
    `/api/panel/flows/${encodeURIComponent(id)}`,
    {
      method: 'PUT',
      body: JSON.stringify(data),
    },
  );
}

export function deleteFlow(id: string): Promise<FlowDeleteResponseData> {
  return apiFetch<FlowDeleteResponseData>(
    `/api/panel/flows/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  );
}

// ============================================================
// Settings
// ============================================================

export function fetchProfile(): Promise<ProfileResponseData> {
  return apiFetch<ProfileResponseData>('/api/panel/settings/profile');
}

export function updateProfile(
  data: Record<string, unknown>,
): Promise<ProfileResponseData> {
  return apiFetch<ProfileResponseData>('/api/panel/settings/profile', {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

// ============================================================
// System
// ============================================================

export function fetchVersion(): Promise<ApiVersion> {
  return apiFetch<ApiVersion>('/api/panel/version');
}

export function fetchUpdates(): Promise<UpdatesResponseData> {
  return apiFetch<UpdatesResponseData>('/api/panel/updates');
}

export function fetchUpdateSettings(): Promise<ApiUpdateSettings> {
  return apiFetch<ApiUpdateSettings>('/api/panel/updates/settings');
}

export function updateUpdateSettings(
  autoUpdate: Partial<Record<ApiUpdateTarget, boolean>>,
): Promise<ApiUpdateSettings> {
  return apiFetch<ApiUpdateSettings>('/api/panel/updates/settings', {
    method: 'PUT',
    body: JSON.stringify({auto_update: autoUpdate}),
  });
}

export function applyUpdate(target: ApiUpdateTarget): Promise<UpdateApplyResponseData> {
  return apiFetch<UpdateApplyResponseData>(
    `/api/panel/updates/${encodeURIComponent(target)}/apply`,
    {
      method: 'POST',
      body: JSON.stringify({}),
    },
  );
}

export function restartKernel(): Promise<KernelRestartResponseData> {
  return apiFetch<KernelRestartResponseData>('/api/panel/kernel/restart', {
    method: 'POST',
  });
}

// ============================================================
// Setup
// ============================================================

export function fetchSetupStatus(): Promise<SetupStatusResponseData> {
  return apiFetch<SetupStatusResponseData>('/api/setup/status');
}

export function startOAuth(): Promise<OAuthStartResponseData> {
  return apiFetch<OAuthStartResponseData>('/api/setup/oauth/start');
}

// ============================================================
// Health
// ============================================================

export function checkHealth(): Promise<HealthResponseData> {
  return apiFetch<HealthResponseData>('/health');
}

// ============================================================
// Capability Graph
// ============================================================

export function fetchCapabilityNodes(): Promise<CapabilityNodesResponseData> {
  return apiFetch<CapabilityNodesResponseData>('/api/panel/nodes');
}

export function fetchCapabilityProfiles(): Promise<CapabilityProfilesResponseData> {
  return apiFetch<CapabilityProfilesResponseData>('/api/panel/profiles');
}

export function fetchCapabilityProfileNodes(
  profileId: string,
): Promise<CapabilityProfileNodesResponseData> {
  return apiFetch<CapabilityProfileNodesResponseData>(
    `/api/panel/profiles/${encodeURIComponent(profileId)}/nodes`,
  );
}

export function enableCapabilityProfileNode(
  profileId: string,
  nodeId: string,
): Promise<{ profile_id: string; node_id: string; enabled: boolean }> {
  return apiFetch(`/api/panel/profiles/${encodeURIComponent(profileId)}/nodes/${encodeURIComponent(nodeId)}/enable`, {
    method: 'POST',
  });
}

export function disableCapabilityProfileNode(
  profileId: string,
  nodeId: string,
): Promise<{ profile_id: string; node_id: string; enabled: boolean }> {
  return apiFetch(`/api/panel/profiles/${encodeURIComponent(profileId)}/nodes/${encodeURIComponent(nodeId)}/disable`, {
    method: 'POST',
  });
}

export function fetchCapabilityGraphs(): Promise<CapabilityGraphsResponseData> {
  return apiFetch<CapabilityGraphsResponseData>('/api/panel/graphs');
}

export function fetchCapabilityGraph(graphId: string): Promise<CapabilityGraphResponseData> {
  return apiFetch<CapabilityGraphResponseData>(
    `/api/panel/graphs/${encodeURIComponent(graphId)}`,
  );
}

export function validateCapabilityGraph(
  graphId: string,
  profileId: string,
  graph?: Record<string, unknown>,
): Promise<CapabilityGraphCompileResponseData> {
  return apiFetch<CapabilityGraphCompileResponseData>(
    `/api/panel/graphs/${encodeURIComponent(graphId)}/validate`,
    {
      method: 'POST',
      body: JSON.stringify({profile_id: profileId, graph}),
    },
  );
}

export function compileCapabilityGraph(
  graphId: string,
  profileId: string,
  graph?: Record<string, unknown>,
): Promise<CapabilityGraphCompileResponseData> {
  return apiFetch<CapabilityGraphCompileResponseData>(
    `/api/panel/graphs/${encodeURIComponent(graphId)}/compile`,
    {
      method: 'POST',
      body: JSON.stringify({profile_id: profileId, register: false, graph}),
    },
  );
}

export function saveCapabilityGraph(
  graph: Record<string, unknown>,
  create = false,
): Promise<CapabilityGraphSaveResponseData> {
  const graphId = String(graph.graph_id ?? '');
  if (create) {
    return apiFetch<CapabilityGraphSaveResponseData>('/api/panel/graphs', {
      method: 'POST',
      body: JSON.stringify({graph}),
    });
  }
  return apiFetch<CapabilityGraphSaveResponseData>(
    `/api/panel/graphs/${encodeURIComponent(graphId)}`,
    {
      method: 'PUT',
      body: JSON.stringify({graph}),
    },
  );
}

export function cloneCapabilityProfile(
  profileId: string,
  data: {profile_id: string; display_name?: string},
): Promise<CapabilityProfileCloneResponseData> {
  return apiFetch<CapabilityProfileCloneResponseData>(
    `/api/panel/profiles/${encodeURIComponent(profileId)}/clone`,
    {
      method: 'POST',
      body: JSON.stringify(data),
    },
  );
}
