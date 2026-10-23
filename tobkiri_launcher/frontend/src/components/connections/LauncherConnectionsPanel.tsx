import {useEffect, useRef, useState} from 'react';
import {Cable, CircleAlert, ShieldCheck} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Card, CardContent, CardDescription, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {
  isLauncherActiveProfileContext,
  isLauncherStaleContextError,
  LAUNCHER_STALE_CONTEXT_MESSAGE,
  LauncherConnectionsError,
  type LauncherActiveProfileContext,
  type LauncherConnectionsSnapshot,
  type LauncherModelProfileInput,
  type LauncherModelProfileList,
  type LauncherProviderConfigureRequest,
} from '@/src/lib/launcherConnections';
import {
  defaultLauncherConnectionsClient,
  readLauncherProviderPending,
  type LauncherConnectionsClient,
  type LauncherProviderPendingRecord,
} from '@/src/lib/launcherConnectionsConfigure';
import {formatUserFacingError} from '@/src/lib/userFacingError';
import {getBrowserStorage} from '@/src/lib/safeStorage';
import {ConnectionStatusList} from './ConnectionStatusList';
import {ModelProfileForm} from './ModelProfileForm';
import {ProviderConnectionForm} from './ProviderConnectionForm';

const COPY = {
  title: 'Connections & models',
  description:
    'Register provider API connections and models for the verified active '
    + 'Profile. The same registry is shared by Harness and Flow; secrets are '
    + 'only accepted through the Host approval flow and are never stored here.',
  activeProfile: 'active Profile',
  readOnly: 'read-only',
  noScope:
    'A verified active Profile is required to read or change the shared '
    + 'connections registry.',
  pendingHint:
    'An unfinished registration is recorded for connection',
  pendingHintSuffix:
    'under this Profile. Save the identical input again to check or complete it.',
  contextChanged: LAUNCHER_STALE_CONTEXT_MESSAGE,
};

/**
 * Shared provider/model registration panel. `profile` must be the verified
 * active-Profile identity captured by the root runtime surface; `disabled`
 * additionally renders everything read-only while an inactive Profile is
 * inspected. The root owns Settings wiring; this component never navigates.
 */
