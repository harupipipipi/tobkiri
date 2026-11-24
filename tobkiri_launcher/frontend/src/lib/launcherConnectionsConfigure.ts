/**
 * Provider-connection registration for Tobkiri Launcher.
 *
 * This is a scoped port of the Defaults control panel's
 * `configureProvider` flow. It preserves every safety property of that
 * flow while binding the pending marker to the exact verified Profile and
 * activation the caller supplies:
 *
 * - A correlation id is generated once per pending attempt and persisted
 *   before `prepare` is sent, so a lost prepare response is reconciled by
 *   `lookup` — the request (and especially the API key) is never replayed.
 * - Only bounded non-secret data is persisted: connection name, opaque
 *   effect id, request digest, and correlation id. The key value never
 *   reaches storage, logs, telemetry, or a URL.
 * - The pending marker lives in session storage under a key derived from
 *   `profile_id` + `activation_id`, so it can never be resumed under a
 *   different Profile or a stale activation. The Host additionally verifies
 *   the full captured context on every phase.
 * - Approval is Host-owned: the panel only reads the approval state and
 *   asks the Host to open its approval window. The browser never signs or
 *   submits an approval decision.
 * - Terminal failure states and denied/expired approvals surface as
 *   panel-safe errors; nothing auto-retries.
 */

import {getBrowserStorage, type SafeStorage} from './safeStorage';
import {fetchFrontendContractOperation} from './defaultspackClient';
import {
  fetchLauncherApprovalStatus,
  fetchLauncherConnectionsSnapshot,
  fetchLauncherModelProfiles,
  isLauncherProviderEffectStatus,
  isLauncherStaleContextError,
  LAUNCHER_STALE_CONTEXT_MESSAGE,
  LauncherConnectionsError,
  openLauncherApprovalWindow,
  saveLauncherModelProfile,
  type LauncherActiveProfileContext,
  type LauncherApprovalStatus,
  type LauncherConnectionsSnapshot,
  type LauncherContractInvoker,
  type LauncherModelProfile,
  type LauncherModelProfileInput,
  type LauncherModelProfileList,
  type LauncherProviderConfigureRequest,
  type LauncherProviderEffectStatus,
} from './launcherConnections';

const PROVIDER_CONFIGURE_EFFECT_KIND = 'provider_configure';
const PROVIDER_KEY_TARGET = '/api/ai/provider-key';
const PENDING_STORAGE_PREFIX =
  'tobkiri-launcher-provider-configuration-pending-v1:';
const MAX_POLL_ATTEMPTS = 300;

/** Pending marker shape — bounded non-secret data only. */
export interface LauncherProviderPendingRecord {
  readonly v: 1;
  readonly connection: string;
  readonly effect: string | null;
  readonly digest: string;
  readonly correlation?: string;
}

function isPendingRecord(value: unknown): value is LauncherProviderPendingRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return (
    record.v === 1
    && typeof record.connection === 'string'
    && record.connection.length > 0
    && record.connection.length <= 128
    && (record.effect === null
      || (typeof record.effect === 'string' && record.effect.length <= 255))
    && typeof record.digest === 'string'
    && /^[0-9a-f]{64}$/.test(record.digest)
    && (record.correlation === undefined
      || (typeof record.correlation === 'string'
        && record.correlation.length <= 64))
  );
}

function pendingStorageKey(scope: LauncherActiveProfileContext): string {
  return `${PENDING_STORAGE_PREFIX}${scope.profile_id}:${scope.activation_id}`;
}

/**
 * Read the pending marker for this exact Profile+activation. A malformed
 * record is discarded (never trusted, never migrated).
 */
