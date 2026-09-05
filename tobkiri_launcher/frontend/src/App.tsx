import {
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { BrowserRouter, Routes, Route, Link, useLocation } from 'react-router';
import {ShieldAlert} from 'lucide-react';
import {
  cancelPackMutationReconciliation,
  useAppStore,
} from '@/src/store';
import type {RuntimeStatus} from '@/src/lib/apiTypes';
import { Layout } from '@/src/components/layout/Layout';
import { Setup } from '@/src/pages/Setup';
import { Dashboard } from '@/src/pages/Dashboard';
import { ToastContainer } from '@/src/components/ui/ToastContainer';
import { DialogContainer } from '@/src/components/ui/DialogContainer';
import { CopyErrorButton } from '@/src/components/ui/CopyErrorButton';
import { bootstrapPanelSession, hasPendingPanelBootstrapCode } from '@/src/lib/api';
import { applyAppearanceToRoot } from '@/src/lib/appearance';
import { runtimeMonitorDelay } from '@/src/lib/runtimeHealth';
import { panelRoutes } from '@/src/lib/routes';
import {
  resolveSetupVerificationState,
  type SetupVerificationState,
} from '@/src/lib/setupVerification';
import { RouteAnnouncer } from '@/src/components/layout/RouteAnnouncer';
import {
  LazyAiInput,
  LazyApiMap,
  LazyFlow,
  LazyGraph,
  LazyPackDetail,
  LazyPacks,
  LazyNodeManager,
  LazyProfile,
  LazyProfileFiles,
  LazyProfileWiring,
  LazySettings,
} from '@/src/lib/routeModules';

export default function App() {
  const theme = useAppStore(state => state.theme);
  const colorMode = useAppStore(state => state.colorMode);
  const isSetupDone = useAppStore(state => state.isSetupDone);
  const runtimeReady = useAppStore(state => state.runtimeReady);
  const runtimeStatus = useAppStore(state => state.runtimeStatus);
  const runtimeDisconnected = useAppStore(state => state.runtimeDisconnected);
  const defaultsBootstrapRequired = useAppStore(state => state.defaultsBootstrapRequired);
  const addToast = useAppStore(state => state.addToast);
  const refreshRuntimeHealth = useAppStore(state => state.refreshRuntimeHealth);

  useLayoutEffect(() => {
    applyAppearanceToRoot(document.documentElement, { theme, colorMode });
  }, [theme, colorMode]);

  useEffect(() => () => {
    cancelPackMutationReconciliation();
  }, []);

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
      <DeferredRouteTree
        isSetupDone={isSetupDone}
        runtimeReady={runtimeReady}
        runtimeStatus={runtimeStatus}
        runtimeDisconnected={runtimeDisconnected}
        defaultsBootstrapRequired={defaultsBootstrapRequired}
        onRetryRuntimeHealth={refreshRuntimeHealth}
      />
      <ToastContainer />
      <DialogContainer />
    </BrowserRouter>
  );
}

export interface SetupVerificationGateProps {
  children: ReactNode;
  isSetupDone: boolean;
  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeDisconnected: boolean;
  defaultsBootstrapRequired: boolean;
  onRetry?: () => void | Promise<void>;
  /** Render a compact blocker inside the already-mounted panel layout. */
  embedded?: boolean;
}

export interface SetupVerificationBannerProps {
  isSetupDone: boolean;
  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeDisconnected: boolean;
  defaultsBootstrapRequired: boolean;
  onRetry?: () => void | Promise<void>;
}

export interface HomeRouteProps {
  verificationBanner: ReactNode;
}

export interface DevtoolsRouteGateProps {
  children: ReactNode;
  /** Test seam; production always reads the Launcher-local preference. */
  enabled?: boolean;
}

type SetupVerificationCopy = {
  role: 'status' | 'alert';
  title: string;
  detail: string;
};

