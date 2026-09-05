import { Outlet, useLocation } from 'react-router';
import type {ReactNode} from 'react';
import {AlertCircle, AlertTriangle} from 'lucide-react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { ViewerVersionLabel } from './ViewerVersionLabel';
import { useAppStore } from '@/src/store';
import { describeRuntimeBanner } from '@/src/lib/runtimeHealth';
import { panelRoutes } from '@/src/lib/routes';
import { RouteBoundary } from './RouteBoundary';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';

export function shouldShowRuntimeErrorCopy(
  banner: Pick<ReturnType<typeof describeRuntimeBanner>, 'tone' | 'detail'>,
): boolean {
  return banner.tone === 'danger' && Boolean(banner.detail.trim());
}

export function runtimeBannerIconKind(
  tone: ReturnType<typeof describeRuntimeBanner>['tone'],
  runtimeStatus: string,
): 'error' | 'warning' | 'progress' {
  if (tone === 'danger') return 'error';
  if (runtimeStatus === 'profile_reconfirmation_required') return 'warning';
  return 'progress';
}

export function Layout({verificationBanner}: {verificationBanner?: ReactNode}) {
  const location = useLocation();
  const runtimeReady = useAppStore(state => state.runtimeReady);
  const runtimeStatus = useAppStore(state => state.runtimeStatus);
  const runtimeError = useAppStore(state => state.runtimeError);
  const runtimeDisconnected = useAppStore(state => state.runtimeDisconnected);
  const lastRuntimeHealthyAt = useAppStore(state => state.lastRuntimeHealthyAt);

  const runtimeBanner = describeRuntimeBanner({
    runtimeReady,
    runtimeStatus,
    runtimeError,
    runtimeDisconnected,
    lastRuntimeHealthyAt,
  });
  const canCopyRuntimeError = shouldShowRuntimeErrorCopy(runtimeBanner);
  const runtimeIconKind = runtimeBannerIconKind(runtimeBanner.tone, runtimeStatus);

  return (
    <div className="flex h-screen overflow-hidden bg-bg-main text-text-main">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main id="panel-main" tabIndex={-1} className="flex-1 flex flex-col relative overflow-hidden">
          {verificationBanner}
          {!runtimeReady && !verificationBanner && (
            <div
              role="alert"
              className={`flex items-center gap-3 border-b px-6 py-3 text-sm ${
                runtimeBanner.tone === 'danger'
                  ? 'border-destructive/35 bg-destructive/8 text-destructive'
                  : 'border-warning/35 bg-warning/8 text-warning'
              }`}
            >
              <span aria-hidden="true" className="flex h-4 w-4 shrink-0 items-center justify-center">
                {runtimeIconKind === 'error' ? (
                  <AlertCircle className="h-4 w-4" data-runtime-banner-icon="error" />
                ) : runtimeIconKind === 'warning' ? (
                  <AlertTriangle className="h-4 w-4" data-runtime-banner-icon="warning" />
                ) : (
                  <span className="h-2 w-2 rounded-full bg-current" data-runtime-banner-icon="progress" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className="font-medium">{runtimeBanner.title}</p>
                <p className="text-xs opacity-80">{runtimeBanner.detail}</p>
              </div>
              {canCopyRuntimeError ? (
                <CopyErrorButton
                  label="Copy runtime status error"
                  text={`${runtimeBanner.title}\n${runtimeBanner.detail}`}
                />
              ) : null}
            </div>
          )}
          <RouteBoundary>
            <Outlet />
          </RouteBoundary>
          {location.pathname === panelRoutes.home && <ViewerVersionLabel />}
        </main>
      </div>
    </div>
  );
}
