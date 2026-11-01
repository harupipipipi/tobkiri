/**
 * Shared provider connections and model profiles for Tobkiri Launcher.
 *
 * These helpers talk to the same captured defaultspack contract routes that
 * the Defaults control panel and the Flow/Harness surfaces use, so Launcher
 * Settings reads and writes the single registry of the active Profile. No
 * Launcher-side credential copy exists anywhere in this module: the API key
 * only travels inside the Host-owned interactive-effect request and is never
 * persisted, logged, sent to telemetry, or placed in a URL.
 */

import {ApiContractError} from './apiTransport';
import {
  fetchFrontendContractOperation,
  type FrontendContractMethod,
} from './defaultspackClient';
import {loadTauriInvoke} from './desktopHost';
import {recordClientDiagnostic} from './clientDiagnostics';

/** Low-level seam so tests can drive the contract layer without HTTP. */
export type LauncherContractInvoker = <T>(
  method: FrontendContractMethod,
  target: string,
  payload?: Record<string, unknown>,
) => Promise<T>;

const defaultContractInvoker: LauncherContractInvoker = <T>(
  method: FrontendContractMethod,
  target: string,
  payload?: Record<string, unknown>,
) => fetchFrontendContractOperation<T>(method, target, payload);

/**
 * Fail-closed error whose message is always safe to show in the panel.
 * Transport details never reach it; diagnostics record only the type name.
 */
export class LauncherConnectionsError extends Error {
  /** True when the Host rejected the call for a stale activation capture. */
  readonly contextStale: boolean;

  constructor(
    message: string,
    options?: ErrorOptions & {contextStale?: boolean},
  ) {
    super(message, options);
    this.name = 'LauncherConnectionsError';
    this.contextStale = options?.contextStale === true;
  }
}

export const LAUNCHER_STALE_CONTEXT_MESSAGE =
  'The verified Profile context changed. Reopen this panel with a fresh '
  + 'verified Profile before registering again.';

/**
 * Detect the Host-owned stale-capture rejections. The contract dispatcher
 * answers `CONTRACT_MAP_STALE` when the captured activation identity no
 * longer matches the live dispatch session, and the dispatch session itself
 * fails `assert_current` once the persisted activation was replaced. Those
 * checks run against Host-captured context only — never client-sent values.
 */
export function isLauncherStaleContextError(error: unknown): boolean {
  if (error instanceof LauncherConnectionsError) return error.contextStale;
  if (error instanceof ApiContractError) {
    const data = error.data;
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      const code = (data as Record<string, unknown>).code;
      if (typeof code === 'string' && /stale/i.test(code)) return true;
    }
  }
  return (
    error instanceof Error
    && /\bstale\b|no longer matches/i.test(error.message)
  );
}

function toPanelError(error: unknown, fallback: string): LauncherConnectionsError {
  if (error instanceof LauncherConnectionsError) return error;
  if (isLauncherStaleContextError(error)) {
    return new LauncherConnectionsError(LAUNCHER_STALE_CONTEXT_MESSAGE, {
      contextStale: true,
    });
  }
  recordClientDiagnostic({
    code: 'launcher.connections.transport',
    operation: 'launcherConnections',
    error,
  });
  return new LauncherConnectionsError(fallback);
}

// ---------------------------------------------------------------------------
// Verified active-Profile scope
// ---------------------------------------------------------------------------

/**
 * Identity of the verified active Profile captured by the root runtime
 * surface. The panel uses it only as a local scope key for pending markers
 * and React lifecycle; it is never sent as request authority — the contract
 * dispatcher re-proves the Host's own captured activation on every call and
 * rejects stale captures server-side.
 */
export interface LauncherActiveProfileContext {
  readonly profile_id: string;
  readonly profile_revision: string;
  readonly catalog_revision: string;
  readonly plan_digest: string;
  readonly activation_id: string;
}

const ACTIVATION_ID_PATTERN = /^activation:[a-z0-9][a-z0-9._-]{7,127}$/;

function isNonEmptyId(value: unknown, maxLength = 255): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= maxLength;
}

/** Accept only the exact verified context shape the root surface publishes. */
export function isLauncherActiveProfileContext(
  value: unknown,
): value is LauncherActiveProfileContext {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const context = value as Record<string, unknown>;
  return (
    isNonEmptyId(context.profile_id, 128)
    && isNonEmptyId(context.profile_revision)
    && isNonEmptyId(context.catalog_revision)
    && isNonEmptyId(context.plan_digest)
    && typeof context.activation_id === 'string'
    && ACTIVATION_ID_PATTERN.test(context.activation_id)
  );
}