export function readLauncherProviderPending(
  storage: SafeStorage | null,
  scope: LauncherActiveProfileContext,
): LauncherProviderPendingRecord | null {
  if (!storage) return null;
  let raw: string | null;
  try {
    raw = storage.getItem(pendingStorageKey(scope));
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    return isPendingRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writePending(
  storage: SafeStorage,
  scope: LauncherActiveProfileContext,
  pending: LauncherProviderPendingRecord,
): void {
  storage.setItem(pendingStorageKey(scope), JSON.stringify(pending));
}

function clearPending(
  storage: SafeStorage,
  scope: LauncherActiveProfileContext,
): void {
  storage.removeItem(pendingStorageKey(scope));
}

async function requestDigest(
  request: LauncherProviderConfigureRequest,
): Promise<string> {
  const digestBytes = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(JSON.stringify(request)),
  );
  return Array.from(
    new Uint8Array(digestBytes),
    (byte) => byte.toString(16).padStart(2, '0'),
  ).join('');
}

/** Pluggable Host boundary for the registration flow (defaults below). */
export interface LauncherProviderConfigurePorts {
  readonly storage: SafeStorage;
  readonly prepare: (
    request: LauncherProviderConfigureRequest,
    correlation: string,
  ) => Promise<LauncherProviderEffectStatus>;
  readonly lookup: (correlation: string) => Promise<LauncherProviderEffectStatus>;
  readonly status: (effect: string) => Promise<LauncherProviderEffectStatus>;
  readonly resume: (effect: string) => Promise<LauncherProviderEffectStatus>;
  readonly cancel: (effect: string) => Promise<LauncherProviderEffectStatus>;
  readonly approval: (id: string) => Promise<LauncherApprovalStatus>;
  readonly openApproval: (id: string) => Promise<boolean>;
  readonly pause: () => Promise<void>;
  /** Optional observer for panel progress; receives redacted statuses only. */
  readonly onStatus?: (status: LauncherProviderEffectStatus) => void;
}

const busyScopes = new Set<string>();

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) {
    throw new LauncherConnectionsError(
      'Registration confirmation was interrupted. Save the same connection '
        + 'again to check its state.',
    );
  }
}

/**
 * Complete or reconcile one connection registration for the verified active
 * Profile, never replaying an uncertain prepare.
 *
 * The promise resolves only when the Host reports the effect `succeeded`.
 * Every failure throws a `LauncherConnectionsError` with a panel-safe
 * message; the pending marker stays available for the next explicit save of
 * the same input under the same Profile and activation.
 */