const setupVerificationCopy: Record<SetupVerificationState, SetupVerificationCopy> = {
  checking: {
    role: 'status',
    title: 'Verifying Tobkiri setup',
    detail: 'The local runtime is still being verified. Pages and runtime actions will appear after the current Profile and authority state are confirmed.',
  },
  verified: {
    role: 'status',
    title: 'Tobkiri setup verified',
    detail: 'The local runtime is ready.',
  },
  needs_setup: {
    role: 'alert',
    title: 'Complete setup to continue',
    detail: 'Open Setup to review the Host-owned activation state before using runtime pages.',
  },
  needs_reconfirm: {
    role: 'alert',
    title: 'Review setup before continuing',
    detail: 'The active Profile needs a fresh Host verification before runtime pages and actions can resume.',
  },
  denied: {
    role: 'alert',
    title: 'Setup verification is unavailable',
    detail: 'Tobkiri could not confirm the local runtime. Retry verification or open Setup to recover safely.',
  },
};

function VerificationActions({
  onRetry,
  retrying,
  linkClassName,
}: {
  onRetry?: () => void | Promise<void>;
  retrying: boolean;
  linkClassName: string;
}) {
  const retry = () => {
    if (!onRetry || retrying) return;
    void onRetry();
  };

  return (
    <div className="mt-6 flex flex-wrap items-center gap-3">
      {onRetry ? (
        <button
          type="button"
          className="inline-flex min-h-11 items-center justify-center rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50"
          disabled={retrying}
          aria-busy={retrying}
          onClick={retry}
        >
          {retrying ? 'Checking…' : 'Retry verification'}
        </button>
      ) : null}
      <Link
        className={linkClassName}
        to={panelRoutes.setup}
      >
        Open Setup
      </Link>
    </div>
  );
}