// ---------------------------------------------------------------------------
// Connection status snapshot (GET /api/connections/status)
// ---------------------------------------------------------------------------

const CONNECTION_CREDENTIAL_STATUSES = new Set(['configured', 'missing', 'not_required']);
const CONNECTION_HEALTH_STATUSES = new Set(['verified', 'unverified']);
const CONNECTION_REACHABILITY_STATUSES = new Set(['available', 'unavailable', 'unknown']);

export interface LauncherProviderConnectionStatus {
  readonly provider_instance_id: string;
  readonly display_name: string;
  readonly enabled: boolean;
  readonly credential_status: 'configured' | 'missing' | 'not_required';
  readonly health_status: 'verified' | 'unverified';
  readonly reachability: 'available' | 'unavailable' | 'unknown';
  readonly observed_at: number | null;
}

export interface LauncherConnectionsSnapshot {
  readonly revision: number;
  readonly providers: readonly LauncherProviderConnectionStatus[];
}

const CONNECTION_STATUS_KEYS = [
  'provider_instance_id',
  'display_name',
  'enabled',
  'credential_status',
  'health_status',
  'reachability',
  'observed_at',
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Strictly validate one provider entry: exactly the seven published keys. */
export function isLauncherProviderConnectionStatus(
  value: unknown,
): value is LauncherProviderConnectionStatus {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  if (keys.length !== CONNECTION_STATUS_KEYS.length) return false;
  if (!CONNECTION_STATUS_KEYS.every((key) => keys.includes(key))) return false;
  return (
    isNonEmptyId(value.provider_instance_id)
    && typeof value.display_name === 'string'
    && typeof value.enabled === 'boolean'
    && typeof value.credential_status === 'string'
    && CONNECTION_CREDENTIAL_STATUSES.has(value.credential_status)
    && typeof value.health_status === 'string'
    && CONNECTION_HEALTH_STATUSES.has(value.health_status)
    && typeof value.reachability === 'string'
    && CONNECTION_REACHABILITY_STATUSES.has(value.reachability)
    && (typeof value.observed_at === 'number' || value.observed_at === null)
  );
}

/** The status route publishes exactly {revision, providers}; nothing else. */
export function isLauncherConnectionsSnapshot(
  value: unknown,
): value is LauncherConnectionsSnapshot {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes('revision') || !keys.includes('providers')) {
    return false;
  }
  return (
    typeof value.revision === 'number'
    && Number.isInteger(value.revision)
    && value.revision >= 0
    && Array.isArray(value.providers)
    && value.providers.every(isLauncherProviderConnectionStatus)
  );
}

/** Read the shared provider-connection registry of the active Profile. */
export async function fetchLauncherConnectionsSnapshot(
  invoker: LauncherContractInvoker = defaultContractInvoker,
): Promise<LauncherConnectionsSnapshot> {
  const data = await invoker<unknown>('GET', '/api/connections/status');
  if (!isLauncherConnectionsSnapshot(data)) {
    throw new LauncherConnectionsError('Tobkiri returned an invalid connections status.');
  }
  return {revision: data.revision, providers: [...data.providers]};
}

// ---------------------------------------------------------------------------
// Model profile list + narrow idempotent create (GET/POST /api/ai/profiles)
// ---------------------------------------------------------------------------

export interface LauncherModelProfile {
  readonly profile_id: string;
  readonly display_name: string;
  readonly model_id?: string;
  readonly provider_id?: string;
  readonly route_configured?: boolean;
}

export interface LauncherModelProfileList {
  readonly profiles: readonly LauncherModelProfile[];
  readonly count: number;
  readonly registry_revision: number;
}

/** The list route publishes exactly {profiles, count, registry_revision}. */
export function isLauncherModelProfileList(
  value: unknown,
): value is LauncherModelProfileList {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  if (
    keys.length !== 3
    || !keys.includes('profiles')
    || !keys.includes('count')
    || !keys.includes('registry_revision')
  ) {
    return false;
  }
  if (
    typeof value.count !== 'number'
    || !Number.isInteger(value.count)
    || value.count < 0
    || typeof value.registry_revision !== 'number'
    || !Number.isInteger(value.registry_revision)
    || value.registry_revision < 0
    || !Array.isArray(value.profiles)
  ) {
    return false;
  }
  return value.profiles.every((profile) => {
    if (!isRecord(profile)) return false;
    if (!isNonEmptyId(profile.profile_id) || !isNonEmptyId(profile.display_name)) {
      return false;
    }
    if (profile.model_id !== undefined && typeof profile.model_id !== 'string') return false;
    if (profile.provider_id !== undefined && typeof profile.provider_id !== 'string') {
      return false;
    }
    if (
      profile.route_configured !== undefined
      && typeof profile.route_configured !== 'boolean'
    ) {
      return false;
    }
    return true;
  });
}

