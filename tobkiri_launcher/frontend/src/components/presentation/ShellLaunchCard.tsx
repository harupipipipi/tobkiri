import {useCallback, useEffect, useRef, useState} from 'react';
import {AlertCircle, Monitor, Route} from 'lucide-react';
import {Link} from 'react-router';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {Card, CardContent, CardHeader, CardTitle} from '@/src/components/ui/Card';
import {TobkiriLoadingMark} from '@/src/components/ui/TobkiriLoader';
import {
  fetchPresentationState,
  isDesktopShellAvailable,
  launchSelectedPresentation,
} from '@/src/lib/api';
import type {ApiPresentationState} from '@/src/lib/apiTypes';
import {launchDisabledReason} from '@/src/lib/presentation';
import {useAppStore} from '@/src/store';

function formatError(error: unknown): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : 'Tobkiri could not verify the selected Profile launch surface.';
}

export function ShellLaunchCard({
  runtimeReady,
  profileId,
  profileDisplayName,
  active = true,
  activationHref,
  onChooseShell,
}: {
  runtimeReady: boolean;
  profileId?: string;
  profileDisplayName?: string;
  active?: boolean;
  activationHref?: string;
  onChooseShell?: () => void;
}) {
  const addToast = useAppStore((state) => state.addToast);
  const [presentation, setPresentation] = useState<ApiPresentationState | null>(null);
  const [loading, setLoading] = useState(false);
  const [launching, setLaunching] = useState(false);
  const launchingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const desktopShell = isDesktopShellAvailable();
  const loadSurfaceState = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextPresentation = await fetchPresentationState();
      setPresentation(nextPresentation);
    } catch (loadError) {
      setError(formatError(loadError));
      setPresentation(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active && desktopShell && runtimeReady) {
      void loadSurfaceState();
    }
  }, [active, desktopShell, runtimeReady, loadSurfaceState]);

  const selectedShell = presentation?.selection
    ? presentation.catalog.shell_providers.find(
      (provider) => provider.provider_id === presentation.selection?.shell_provider_id,
    )
    : null;
  const materialization = presentation?.materialization ?? null;
  const needsSelection = Boolean(presentation && !presentation.selection);
  const blockedReason = !active
    ? 'Activate this Profile before launching its Shell.'
    : !desktopShell
    ? 'Profile launch is available in Tobkiri Launcher.'
    : !runtimeReady
    ? 'The selected Shell becomes available after Tobkiri runtime readiness.'
    : !presentation?.selection
      ? 'No verified Shell selection is active.'
      : materialization
        ? launchDisabledReason(materialization)
        : 'The selected Shell materialization is unavailable.';

  const launch = async () => {
    if (blockedReason || launching || launchingRef.current) return;
    launchingRef.current = true;
    setLaunching(true);
    setError(null);
    try {
      const result = await launchSelectedPresentation();
      addToast(result.message || `${profileDisplayName ?? 'Profile'} opened in the selected Shell.`, 'success');
    } catch (launchError) {
      setError(formatError(launchError));
    } finally {
      launchingRef.current = false;
      setLaunching(false);
    }
  };

  return (
    <Card aria-labelledby={`shell-launch-title-${profileId ?? 'default'}`}>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2">
            <Monitor className="h-4 w-4 text-accent" aria-hidden="true" />
            <CardTitle id={`shell-launch-title-${profileId ?? 'default'}`}>{profileDisplayName ?? 'Defaults Profile'} launch</CardTitle>
          </div>
          <Badge variant={blockedReason ? 'warning' : 'success'}>
            {blockedReason ? 'Unavailable' : 'Ready'}
          </Badge>
        </div>
        <p className="text-sm leading-relaxed text-text-muted">
          {active
            ? 'Launch this active execution Profile through its verified Tobkiri Shell binding. The handoff remains bound to the Profile revision, activation, and Plan identity.'
            : 'This Profile is browse-only until it completes the same resolve, review, Authority approval, and activation ceremony as every other Profile.'}
        </p>
      </CardHeader>
      <CardContent>
        {loading ? (
          <p className="flex items-center gap-2 text-sm text-text-muted" role="status" aria-busy="true">
            <TobkiriLoadingMark />
            Loading the selected Shell…
          </p>
        ) : error ? (
          <div className="flex flex-wrap items-center gap-3" role="alert">
            <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0 text-destructive" />
            <p className="flex-1 text-sm text-destructive">{error}</p>
            <CopyErrorButton label="Copy Shell launch error" text={error} />
            <Button variant="outline" size="sm" onClick={() => void loadSurfaceState()}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 space-y-1 text-sm">
              <p className="flex items-center gap-2 text-text-main">
                <Monitor className="h-4 w-4 shrink-0 text-text-muted" />
                <span className="truncate">{selectedShell?.display_name ?? 'No Shell selected'}</span>
              </p>
              <p className="flex items-center gap-2 text-xs text-text-muted">
                <Route className="h-3.5 w-3.5 shrink-0" />
                <span>{blockedReason ?? 'Verified Profile handoff is ready.'}</span>
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <Button
                className="min-h-11"
                disabled={(Boolean(blockedReason) && (!needsSelection || !onChooseShell)) || launching}
                loading={launching}
                onClick={() => needsSelection && onChooseShell ? onChooseShell() : void launch()}
                aria-busy={launching}
                aria-label={`${needsSelection && onChooseShell ? 'Choose Shell for' : 'Launch'} ${profileDisplayName ?? 'Defaults Profile'}`}
              >
                {launching ? 'Opening…' : needsSelection && onChooseShell ? 'Choose Shell' : `Launch ${profileDisplayName ?? 'Defaults Profile'}`}
              </Button>
              {!active && activationHref ? (
                <Link
                  className="inline-flex min-h-11 items-center justify-center rounded-lg border border-border bg-bg-main px-4 py-2 text-sm font-medium text-text-main transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2"
                  to={activationHref}
                >
                  Activate first
                </Link>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