function VerificationMessage({
  state,
  onRetry,
  retrying,
  compact = false,
  testId,
  titleId,
}: {
  state: SetupVerificationState;
  onRetry?: () => void | Promise<void>;
  retrying: boolean;
  compact?: boolean;
  testId?: string;
  titleId?: string;
}) {
  const copy = setupVerificationCopy[state];
  const resolvedTitleId = titleId ?? (compact ? 'runtime-route-verification-title' : 'setup-verification-title');
  const linkClassName = 'inline-flex min-h-11 items-center justify-center rounded-lg border border-border bg-bg-main px-4 py-2 text-sm font-medium transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2';

  return (
    <section
      aria-busy={state === 'checking'}
      aria-labelledby={resolvedTitleId}
      aria-live={copy.role === 'status' ? 'polite' : 'assertive'}
      className={compact
        ? 'rounded-xl border border-border bg-bg-card p-5'
        : 'w-full max-w-xl rounded-2xl border border-border bg-bg-card p-7 shadow-lg'}
      data-testid={testId}
      role={copy.role}
    >
      <p className="text-xs font-medium text-text-muted">
        Runtime access
      </p>
      {compact ? (
        <h2 id={resolvedTitleId} className="mt-2 text-lg font-semibold tracking-tight">
          {copy.title}
        </h2>
      ) : (
        <h1 id={resolvedTitleId} className="mt-3 text-2xl font-semibold tracking-tight">
          {copy.title}
        </h1>
      )}
      <div className="mt-3 flex items-start gap-2">
        {state === 'denied' ? <ShieldAlert aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" data-error-icon="setup-verification" /> : null}
        <p className="min-w-0 flex-1 text-sm leading-6 text-text-muted">{copy.detail}</p>
        {state === 'denied' ? (
          <CopyErrorButton
            label="Copy setup verification error"
            text={`${copy.title}\n${copy.detail}`}
          />
        ) : null}
      </div>
      <VerificationActions
        linkClassName={linkClassName}
        onRetry={onRetry}
        retrying={retrying}
      />
    </section>
  );
}

/** Keep Home visible while directing unresolved runtime state to Setup. */
export function SetupVerificationBanner({
  isSetupDone,
  runtimeReady,
  runtimeStatus,
  runtimeDisconnected,
  defaultsBootstrapRequired,
  onRetry,
}: SetupVerificationBannerProps) {
  const state = resolveSetupVerificationState({
    isSetupDone,
    runtimeReady,
    runtimeStatus,
    runtimeDisconnected,
    defaultsBootstrapRequired,
  });
  const [retrying, setRetrying] = useState(false);

  if (state === 'verified') return null;

  const retry = () => {
    if (!onRetry || retrying) return;
    setRetrying(true);
    void Promise.resolve()
      .then(onRetry)
      .finally(() => setRetrying(false));
  };

  return (
    <div className="border-b border-amber-200 bg-amber-50 px-6 py-3 text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-200" data-testid="setup-verification-banner">
      <VerificationMessage
        state={state}
        onRetry={onRetry ? retry : undefined}
        retrying={retrying}
        compact
        testId="setup-verification-banner-content"
        titleId="setup-verification-banner-title"
      />
    </div>
  );
}

/**
 * Home is the Profile catalog and recovery surface. Keep its content mounted
 * while runtime verification is unresolved; runtime-only child routes use the
 * gate below instead.
 */
export function HomeRoute({verificationBanner}: HomeRouteProps) {
  return <Layout verificationBanner={verificationBanner} />;
}

/**
 * Hide diagnostic routes and raw invocation UI without changing their stable
 * URLs. This local presentation gate is deliberately independent from runtime
 * Profile, Pack closure, and authority checks.
 */
export function DevtoolsRouteGate({children, enabled}: DevtoolsRouteGateProps) {
  const storedPreference = useAppStore((state) => state.devtoolsEnabled);
  const devtoolsEnabled = enabled ?? storedPreference;
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!devtoolsEnabled) {
      headingRef.current?.focus({preventScroll: true});
    }
  }, [devtoolsEnabled]);

  if (devtoolsEnabled) return <>{children}</>;

  return (
    <section
      className="m-auto w-full max-w-xl rounded-2xl border border-border bg-bg-card p-7 shadow-sm"
      aria-labelledby="devtools-route-gate-title"
      data-testid="devtools-route-gate"
    >
      <p className="text-xs font-medium text-text-muted">
        Launcher preference
      </p>
      <h2
        ref={headingRef}
        id="devtools-route-gate-title"
        tabIndex={-1}
        className="mt-3 text-2xl font-semibold tracking-tight focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
      >
        Devtools are hidden
      </h2>
      <p className="mt-3 text-sm leading-6 text-text-muted">
        Enable Devtools in Settings to open this diagnostic surface. The
        preference changes Launcher presentation only and does not grant
        runtime authority or alter the active Profile or Pack closure.
      </p>
      <Link
        className="mt-6 inline-flex min-h-11 items-center justify-center rounded-lg border border-border bg-bg-main px-4 py-2 text-sm font-medium transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)] focus-visible:ring-offset-2"
        to={panelRoutes.settings}
      >
        Open Settings
      </Link>
    </section>
  );
}

/**
 * Keep every runtime route behind the same health and authority decision.
 * Setup remains reachable because it is the recovery surface for unresolved
 * or stale runtime state.
 */