/** Read the shared model-profile registry of the active Profile. */
export async function fetchLauncherModelProfiles(
  invoker: LauncherContractInvoker = defaultContractInvoker,
): Promise<LauncherModelProfileList> {
  const data = await invoker<unknown>('GET', '/api/ai/profiles');
  if (!isLauncherModelProfileList(data)) {
    throw new LauncherConnectionsError('Tobkiri returned an invalid model profile list.');
  }
  return {
    profiles: [...data.profiles],
    count: data.count,
    registry_revision: data.registry_revision,
  };
}

// ---------------------------------------------------------------------------
// Provider connection setup (POST /api/ai/provider-key interactive effect)
// ---------------------------------------------------------------------------

export const LOCAL_OPENAI_COMPATIBLE_PROTOCOL = 'local-openai-compatible' as const;

export type LauncherProviderProtocol =
  | 'openai-compatible'
  | 'anthropic'
  | typeof LOCAL_OPENAI_COMPATIBLE_PROTOCOL;

export type LauncherProviderSetupPreset = Readonly<{
  endpoint: string;
  protocol: LauncherProviderProtocol;
}>;

/**
 * Small UI-facing projection of the public provider catalog, mirroring the
 * Defaults setup presets. A custom endpoint still has to be chosen explicitly
 * and is never guessed.
 */
export const LAUNCHER_CUSTOM_PROVIDER_ID = 'openai_compatible';

const OPENAI_COMPATIBLE = 'openai-compatible' as const;

export const LAUNCHER_PROVIDER_SETUP_PRESETS: Readonly<
  Record<string, LauncherProviderSetupPreset>
> = {
  anthropic: {endpoint: 'https://api.anthropic.com', protocol: 'anthropic'},
  openai: {endpoint: 'https://api.openai.com/v1', protocol: OPENAI_COMPATIBLE},
  google: {
    endpoint: 'https://generativelanguage.googleapis.com/v1beta/openai',
    protocol: OPENAI_COMPATIBLE,
  },
  openrouter: {endpoint: 'https://openrouter.ai/api/v1', protocol: OPENAI_COMPATIBLE},
  cerebras: {endpoint: 'https://api.cerebras.ai/v1', protocol: OPENAI_COMPATIBLE},
  deepseek: {endpoint: 'https://api.deepseek.com/v1', protocol: OPENAI_COMPATIBLE},
  deepinfra: {endpoint: 'https://api.deepinfra.com/v1/openai', protocol: OPENAI_COMPATIBLE},
  fireworks: {endpoint: 'https://api.fireworks.ai/inference/v1', protocol: OPENAI_COMPATIBLE},
  glm: {endpoint: 'https://api.z.ai/api/paas/v4', protocol: OPENAI_COMPATIBLE},
  groq: {endpoint: 'https://api.groq.com/openai/v1', protocol: OPENAI_COMPATIBLE},
  hyperbolic: {endpoint: 'https://api.hyperbolic.xyz/v1', protocol: OPENAI_COMPATIBLE},
  mistral: {endpoint: 'https://api.mistral.ai/v1', protocol: OPENAI_COMPATIBLE},
  moonshotai: {endpoint: 'https://api.moonshot.ai/v1', protocol: OPENAI_COMPATIBLE},
  nebius: {endpoint: 'https://api.studio.nebius.ai/v1', protocol: OPENAI_COMPATIBLE},
  novita: {endpoint: 'https://api.novita.ai/openai/v1', protocol: OPENAI_COMPATIBLE},
  nvidia: {endpoint: 'https://integrate.api.nvidia.com/v1', protocol: OPENAI_COMPATIBLE},
  perplexity: {endpoint: 'https://api.perplexity.ai/v1', protocol: OPENAI_COMPATIBLE},
  sambanova: {endpoint: 'https://api.sambanova.ai/v1', protocol: OPENAI_COMPATIBLE},
  together: {endpoint: 'https://api.together.xyz/v1', protocol: OPENAI_COMPATIBLE},
  xai: {endpoint: 'https://api.x.ai/v1', protocol: OPENAI_COMPATIBLE},
};