export function LauncherConnectionsPanel({
  profile,
  disabled = false,
  client = defaultLauncherConnectionsClient,
  onRegistryChanged,
}: {
  profile: LauncherActiveProfileContext | null;
  disabled?: boolean;
  client?: LauncherConnectionsClient;
  onRegistryChanged?: () => void;
}) {
  const scope =
    profile && isLauncherActiveProfileContext(profile) ? profile : null;
  const scopeKey = scope ? `${scope.profile_id}:${scope.activation_id}` : null;

  const [connections, setConnections] =
    useState<LauncherConnectionsSnapshot | null>(null);
  const [models, setModels] = useState<LauncherModelProfileList | null>(null);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [pending, setPending] =
    useState<LauncherProviderPendingRecord | null>(null);
  const [contextStale, setContextStale] = useState(false);
  const requestVersion = useRef(0);
  const mountedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);

  const refreshPending = (context: LauncherActiveProfileContext | null) => {
    setPending(
      context
        ? readLauncherProviderPending(getBrowserStorage('session'), context)
        : null,
    );
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    // A Profile or activation change abandons any in-flight confirmation.
    abortRef.current?.abort();
    abortRef.current = null;
    requestVersion.current += 1;
    setContextStale(false);
    refreshPending(scope);
  }, [scopeKey]); // eslint-disable-line react-hooks/exhaustive-deps -- scope derives from scopeKey

  useEffect(() => {
    // Inspecting an inactive Profile stops an in-flight confirmation, not
    // merely the submit controls.
    if (disabled) {
      abortRef.current?.abort();
      abortRef.current = null;
    }
  }, [disabled]);

  useEffect(() => {
    if (!scope) {
      setConnections(null);
      setModels(null);
      setConnectionsError(null);
      setModelsError(null);
      setLoading(false);
      return;
    }
    const version = ++requestVersion.current;
    setLoading(true);
    void Promise.allSettled([
      client.fetchConnections(),
      client.fetchModelProfiles(),
    ]).then(([connectionsResult, modelsResult]) => {
      if (!mountedRef.current || requestVersion.current !== version) return;
      if (connectionsResult.status === 'fulfilled') {
        setConnections(connectionsResult.value);
        setConnectionsError(null);
      } else if (isLauncherStaleContextError(connectionsResult.reason)) {
        setConnections(null);
        setContextStale(true);
      } else {
        setConnectionsError(formatUserFacingError(
          connectionsResult.reason,
          'The registered connections could not be loaded.',
          'launcher.connections.status',
        ));
      }
      if (modelsResult.status === 'fulfilled') {
        setModels(modelsResult.value);
        setModelsError(null);
      } else if (isLauncherStaleContextError(modelsResult.reason)) {
        setModels(null);
        setContextStale(true);
      } else {
        setModelsError(formatUserFacingError(
          modelsResult.reason,
          'The registered models could not be loaded.',
          'launcher.connections.models',
        ));
      }
      setLoading(false);
    });
  }, [scopeKey, client, reloadNonce]); // eslint-disable-line react-hooks/exhaustive-deps -- scope derives from scopeKey

  const submitProviderConnection = async (
    request: LauncherProviderConfigureRequest,
  ): Promise<void> => {
    if (!scope || disabled || contextStale) {
      throw new LauncherConnectionsError(
        'A verified active Profile is required to register a connection.',
      );
    }
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    try {
      await client.configureProviderConnection(request, scope, {
        signal: controller.signal,
      });
    } catch (error) {
      // A Host-side stale capture invalidates the whole rendered view.
      if (isLauncherStaleContextError(error)) setContextStale(true);
      throw error;
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      refreshPending(scope);
    }
    setReloadNonce((nonce) => nonce + 1);
    onRegistryChanged?.();
  };

  const submitModelProfile = async (
    input: LauncherModelProfileInput,
  ): Promise<void> => {
    if (!scope || disabled || contextStale) {
      throw new LauncherConnectionsError(
        'A verified active Profile is required to register a model.',
      );
    }
    try {
      await client.saveModelProfile(input);
    } catch (error) {
      if (isLauncherStaleContextError(error)) setContextStale(true);
      throw error;
    }
    setReloadNonce((nonce) => nonce + 1);
    onRegistryChanged?.();
  };

  const readOnly = disabled || !scope || contextStale;

  return (
    <Card id="launcher-connections">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2">
            <Cable className="h-4 w-4" aria-hidden="true" />
            {COPY.title}
          </CardTitle>
          <div className="flex items-center gap-2">
            {scope ? (
              <Badge variant="success">
                <ShieldCheck className="mr-1 h-3 w-3" aria-hidden="true" />
                {`${COPY.activeProfile}: ${scope.profile_id}`}
              </Badge>
            ) : null}
            {readOnly ? <Badge variant="secondary">{COPY.readOnly}</Badge> : null}
          </div>
        </div>
        <CardDescription>{COPY.description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {!scope ? (
          <div className="flex items-start gap-2 rounded-lg border border-border bg-bg-hover/40 px-3 py-2">
            <CircleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-text-muted" />
            <p className="text-xs text-text-muted">{COPY.noScope}</p>
          </div>
        ) : (
          <>
            {contextStale ? (
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2" role="alert">
                <p className="text-xs text-destructive">{COPY.contextChanged}</p>
              </div>
            ) : null}
            {pending ? (
              <div className="rounded-lg border border-warning/40 bg-warning/5 px-3 py-2" role="status">
                <p className="text-xs text-text-main">
                  {`${COPY.pendingHint} ${pending.connection} ${COPY.pendingHintSuffix}`}
                </p>
              </div>
            ) : null}
            <ConnectionStatusList
              snapshot={connections}
              models={models}
              loading={loading}
              connectionsError={connectionsError}
              modelsError={modelsError}
              onRefresh={() => {
                setContextStale(false);
                setReloadNonce((nonce) => nonce + 1);
              }}
            />
            <ProviderConnectionForm
              key={`provider:${scopeKey ?? 'none'}`}
              disabled={readOnly}
              onSubmit={submitProviderConnection}
            />
            <ModelProfileForm
              key={`models:${scopeKey ?? 'none'}`}
              disabled={readOnly}
              providers={connections?.providers ?? []}
              onSubmit={submitModelProfile}
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}