export function SetupVerificationGate({
  children,
  isSetupDone,
  runtimeReady,
  runtimeStatus,
  runtimeDisconnected,
  defaultsBootstrapRequired,
  onRetry,
  embedded = false,
}: SetupVerificationGateProps) {
  const state = resolveSetupVerificationState({
    isSetupDone,
    runtimeReady,
    runtimeStatus,
    runtimeDisconnected,
    defaultsBootstrapRequired,
  });
  const [retrying, setRetrying] = useState(false);

  if (state === 'verified') return <>{children}</>;

  const retry = () => {
    if (!onRetry || retrying) return;
    setRetrying(true);
    void Promise.resolve()
      .then(onRetry)
      .finally(() => setRetrying(false));
  };

  if (embedded) {
    return (
      <VerificationMessage
        state={state}
        onRetry={onRetry ? retry : undefined}
        retrying={retrying}
        compact
        testId="runtime-route-verification-gate"
        titleId="runtime-route-verification-title"
      />
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg-main px-6 py-10 text-text-main">
      <VerificationMessage
        state={state}
        onRetry={retry}
        retrying={retrying}
        testId="setup-verification-gate"
      />
    </main>
  );
}

function DeferredRouteTree({
  isSetupDone,
  runtimeReady,
  runtimeStatus,
  runtimeDisconnected,
  defaultsBootstrapRequired,
  onRetryRuntimeHealth,
}: {
  isSetupDone: boolean;
  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeDisconnected: boolean;
  defaultsBootstrapRequired: boolean;
  onRetryRuntimeHealth: () => Promise<void>;
}) {
  const location = useLocation();
  const deferredLocation = useDeferredValue(location);
  const routePending =
    deferredLocation.pathname !== location.pathname ||
    deferredLocation.search !== location.search ||
    deferredLocation.hash !== location.hash;

  const verificationBanner = (
    <SetupVerificationBanner
      isSetupDone={isSetupDone}
      runtimeReady={runtimeReady}
      runtimeStatus={runtimeStatus}
      runtimeDisconnected={runtimeDisconnected}
      defaultsBootstrapRequired={defaultsBootstrapRequired}
      onRetry={onRetryRuntimeHealth}
    />
  );
  const gateRuntimeRoute = (element: ReactNode) => (
    <SetupVerificationGate
      isSetupDone={isSetupDone}
      runtimeReady={runtimeReady}
      runtimeStatus={runtimeStatus}
      runtimeDisconnected={runtimeDisconnected}
      defaultsBootstrapRequired={defaultsBootstrapRequired}
      onRetry={onRetryRuntimeHealth}
      embedded
    >
      {element}
    </SetupVerificationGate>
  );
  const gateDevtoolsRoute = (element: ReactNode) => (
    <DevtoolsRouteGate>{gateRuntimeRoute(element)}</DevtoolsRouteGate>
  );

  return (
    <>
      <RouteAnnouncer pathname={deferredLocation.pathname} />
      <Routes location={deferredLocation}>
        <Route path={panelRoutes.setup} element={<Setup />} />

        <Route
          path={panelRoutes.home}
          element={<HomeRoute verificationBanner={verificationBanner} />}
        >
          <Route index element={<Dashboard />} />
          <Route path={panelRoutes.packs.slice(1)} element={gateRuntimeRoute(<LazyPacks />)} />
          <Route path={`${panelRoutes.packs.slice(1)}/:id`} element={gateRuntimeRoute(<LazyPackDetail />)} />
          <Route path={panelRoutes.profile.slice(1)} element={<LazyProfile />} />
          <Route path={panelRoutes.settings.slice(1)} element={<LazySettings />} />
          <Route path={panelRoutes.profileWiring.slice(1)} element={gateDevtoolsRoute(<LazyProfileWiring />)} />
          <Route path={panelRoutes.profileFiles.slice(1)} element={gateDevtoolsRoute(<LazyProfileFiles />)} />
          <Route path={panelRoutes.flow.slice(1)} element={gateDevtoolsRoute(<LazyFlow />)} />
          <Route path={panelRoutes.graph.slice(1)} element={gateDevtoolsRoute(<LazyGraph />)} />
          <Route path={panelRoutes.aiInput.slice(1)} element={gateDevtoolsRoute(<LazyAiInput />)} />
          <Route path={panelRoutes.apiMap.slice(1)} element={gateDevtoolsRoute(<LazyApiMap />)} />
          <Route path={panelRoutes.nodeManager.slice(1)} element={gateDevtoolsRoute(<LazyNodeManager />)} />
        </Route>
      </Routes>
      {routePending && (
        <div
          role="status"
          aria-label="Opening page"
          className="pointer-events-none fixed inset-x-0 top-0 z-[100] h-0.5 overflow-hidden bg-accent/15"
        >
          <div className="h-full w-full origin-left animate-pulse bg-accent" />
        </div>
      )}
    </>
  );
}