export function launcherProviderSetupPreset(
  providerId: string,
): LauncherProviderSetupPreset | null {
  return LAUNCHER_PROVIDER_SETUP_PRESETS[providerId.trim().toLowerCase()] ?? null;
}

/** Connection names become provider_instance_id = "provider." + name. */
const CONNECTION_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const PROFILE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/;
const LOCAL_ENDPOINT_PATTERN = /^http:\/\/(?:127\.0\.0\.1|\[::1\]):(\d{1,5})\/v1\/?$/;
const MAX_ENDPOINT_LENGTH = 2048;
const MAX_KEY_LENGTH = 16384;

/**
 * Credential capability scopes the Host may freeze into the prepare plan.
 * `ai.generate`/`ai.stream` are the legacy text-only defaults; the two audio
 * scopes exist only for hosted OpenAI-compatible connections and require an
 * explicit operator grant.
 */
export type LauncherProviderCapability =
  | 'ai.generate'
  | 'ai.stream'
  | 'ai.audio.transcribe'
  | 'ai.audio.speech';

export const LAUNCHER_PROVIDER_CAPABILITIES: readonly LauncherProviderCapability[] = [
  'ai.generate',
  'ai.stream',
  'ai.audio.transcribe',
  'ai.audio.speech',
];

export const LAUNCHER_AUDIO_CAPABILITIES: readonly LauncherProviderCapability[] = [
  'ai.audio.transcribe',
  'ai.audio.speech',
];

/** The exact request the provider_configure interactive effect accepts. */
export interface LauncherProviderConfigureRequest {
  readonly connection_name: string;
  readonly protocol: LauncherProviderProtocol;
  readonly endpoint: string;
  readonly key_value: string;
  /**
   * Present only when the operator explicitly granted scopes. When absent
   * the Host keeps its legacy text-only behavior; it is never sent empty.
   */
  readonly capabilities?: readonly LauncherProviderCapability[];
}

export function isLocalOpenAICompatibleProtocol(
  protocol: LauncherProviderProtocol | undefined,
): boolean {
  return protocol === LOCAL_OPENAI_COMPATIBLE_PROTOCOL;
}

/**
 * Validate the setup form against the same fail-closed rules the Host
 * applies, so obvious mistakes are rejected before an effect is created.
 * Returns a panel-safe message or null when the request is well-formed.
 */
