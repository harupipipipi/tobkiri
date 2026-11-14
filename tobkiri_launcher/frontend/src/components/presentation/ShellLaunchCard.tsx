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
  openExternalUrl,
} from '@/src/lib/desktopHost';
import type {ApiPresentationState} from '@/src/lib/apiTypes';
import {translate, useT} from '@/src/lib/i18n';
import {packVmLaunchBlockedReason, WINDOWS_PACKVM_ISSUE_URL} from '@/src/lib/packVmLaunchReadiness';
import {launchDisabledReason} from '@/src/lib/presentation';
import {panelRoutes} from '@/src/lib/routes';
import {useAppStore} from '@/src/store';

function formatError(error: unknown): string {
  if (typeof error === 'string' && error.trim()) return error;
  return error instanceof Error && error.message.trim()
    ? error.message
    : translate('shell_launch.error');
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
  const t = useT();
  const addToast = useAppStore((state) => state.addToast);
  const packVmDoctor = useAppStore((state) => state.packVmDoctor);
  const refreshPackVMDoctor = useAppStore((state) => state.refreshPackVMDoctor);
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

  useEffect(() => {
    if (active && desktopShell && runtimeReady && !packVmDoctor) {
      void refreshPackVMDoctor({reconcile: false});
    }
  }, [active, desktopShell, runtimeReady, packVmDoctor, refreshPackVMDoctor]);

  const selectedShell = presentation?.selection
    ? presentation.catalog.shell_providers.find(
      (provider) => provider.provider_id === presentation.selection?.shell_provider_id,
    )
    : null;
  const materialization = presentation?.materialization ?? null;
  const needsSelection = Boolean(presentation && !presentation.selection);
  const packVmBlockedReason = packVmLaunchBlockedReason(packVmDoctor);
  const windowsPackVmUnavailable = Boolean(packVmBlockedReason) && packVmDoctor?.ready === false
    && packVmDoctor.platform.startsWith('windows-');
  const macPackVmNeedsProvision = packVmDoctor?.ready === false
    && packVmDoctor.platform === 'macos-arm64';
  const blockedReason = !active
    ? t('shell_launch.activate_before')
    : !desktopShell
    ? t('shell_launch.launcher_only')
    : !runtimeReady
    ? t('shell_launch.runtime_not_ready')
    : !presentation?.selection
      ? t('shell_launch.no_verified_selection')
      : packVmBlockedReason
        ? packVmBlockedReason
      : materialization
        ? launchDisabledReason(materialization)
        : t('shell_launch.materialization_unavailable');

  const launch = async () => {
    if (blockedReason || launching || launchingRef.current) return;
    launchingRef.current = true;
    setLaunching(true);
    setError(null);
    try {
      const result = await launchSelectedPresentation();
      addToast(result.message || t('shell_launch.opened', {name: profileDisplayName ?? t('shell_launch.profile')}), 'success');
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
            <CardTitle id={`shell-launch-title-${profileId ?? 'default'}`}>{t('shell_launch.title', {name: profileDisplayName ?? t('shell_launch.harness')})}</CardTitle>
          </div>
          <Badge variant={blockedReason ? 'warning' : 'success'}>
            {t(blockedReason ? 'shell_launch.unavailable' : 'shell_launch.verified')}
          </Badge>
        </div>
        <p className="text-sm leading-relaxed text-text-muted">
          {active
            ? t('shell_launch.active_description')
            : t('shell_launch.inactive_description')}
        </p>
      </CardHeader>
      <CardContent>
        {loading ? (
          <p className="flex items-center gap-2 text-sm text-text-muted" role="status" aria-busy="true">
            <TobkiriLoadingMark />
            {t('shell_launch.loading')}
          </p>
        ) : error ? (
          <div className="flex flex-wrap items-center gap-3" role="alert">
            <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0 text-destructive" />
            <p className="flex-1 text-sm text-destructive">{error}</p>
            <CopyErrorButton label={t('shell_launch.copy_error')} text={error} />
            <Button variant="outline" size="sm" onClick={() => void loadSurfaceState()}>
              {t('shell_launch.retry')}
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 space-y-1 text-sm">
              <p className="flex items-center gap-2 text-text-main">
                <Monitor className="h-4 w-4 shrink-0 text-text-muted" />
                <span className="truncate">{selectedShell?.display_name ?? t('shell_launch.no_shell')}</span>
              </p>
              <p className="flex items-center gap-2 text-xs text-text-muted">
                <Route className="h-3.5 w-3.5 shrink-0" />
                <span>{blockedReason ?? t('shell_launch.binding_verified')}</span>
              </p>
              {macPackVmNeedsProvision && (
                <p className="text-xs text-text-muted">{t('shell_launch.packvm_needs_provision')}</p>
              )}
              {(packVmBlockedReason || macPackVmNeedsProvision) && presentation?.selection && (
                <p className="flex flex-wrap gap-2 text-xs">
                  <Link className="underline" to={panelRoutes.packs}>{t('shell_launch.open_packs')}</Link>
                  {windowsPackVmUnavailable && (
                    <button
                      className="underline"
                      onClick={() => void openExternalUrl(WINDOWS_PACKVM_ISSUE_URL).catch(() => {
                        addToast(t('shell_launch.issue_open_failed'), 'error');
                      })}
                      type="button"
                    >{t('shell_launch.windows_issue')}</button>
                  )}
                </p>
              )}
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <Button
                className="min-h-11"
                disabled={(Boolean(blockedReason) && (!needsSelection || !onChooseShell)) || launching}
                loading={launching}
                onClick={() => needsSelection && onChooseShell ? onChooseShell() : void launch()}
                aria-busy={launching}
                aria-label={t(needsSelection && onChooseShell ? 'shell_launch.choose_for' : 'shell_launch.launch', {name: profileDisplayName ?? t('shell_launch.harness')})}
              >
                {launching ? t('shell_launch.opening') : needsSelection && onChooseShell ? t('shell_launch.choose') : t('shell_launch.launch', {name: profileDisplayName ?? t('shell_launch.harness')})}
              </Button>
              {!active && activationHref ? (
                <Link
                  className="inline-flex min-h-11 items-center justify-center rounded-lg border border-border bg-bg-main px-4 py-2 text-sm font-medium text-text-main transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2"
                  to={activationHref}
                >
                  {t('shell_launch.activate_first')}
                </Link>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
