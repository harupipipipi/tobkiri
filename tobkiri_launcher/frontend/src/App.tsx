import { useDeferredValue, useEffect, useLayoutEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router';
import { useAppStore } from '@/src/store';
import { Layout } from '@/src/components/layout/Layout';
import { Setup } from '@/src/pages/Setup';
import { Dashboard } from '@/src/pages/Dashboard';
import { ToastContainer } from '@/src/components/ui/ToastContainer';
import { DialogContainer } from '@/src/components/ui/DialogContainer';
import { bootstrapPanelSession, hasPendingPanelBootstrapCode } from '@/src/lib/api';
import { applyAppearanceToRoot } from '@/src/lib/appearance';
import { runtimeMonitorDelay } from '@/src/lib/runtimeHealth';
import { panelRoutes } from '@/src/lib/routes';
import { hasSelectedSetupPack } from '@/src/lib/setupPacks';
import {
  LazyAiInputInspector,
  LazyApiMap,
  LazyFlows,
  LazyGraphEditor,
  LazyNodeManager,
  LazyPackDetail,
  LazyPacks,
  LazyProfileGraphEditor,
  LazyProfileWorkspace,
  LazySettings,
  LazyStartupProfiles,
} from '@/src/lib/routeModules';

export default function App() {
  const theme = useAppStore(state => state.theme);
  const colorMode = useAppStore(state => state.colorMode);
  const isSetupDone = useAppStore(state => state.isSetupDone);
  const setSetupDone = useAppStore(state => state.setSetupDone);
  const addToast = useAppStore(state => state.addToast);
  const refreshRuntimeHealth = useAppStore(state => state.refreshRuntimeHealth);

  useLayoutEffect(() => {
    applyAppearanceToRoot(document.documentElement, { theme, colorMode });
  }, [theme, colorMode]);

  useEffect(() => {
    if (!hasPendingPanelBootstrapCode()) {
      return;
    }

    void bootstrapPanelSession().catch((error) => {
      const message = error instanceof Error ? error.message : 'Panel bootstrap failed';
      addToast(message, 'error');
    });
  }, [addToast]);

  useEffect(() => {
    if (!isSetupDone) return;
    type IdleWindow = Window & {
      requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    const target = window as IdleWindow;
    let cancelled = false;
    const verifySetupPack = () => {
      void hasSelectedSetupPack()
        .then((verified) => {
          if (cancelled || verified) return;
          addToast('The selected setup pack is no longer available. Setup must be completed again.', 'error');
          setSetupDone(false);
        })
        .catch((error) => {
          if (cancelled) return;
          addToast(error instanceof Error ? error.message : 'Setup pack verification failed', 'error');
        });
    };

    let cancelScheduled: () => void;
    if (typeof target.requestIdleCallback === 'function') {
      const handle = target.requestIdleCallback(verifySetupPack, { timeout: 1_000 });
      cancelScheduled = () => target.cancelIdleCallback?.(handle);
    } else {
      const handle = window.setTimeout(verifySetupPack, 300);
      cancelScheduled = () => window.clearTimeout(handle);
    }

    return () => {
      cancelled = true;
      cancelScheduled();
    };
  }, [isSetupDone, setSetupDone, addToast]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const pollRuntimeHealth = async () => {
      while (!cancelled) {
        await refreshRuntimeHealth();
        if (cancelled) {
          return;
        }
        const currentState = useAppStore.getState();
        await new Promise<void>((resolve) => {
          timer = window.setTimeout(resolve, runtimeMonitorDelay({
            runtimeReady: currentState.runtimeReady,
            runtimeStatus: currentState.runtimeStatus,
            runtimeError: currentState.runtimeError,
            runtimeDisconnected: currentState.runtimeDisconnected,
            lastRuntimeHealthyAt: currentState.lastRuntimeHealthyAt,
          }));
        });
      }
    };

    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') {
        void refreshRuntimeHealth();
      }
    };

    window.addEventListener('focus', refreshWhenVisible);
    window.addEventListener('online', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    void pollRuntimeHealth();

    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
      window.removeEventListener('focus', refreshWhenVisible);
      window.removeEventListener('online', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [refreshRuntimeHealth]);

  return (
    <BrowserRouter basename="/panel">
      <DeferredRouteTree isSetupDone={isSetupDone} />
      <ToastContainer />
      <DialogContainer />
    </BrowserRouter>
  );
}

function DeferredRouteTree({ isSetupDone }: { isSetupDone: boolean }) {
  const location = useLocation();
  const deferredLocation = useDeferredValue(location);
  const routePending =
    deferredLocation.pathname !== location.pathname ||
    deferredLocation.search !== location.search ||
    deferredLocation.hash !== location.hash;

  return (
    <>
      <Routes location={deferredLocation}>
        <Route path={panelRoutes.setup} element={<Setup />} />

        <Route
          path={panelRoutes.home}
          element={isSetupDone ? <Layout /> : <Navigate to={panelRoutes.setup} replace />}
        >
          <Route index element={<Dashboard />} />
          <Route path={panelRoutes.packs.slice(1)} element={<LazyPacks />} />
          <Route path={`${panelRoutes.packs.slice(1)}/:id`} element={<LazyPackDetail />} />
          <Route path={panelRoutes.nodes.slice(1)} element={<LazyNodeManager />} />
          <Route path={panelRoutes.graphEditor.slice(1)} element={<LazyGraphEditor />} />
          <Route path={panelRoutes.profileGraph.slice(1)} element={<LazyProfileGraphEditor />} />
          <Route path={panelRoutes.aiInput.slice(1)} element={<LazyAiInputInspector />} />
          <Route path={panelRoutes.apiMap.slice(1)} element={<LazyApiMap />} />
          <Route path={panelRoutes.profileWorkspace.slice(1)} element={<LazyProfileWorkspace />} />
          <Route path={panelRoutes.startup.slice(1)} element={<LazyStartupProfiles />} />
          <Route path={panelRoutes.flows.slice(1)} element={<LazyFlows />} />
          <Route path={panelRoutes.settings.slice(1)} element={<LazySettings />} />
        </Route>
      </Routes>
      {routePending && (
        <div
          role="status"
          aria-label="Opening page"
          className="pointer-events-none fixed inset-x-0 top-0 z-[100] h-0.5 overflow-hidden bg-accent/15"
        >
          <div className="h-full w-1/3 animate-pulse bg-accent" />
        </div>
      )}
    </>
  );
}