export function validateLauncherProviderSetup(input: {
  providerId: string;
  apiId: string;
  endpoint?: string;
  protocol?: LauncherProviderProtocol;
  key: string;
  capabilities?: readonly LauncherProviderCapability[];
}): {request: LauncherProviderConfigureRequest} | {error: string} {
  const providerId = input.providerId.trim().toLowerCase();
  const apiId = input.apiId.trim() || 'default';
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(providerId)) {
    return {error: 'Choose a provider before registering a connection.'};
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(apiId)) {
    return {error: 'The connection suffix may only use letters, digits, dots, dashes, and underscores.'};
  }
  const connection = `${providerId}.${apiId}`;
  if (!CONNECTION_NAME_PATTERN.test(connection)) {
    return {error: 'The connection name is invalid.'};
  }
  const preset = launcherProviderSetupPreset(providerId);
  const protocol = input.protocol ?? preset?.protocol;
  if (!protocol) {
    return {error: 'Choose a protocol for this connection.'};
  }
  if (
    protocol !== 'openai-compatible'
    && protocol !== 'anthropic'
    && protocol !== LOCAL_OPENAI_COMPATIBLE_PROTOCOL
  ) {
    return {error: 'The connection protocol is unsupported.'};
  }
  const endpoint = (input.endpoint?.trim() || preset?.endpoint || '');
  if (!endpoint) {
    return {error: 'Enter the HTTPS endpoint for this connection.'};
  }
  if (endpoint.length > MAX_ENDPOINT_LENGTH || /\s/.test(endpoint)) {
    return {error: 'The endpoint must not contain spaces or exceed 2048 characters.'};
  }
  let parsed: URL;
  try {
    parsed = new URL(endpoint);
  } catch {
    return {error: 'The endpoint is not a valid URL.'};
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    return {error: 'The endpoint must not contain credentials, a query, or a fragment.'};
  }
  if (protocol === LOCAL_OPENAI_COMPATIBLE_PROTOCOL) {
    const match = LOCAL_ENDPOINT_PATTERN.exec(endpoint);
    const port = match ? Number.parseInt(match[1], 10) : NaN;
    if (!match || !Number.isInteger(port) || port < 1024 || port > 65535) {
      return {
        error: 'A local keyless connection requires an endpoint of the form '
          + 'http://127.0.0.1:PORT/v1 (or http://[::1]:PORT/v1) with a port between 1024 and 65535.',
      };
    }
    if (input.key !== '') {
      return {error: 'Local keyless connections must be saved with an empty API key field.'};
    }
  } else {
    if (parsed.protocol !== 'https:') {
      return {error: 'Hosted connections require an HTTPS endpoint.'};
    }
    if (input.key.length === 0) {
      return {error: 'Enter the API key for this connection.'};
    }
    if (input.key.length > MAX_KEY_LENGTH || /[\x00-\x1f\x7f]/.test(input.key)) {
      return {error: 'The API key contains unsupported characters or is too long.'};
    }
    if (endpoint.includes(input.key) || connection.includes(input.key)) {
      return {error: 'The API key must not appear inside the endpoint or connection name.'};
    }
  }
  let capabilities: readonly LauncherProviderCapability[] | undefined;
  if (input.capabilities !== undefined) {
    const granted = new Set(input.capabilities);
    if (
      input.capabilities.length === 0
      || granted.size !== input.capabilities.length
      || input.capabilities.some(
        (item) => !LAUNCHER_PROVIDER_CAPABILITIES.includes(item),
      )
      || !granted.has('ai.generate')
      || !granted.has('ai.stream')
    ) {
      return {error: 'The capability grant must list the text scopes and any explicitly selected audio scopes.'};
    }
    if (
      input.capabilities.some((item) => LAUNCHER_AUDIO_CAPABILITIES.includes(item))
      && protocol !== 'openai-compatible'
    ) {
      return {error: 'Audio capabilities are only available for hosted OpenAI-compatible connections.'};
    }
    // Deterministic catalog order so the frozen plan and its digest are stable.
    capabilities = LAUNCHER_PROVIDER_CAPABILITIES.filter((item) => granted.has(item));
  }
  return {
    request: {
      connection_name: connection,
      protocol,
      endpoint,
      key_value: input.key,
      ...(capabilities ? {capabilities} : {}),
    },
  };
}

// ---------------------------------------------------------------------------
// Interactive-effect status shapes (Host-owned; validated fail-closed)
// ---------------------------------------------------------------------------

export interface LauncherProviderEffectStatus {
  readonly effect_id: string;
  readonly approval_request_id: string | null;
  readonly state: string;
}

export function isLauncherProviderEffectStatus(
  value: unknown,
): value is LauncherProviderEffectStatus {
  return (
    isRecord(value)
    && typeof value.effect_id === 'string'
    && value.effect_id.length > 0
    && value.effect_id.length <= 255
    && (value.approval_request_id === null
      || (typeof value.approval_request_id === 'string'
        && value.approval_request_id.length <= 255))
    && typeof value.state === 'string'
  );
}

export interface LauncherApprovalStatus {
  readonly request_id: string;
  readonly state: string;
}

export function isLauncherApprovalStatus(value: unknown): value is LauncherApprovalStatus {
  return (
    isRecord(value)
    && typeof value.request_id === 'string'
    && value.request_id.length > 0
    && typeof value.state === 'string'
  );
}

// ---------------------------------------------------------------------------
// Approval helpers (Host-owned approval window; no client-side signing)
// ---------------------------------------------------------------------------

/**
 * Read one interactive-approval request by its exact id. Only the id is
 * sent — the panel never supplies approval decisions itself.
 */
export async function fetchLauncherApprovalStatus(
  requestId: string,
  invoker: LauncherContractInvoker = defaultContractInvoker,
): Promise<LauncherApprovalStatus> {
  const data = await invoker<unknown>(
    'POST', '/api/interactive-approval/v1/get', {request_id: requestId},
  );
  if (!isLauncherApprovalStatus(data)) {
    throw new LauncherConnectionsError('Tobkiri returned an invalid approval status.');
  }
  return {request_id: data.request_id, state: data.state};
}

/**
 * Ask the Host (via the desktop shell) to open the approval window for one
 * request id. Falls back to the captured approval-window route. Returns
 * whether an approval surface was confirmed open.
 */
