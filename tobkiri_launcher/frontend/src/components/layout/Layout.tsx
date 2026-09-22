import { Outlet, Navigate, useLocation } from 'react-router';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { ViewerVersionLabel } from './ViewerVersionLabel';
import { useAppStore } from '@/src/store';
import { describeRuntimeBanner } from '@/src/lib/runtimeHealth';
import { panelRoutes } from '@/src/lib/routes';
import { RouteBoundary } from './RouteBoundary';

export function Layout() {
  const location = useLocation();
  const isSetupDone = useAppStore(state => state.isSetupDone);
  const runtimeReady = useAppStore(state => state.runtimeReady);
  const runtimeStatus = useAppStore(state => state.runtimeStatus);
  const runtimeError = useAppStore(state => state.runtimeError);
  const runtimeDisconnected = useAppStore(state => state.runtimeDisconnected);
  const lastRuntimeHealthyAt = useAppStore(state => state.lastRuntimeHealthyAt);

  if (!isSetupDone) {
    return <Navigate to={panelRoutes.setup} replace />;
  }
  if (runtimeStatus === 'profile_reconfirmation_required') {
    return <Navigate to={panelRoutes.setup} replace />;
  }

  const runtimeBanner = describeRuntimeBanner({
    runtimeReady,
    runtimeStatus,
    runtimeError,
    runtimeDisconnected,
    lastRuntimeHealthyAt,
  });

  return (
    <div className="flex h-screen overflow-hidden bg-bg-main text-text-main">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Header />
        <main id="panel-main" tabIndex={-1} className="flex-1 flex flex-col relative overflow-hidden">
          {!runtimeReady && (
            <div
              role="alert"
              className={`flex items-center gap-3 border-b px-6 py-3 text-sm ${
                runtimeBanner.tone === 'danger'
                  ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300'
                  : 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/30 dark:bg-amber-950/20 dark:text-amber-300'
              }`}
            >
              <div className={`mt-1 h-2 w-2 shrink-0 rounded-full ${runtimeBanner.tone === 'danger' ? 'bg-red-500' : 'bg-amber-500 animate-pulse'}`} />
              <div className="min-w-0">
                <p className="font-medium">{runtimeBanner.title}</p>
                <p className="text-xs opacity-80">{runtimeBanner.detail}</p>
              </div>
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