export async function configureLauncherProviderConnection(
  request: LauncherProviderConfigureRequest,
  scope: LauncherActiveProfileContext,
  ports: LauncherProviderConfigurePorts,
  options: {signal?: AbortSignal} = {},
): Promise<void> {
  const scopeKey = pendingStorageKey(scope);
  if (busyScopes.has(scopeKey)) {
    throw new LauncherConnectionsError(
      'A connection registration is already in progress for this Profile. '
        + 'Check the approval window.',
    );
  }
  busyScopes.add(scopeKey);
  const signal = options.signal;
  try {
    throwIfAborted(signal);
    const digest = await requestDigest(request);
    let storedRecord: LauncherProviderPendingRecord | null = null;
    const storedRaw = ports.storage.getItem(scopeKey);
    if (storedRaw) {
      let candidate: unknown;
      try {
        candidate = JSON.parse(storedRaw);
      } catch {
        candidate = null;
      }
      if (!isPendingRecord(candidate)) {
        throw new LauncherConnectionsError(
          'A previous registration attempt is recorded but unreadable. '
            + 'Do not resend the key; check the Launcher approvals first.',
        );
      }
      storedRecord = candidate;
    }
    const pending: {
      connection: string;
      effect: string | null;
      digest: string;
      correlation?: string;
    } = storedRecord
      ? {
        connection: storedRecord.connection,
        effect: storedRecord.effect,
        digest: storedRecord.digest,
        correlation: storedRecord.correlation,
      }
      : {
        connection: request.connection_name,
        effect: null,
        digest,
        correlation: crypto.randomUUID(),
      };
    throwIfAborted(signal);
    if (storedRecord && !pending.effect && typeof pending.correlation === 'string') {
      // Only the saved non-secret correlation is sent, never the key or input.
      const receipt = await ports.lookup(pending.correlation);
      if (typeof receipt.effect_id !== 'string' || !receipt.effect_id) {
        throw new LauncherConnectionsError(
          'The registration receipt could not be confirmed. Do not resend.',
        );
      }
      pending.effect = receipt.effect_id;
      writePending(ports.storage, scope, {...pending, v: 1});
    }
    if (pending.connection !== request.connection_name || pending.digest !== digest) {
      if (typeof pending.effect === 'string' && pending.effect) {
        // A changed key must not prevent reading the previous operation's
        // result. Never treat that result as success for the new input or
        // replay either key.
        const previous = await ports.status(pending.effect);
        if (previous.effect_id !== pending.effect) {
          throw new LauncherConnectionsError(
            'The registration operation id did not match.',
          );
        }
        if (['succeeded', 'cancelled'].includes(previous.state)) {
          clearPending(ports.storage, scope);
          throw new LauncherConnectionsError(
            'The previous registration was confirmed as completed or cancelled. '
              + 'The changed input was not saved; review it and save again.',
          );
        }
      }
      throw new LauncherConnectionsError(
        'A previous registration is still unconfirmed. Save the identical '
          + 'input again to check its state.',
      );
    }
    if (storedRecord && (typeof pending.effect !== 'string' || !pending.effect)) {
      throw new LauncherConnectionsError(
        'The registration receipt could not be confirmed. Do not resend; '
          + 'check the Launcher approvals first.',
      );
    }
    let status: LauncherProviderEffectStatus;
    if (storedRecord) {
      status = await ports.status(pending.effect!);
    } else {
      // Written before sending: response loss must not silently create
      // another effect. Only correlation and a request digest are persisted,
      // never the key or URL.
      writePending(ports.storage, scope, {...pending, v: 1});
      throwIfAborted(signal);
      status = await ports.prepare(request, pending.correlation!);
      if (typeof status.effect_id !== 'string' || !status.effect_id) {
        throw new LauncherConnectionsError(
          'The registration receipt could not be confirmed. Do not resend.',
        );
      }
      pending.effect = status.effect_id;
      writePending(ports.storage, scope, {...pending, v: 1});
    }
    ports.onStatus?.(status);
    let opened = false;
    let resumed = false;
    for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt += 1) {
      throwIfAborted(signal);
      if (status.effect_id !== pending.effect) {
        throw new LauncherConnectionsError(
          'The registration operation id did not match.',
        );
      }
      if (status.state === 'succeeded') {
        clearPending(ports.storage, scope);
        return;
      }
      if (['failed', 'ambiguous', 'stale', 'cancelled'].includes(status.state)) {
        if (status.state === 'cancelled') clearPending(ports.storage, scope);
        // Keep the id for inspection, including failures that may have
        // written a key.
        throw new LauncherConnectionsError(
          `The connection registration did not complete (${status.state}). `
            + `Check operation ${pending.effect}.`,
        );
      }
      if (
        !['prepared', 'approval_pending', 'approved', 'claimed', 'dispatched']
          .includes(status.state)
      ) {
        throw new LauncherConnectionsError(
          'The registration state could not be confirmed. Do not resend.',
        );
      }
      if (
        !resumed
        && ['prepared', 'approval_pending', 'approved'].includes(status.state)
      ) {
        const id = status.approval_request_id;
        if (!id) {
          throw new LauncherConnectionsError(
            'The registration approval id is missing.',
          );
        }
        const approval = await ports.approval(id);
        if (approval.request_id !== id) {
          throw new LauncherConnectionsError(
            'The registration approval id did not match.',
          );
        }
        if (approval.state === 'approved') {
          resumed = true;
          status = await ports.resume(pending.effect!);
          ports.onStatus?.(status);
          continue;
        }
        if (
          ['denied', 'expired', 'cancelled', 'rejected'].includes(approval.state)
        ) {
          const cancelled = await ports.cancel(pending.effect!);
          if (
            cancelled.effect_id !== pending.effect
            || cancelled.state !== 'cancelled'
          ) {
            throw new LauncherConnectionsError(
              'The registration cancellation could not be confirmed. '
                + 'The operation id is preserved.',
            );
          }
          clearPending(ports.storage, scope);
          throw new LauncherConnectionsError(
            'The connection registration was not approved. Nothing was saved.',
          );
        }
        if (!opened) {
          opened = await ports.openApproval(id);
          if (!opened) {
            throw new LauncherConnectionsError(
              'Open the Tobkiri Launcher approvals to continue, then save the '
                + 'same connection again to resume.',
            );
          }
        }
      }
      await ports.pause();
      throwIfAborted(signal);
      status = await ports.status(pending.effect!);
      ports.onStatus?.(status);
    }
    throw new LauncherConnectionsError(
      'The registration is still awaiting confirmation. Save the same '
        + 'connection again to resume checking its state.',
    );
  } catch (error) {
    // Transport exceptions may contain request details. Never propagate those.
    if (error instanceof LauncherConnectionsError) throw error;
    if (isLauncherStaleContextError(error)) {
      // The Host rejected the call for a stale activation capture; surface
      // that distinctly so the panel can invalidate its view.
      throw new LauncherConnectionsError(LAUNCHER_STALE_CONTEXT_MESSAGE, {
        contextStale: true,
      });
    }
    throw new LauncherConnectionsError(
      'The registration result could not be confirmed. Save the same '
        + 'connection again to check its state. The key is never resent '
        + 'automatically.',
    );
  } finally {
    busyScopes.delete(scopeKey);
  }
}

