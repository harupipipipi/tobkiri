import {CircleAlert, RefreshCw} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import type {
  LauncherConnectionsSnapshot,
  LauncherModelProfileList,
} from '@/src/lib/launcherConnections';

const COPY = {
  refresh: 'Refresh',
  connectionsHeading: 'Registered connections',
  modelsHeading: 'Registered models',
  connectionsEmpty: 'No provider connections are registered for this Profile.',
  modelsEmpty: 'No model registrations are visible for this Profile.',
  credential: {
    configured: 'key configured',
    missing: 'key missing',
    not_required: 'keyless',
  } as Record<string, string>,
  health: {
    verified: 'verified',
    unverified: 'unverified',
  } as Record<string, string>,
  reachability: {
    available: 'reachable',
    unavailable: 'unreachable',
    unknown: 'unknown reachability',
  } as Record<string, string>,
  enabled: 'enabled',
  disabled: 'disabled',
  routed: 'routed',
};

function badgeVariant(value: string): 'success' | 'secondary' | 'warning' | 'destructive' | 'outline' {
  switch (value) {
    case 'configured':
    case 'verified':
    case 'available':
      return 'success';
    case 'missing':
    case 'unavailable':
      return 'warning';
    default:
      return 'secondary';
  }
}

export function ConnectionStatusList({
  snapshot,
  models,
  loading,
  connectionsError,
  modelsError,
  onRefresh,
}: {
  snapshot: LauncherConnectionsSnapshot | null;
  models: LauncherModelProfileList | null;
  loading: boolean;
  connectionsError: string | null;
  modelsError: string | null;
  onRefresh: () => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-text-muted">
          Connections and models registered here are shared with Harness and
          Flow for this Profile.
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onRefresh}
          disabled={loading}
        >
          <RefreshCw className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
          {COPY.refresh}
        </Button>
      </div>

      <section aria-label={COPY.connectionsHeading}>
        <h3 className="mb-2 text-sm font-medium text-text-main">{COPY.connectionsHeading}</h3>
        {connectionsError ? (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2" role="alert">
            <CircleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <p className="min-w-0 flex-1 break-words text-xs text-destructive">{connectionsError}</p>
            <CopyErrorButton label="Copy connections error" text={connectionsError} />
          </div>
        ) : !snapshot || snapshot.providers.length === 0 ? (
          <p className="text-xs text-text-muted">{COPY.connectionsEmpty}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {snapshot.providers.map((provider) => (
              <li
                key={provider.provider_instance_id}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-border px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-text-main">
                    {provider.display_name || provider.provider_instance_id}
                  </p>
                  <p className="truncate font-mono text-[11px] text-text-muted">
                    {provider.provider_instance_id}
                  </p>
                </div>
                <Badge variant={provider.enabled ? 'outline' : 'secondary'}>
                  {provider.enabled ? COPY.enabled : COPY.disabled}
                </Badge>
                <Badge variant={badgeVariant(provider.credential_status)}>
                  {COPY.credential[provider.credential_status] ?? provider.credential_status}
                </Badge>
                <Badge variant={badgeVariant(provider.health_status)}>
                  {COPY.health[provider.health_status] ?? provider.health_status}
                </Badge>
                <Badge variant={badgeVariant(provider.reachability)}>
                  {COPY.reachability[provider.reachability] ?? provider.reachability}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label={COPY.modelsHeading}>
        <h3 className="mb-2 text-sm font-medium text-text-main">{COPY.modelsHeading}</h3>
        {modelsError ? (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2" role="alert">
            <CircleAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <p className="min-w-0 flex-1 break-words text-xs text-destructive">{modelsError}</p>
            <CopyErrorButton label="Copy models error" text={modelsError} />
          </div>
        ) : !models || models.profiles.length === 0 ? (
          <p className="text-xs text-text-muted">{COPY.modelsEmpty}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {models.profiles.map((profile) => (
              <li
                key={profile.profile_id}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-border px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-text-main">
                    {profile.display_name}
                  </p>
                  <p className="truncate font-mono text-[11px] text-text-muted">
                    {profile.profile_id}
                    {profile.model_id ? ` · ${profile.model_id}` : ''}
                    {profile.provider_id ? ` · ${profile.provider_id}` : ''}
                  </p>
                </div>
                {profile.route_configured ? (
                  <Badge variant="success">{COPY.routed}</Badge>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