export async function openLauncherApprovalWindow(
  requestId: string,
  invoker: LauncherContractInvoker = defaultContractInvoker,
): Promise<boolean> {
  const invoke = await loadTauriInvoke();
  if (invoke) {
    try {
      await invoke('open_authority_approval_window', {requestId});
      return true;
    } catch (error) {
      recordClientDiagnostic({
        code: 'launcher.connections.approval_window',
        operation: 'open_authority_approval_window',
        error,
      });
    }
  }
  try {
    const data = await invoker<unknown>(
      'POST', '/api/authority/approval-window', {request_id: requestId},
    );
    return isRecord(data) && data.opened === true;
  } catch (error) {
    recordClientDiagnostic({
      code: 'launcher.connections.approval_window',
      operation: 'api/authority/approval-window',
      error,
    });
    return false;
  }
}

// ---------------------------------------------------------------------------
// Model profile narrow create
// ---------------------------------------------------------------------------

export interface LauncherModelProfileInput {
  readonly model_profile_id: string;
  readonly model_id: string;
  readonly provider_instance_id: string;
  readonly display_name: string;
}

/**
 * Validate the model-registration form against the same rules the Host
 * normalizer applies before any request is sent.
 */
export function validateLauncherModelProfileInput(
  input: LauncherModelProfileInput,
): {error: string} | null {
  if (!PROFILE_ID_PATTERN.test(input.model_profile_id)) {
    return {error: 'The registration id may only use letters, digits, dots, dashes, slashes, colons, and underscores (max 256 characters).'};
  }
  if (!PROFILE_ID_PATTERN.test(input.model_id)) {
    return {error: 'The model id may only use letters, digits, dots, dashes, slashes, colons, and underscores (max 256 characters).'};
  }
  if (!PROFILE_ID_PATTERN.test(input.provider_instance_id)) {
    return {error: 'Choose a registered connection for this model.'};
  }
  const name = input.display_name.trim();
  if (!name || name.length > 200) {
    return {error: 'Enter a display name (1-200 characters).'};
  }
  if (input.display_name !== name) {
    return {error: 'The display name must not have leading or trailing whitespace.'};
  }
  return null;
}

function modelProfilesMatch(
  saved: LauncherModelProfile,
  input: LauncherModelProfileInput,
): boolean {
  return (
    saved.profile_id === input.model_profile_id
    && saved.model_id === input.model_id
    && saved.provider_id === input.provider_instance_id
    && saved.display_name === input.display_name
  );
}

/**
 * Create one model registration through the narrow idempotent save route.
 *
 * Both registry revisions are re-read on every submit so the request always
 * carries current state, and the bound provider connection must still exist
 * and be enabled. An explicit second save of identical input is the only
 * retry path; it is safe because the backend save is idempotent for an
 * identical record (it preserves rich and disabled profiles untouched). A
 * revision conflict surfaces as a panel-safe error and is never auto-retried.
 */
export async function saveLauncherModelProfile(
  input: LauncherModelProfileInput,
  invoker: LauncherContractInvoker = defaultContractInvoker,
): Promise<LauncherModelProfile> {
  const invalid = validateLauncherModelProfileInput(input);
  if (invalid) throw new LauncherConnectionsError(invalid.error);
  const [connections, current] = await Promise.all([
    fetchLauncherConnectionsSnapshot(invoker),
    fetchLauncherModelProfiles(invoker),
  ]);
  const provider = connections.providers.find(
    (item) => item.provider_instance_id === input.provider_instance_id,
  );
  if (!provider) {
    throw new LauncherConnectionsError('The selected connection is not registered.');
  }
  if (!provider.enabled) {
    throw new LauncherConnectionsError('The selected connection is disabled.');
  }
  const existing = current.profiles.find(
    (profile) => profile.profile_id === input.model_profile_id,
  );
  if (existing && !modelProfilesMatch(existing, input)) {
    throw new LauncherConnectionsError(
      'A model registration with this id already exists with different details.',
    );
  }
  const saved = await invoker<unknown>('POST', '/api/ai/profiles', {
    model_profile_id: input.model_profile_id,
    model_id: input.model_id,
    provider_instance_id: input.provider_instance_id,
    display_name: input.display_name,
    expected_revision: current.registry_revision,
    provider_registry_revision: connections.revision,
  });
  if (!isLauncherModelProfileList(saved) || saved.profiles.length !== 1) {
    throw new LauncherConnectionsError(
      'The saved model registration could not be confirmed; check the list instead of retrying.',
    );
  }
  const profile = saved.profiles[0];
  if (!modelProfilesMatch(profile, input)) {
    throw new LauncherConnectionsError(
      'The saved model registration did not match; check the list instead of retrying.',
    );
  }
  return profile;
}

export {toPanelError};