function parseEffectStatus(
  value: unknown,
  options: {requireEffectId?: boolean} = {},
): LauncherProviderEffectStatus {
  const requireEffectId = options.requireEffectId !== false;
  if (isLauncherProviderEffectStatus(value)) {
    return {
      effect_id: value.effect_id,
      approval_request_id: value.approval_request_id,
      state: value.state,
    };
  }
  if (
    !requireEffectId
    && value
    && typeof value === 'object'
    && !Array.isArray(value)
  ) {
    // A lookup miss may resolve to a receipt without an effect id; the caller
    // decides how to treat the empty marker. All other phases stay strict.
    const record = value as Record<string, unknown>;
    return {
      effect_id: typeof record.effect_id === 'string' ? record.effect_id : '',
      approval_request_id:
        typeof record.approval_request_id === 'string'
          ? record.approval_request_id
          : null,
      state: typeof record.state === 'string' ? record.state : '',
    };
  }
  throw new LauncherConnectionsError(
    'Tobkiri returned an invalid registration status.',
  );
}

const configureDefaultInvoker: LauncherContractInvoker = <T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  target: string,
  payload?: Record<string, unknown>,
) => fetchFrontendContractOperation<T>(method, target, payload);

/**
 * Build the default Host boundary: the captured provider-key contract route
 * for every effect phase, the interactive-approval read, and the Host-owned
 * approval window. Session storage is required because the pending marker is
 * what makes lost responses reconcilable without replaying the key.
 */
export function createLauncherProviderConfigurePorts(
  invoker: LauncherContractInvoker = configureDefaultInvoker,
  extras: {
    pause?: () => Promise<void>;
    onStatus?: (status: LauncherProviderEffectStatus) => void;
  } = {},
): LauncherProviderConfigurePorts {
  const storage = getBrowserStorage('session');
  if (!storage) {
    throw new LauncherConnectionsError(
      'Session storage is unavailable, so a pending registration marker '
        + 'cannot be kept safely. Registration is not attempted.',
    );
  }
  const postEffect = async (
    payload: Record<string, unknown>,
    options: {requireEffectId?: boolean} = {},
  ): Promise<LauncherProviderEffectStatus> => parseEffectStatus(
    await invoker<unknown>('POST', PROVIDER_KEY_TARGET, payload),
    options,
  );
  return {
    storage,
    prepare: (request, correlation) => postEffect({
      phase: 'prepare',
      effect_kind: PROVIDER_CONFIGURE_EFFECT_KIND,
      request,
      correlation_id: correlation,
    }),
    lookup: (correlation) => postEffect(
      {
        phase: 'lookup',
        effect_kind: PROVIDER_CONFIGURE_EFFECT_KIND,
        correlation_id: correlation,
      },
      {requireEffectId: false},
    ),
    status: (effect) => postEffect({phase: 'status', effect_id: effect}),
    resume: (effect) => postEffect({phase: 'resume', effect_id: effect}),
    cancel: (effect) => postEffect({phase: 'cancel', effect_id: effect}),
    approval: (id) => fetchLauncherApprovalStatus(id, invoker),
    openApproval: (id) => openLauncherApprovalWindow(id, invoker),
    pause: extras.pause
      ?? (() => new Promise<void>((resolve) => {
        setTimeout(resolve, 1000);
      })),
    onStatus: extras.onStatus,
  };
}

// ---------------------------------------------------------------------------
// Panel-facing client seam
// ---------------------------------------------------------------------------

export interface LauncherConnectionsClient {
  readonly fetchConnections: () => Promise<LauncherConnectionsSnapshot>;
  readonly fetchModelProfiles: () => Promise<LauncherModelProfileList>;
  readonly saveModelProfile: (
    input: LauncherModelProfileInput,
  ) => Promise<LauncherModelProfile>;
  readonly configureProviderConnection: (
    request: LauncherProviderConfigureRequest,
    scope: LauncherActiveProfileContext,
    options?: {signal?: AbortSignal},
  ) => Promise<void>;
}

export function createLauncherConnectionsClient(
  invoker?: LauncherContractInvoker,
): LauncherConnectionsClient {
  return {
    fetchConnections: () => fetchLauncherConnectionsSnapshot(invoker),
    fetchModelProfiles: () => fetchLauncherModelProfiles(invoker),
    saveModelProfile: (input) => saveLauncherModelProfile(input, invoker),
    configureProviderConnection: (request, scope, options) =>
      configureLauncherProviderConnection(
        request,
        scope,
        createLauncherProviderConfigurePorts(invoker),
        options,
      ),
  };
}

export const defaultLauncherConnectionsClient: LauncherConnectionsClient =
  createLauncherConnectionsClient();
