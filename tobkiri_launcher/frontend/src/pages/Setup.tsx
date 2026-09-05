import {useCallback, useEffect, useRef, useState} from 'react';
import {useNavigate} from 'react-router';
import {AlertCircle} from 'lucide-react';
import {useAppStore} from '@/src/store';
import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {PresentationSelector} from '@/src/components/presentation/PresentationSelector';
import {TobkiriLoader, TobkiriLoadingMark} from '@/src/components/ui/TobkiriLoader';
import {panelRoutes} from '@/src/lib/routes';
import {
  activateDefaultsProfile,
  fetchDefaultsSetupState,
  type DefaultsSetupState,
} from '@/src/lib/defaultsSetup';
import {
  activateDefaultsWithRecovery,
  recoverDefaultsActivation,
} from '@/src/lib/defaultsActivationRecovery';
import {
  fetchPresentationState,
  isDesktopShellAvailable,
  launchSelectedPresentation,
  reconcileDefaultsRuntime,
  selectPresentation,
} from '@/src/lib/api';
import {refreshMountedRuntimeSurfaces} from '@/src/lib/runtimeSurfaceRefresh';
import type {ApiPresentationSelection, ApiPresentationState} from '@/src/lib/apiTypes';
import {
  defaultPresentationSelection,
  normalizePresentationSelection,
} from '@/src/lib/presentation';
import {LAUNCHER_DISPLAY_NAME} from '@/src/lib/launcherBrand';
import {formatPackVMRecoveryError} from '@/src/lib/packvmLifecycle';
import {DefaultsReview} from './DefaultsReview';

function message(error: unknown, fallback: string): string {
  if (typeof error === 'string' && error.trim()) return error;
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

export function Setup() {
  const navigate = useNavigate();
  const setSetupDone = useAppStore((state) => state.setSetupDone);
  const addToast = useAppStore((state) => state.addToast);
  const runtimeStatus = useAppStore((state) => state.runtimeStatus);
  const refreshRuntimeHealth = useAppStore((state) => state.refreshRuntimeHealth);
  const refreshPackVMDoctor = useAppStore((state) => state.refreshPackVMDoctor);
  const loadPacks = useAppStore((state) => state.loadPacks);
  const loadFrontendCatalog = useAppStore((state) => state.loadFrontendCatalog);
  const [setup, setSetup] = useState<DefaultsSetupState | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [activating, setActivating] = useState(false);
  const [activationCommitted, setActivationCommitted] = useState(false);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [reconciliationError, setReconciliationError] = useState<string | null>(null);
  const [presentation, setPresentation] = useState<ApiPresentationState | null>(null);
  const [selection, setSelection] = useState<ApiPresentationSelection | null>(null);
  const [presentationLoading, setPresentationLoading] = useState(false);
  const [presentationSaving, setPresentationSaving] = useState(false);
  const [presentationLaunching, setPresentationLaunching] = useState(false);
  const [presentationError, setPresentationError] = useState<string | null>(null);
  const [complete, setComplete] = useState(false);
  const activationInFlightRef = useRef(false);
  const profileReconfirmationRequired = runtimeStatus === 'profile_reconfirmation_required';
  const desktopShell = isDesktopShellAvailable();

  const completeBrowserSetup = useCallback(() => {
    setSetupDone(true);
    navigate(panelRoutes.home, {replace: true});
  }, [navigate, setSetupDone]);

  const loadPresentation = useCallback(async () => {
    setPresentationLoading(true);
    setPresentationError(null);
    setPresentation(null);
    setSelection(null);
    try {
      const next = await fetchPresentationState();
      setPresentation(next);
      setSelection(
        normalizePresentationSelection(next.catalog, next.selection)
          ?? defaultPresentationSelection(next.catalog),
      );
    } catch (error) {
      setPresentation(null);
      setSelection(null);
      setPresentationError(message(error, 'Presentation catalog could not be loaded.'));
    } finally {
      setPresentationLoading(false);
    }
  }, []);

  useEffect(() => {
    let live = true;
    void fetchDefaultsSetupState()
      .then((next) => {
        if (!live) return;
        setSetup(next);
        setActivationCommitted(next.state === 'active');
        if (next.state === 'active') {
          if (desktopShell) void loadPresentation();
          else completeBrowserSetup();
        }
      })
      .catch((error) => {
        if (live) setSetupError(message(error, 'Defaults Profile could not be loaded.'));
      });
    return () => { live = false; };
  }, [completeBrowserSetup, desktopShell, loadPresentation]);

  const reconcileActiveRuntime = useCallback(async () => {
    await reconcileDefaultsRuntime();
    await refreshRuntimeHealth();
    const runtimeState = useAppStore.getState();
    if (runtimeState.runtimeStatus !== 'runtime_ready') {
      throw new Error(formatPackVMRecoveryError(
        runtimeState.runtimeError,
        'Defaults activation completed without a verified runtime dispatch map.',
      ));
    }

    // Setup owns the authoritative sequence below. The store's normal doctor
    // refresh may hydrate projections for PackVM pages, but doing that here
    // would issue a second load before this reconciliation has verified health.
    const packVmDoctor = await refreshPackVMDoctor({reconcile: false});
    if (!packVmDoctor) {
      const currentState = useAppStore.getState();
      throw new Error(formatPackVMRecoveryError(
        currentState.packVmError,
        'PackVM readiness could not be verified.',
      ));
    }

    // A valid not-ready doctor is the expected fresh-install state before the
    // user provisions PackVM from Packs.  Keep Pack operations fail-closed via
    // the recorded doctor while allowing the authenticated provisioning UI to
    // become reachable.

    await loadPacks(false, {skipMutationReconciliation: true});
    if (packVmDoctor.ready) {
      await loadFrontendCatalog(false);
    }
    const refreshedState = useAppStore.getState();
    const projectionError = refreshedState.packsError
      || (packVmDoctor.ready ? refreshedState.frontendCatalogError : null);
    if (projectionError) {
      throw new Error(formatPackVMRecoveryError(
        projectionError,
        'Authoritative Pack projections could not be reconciled.',
      ));
    }
    try {
      await refreshMountedRuntimeSurfaces();
    } catch (error) {
      throw new Error(formatPackVMRecoveryError(
        error,
        'Mounted runtime surfaces could not be reconciled.',
      ));
    }
  }, [loadFrontendCatalog, loadPacks, refreshMountedRuntimeSurfaces, refreshPackVMDoctor, refreshRuntimeHealth]);

  const applyRecoveryResult = useCallback(async (
    result: Awaited<ReturnType<typeof recoverDefaultsActivation>>,
  ) => {
    setSetup(result.state);
    setReviewed(false);
    setActivationCommitted(result.activationCommitted);
    const failure = result.error
      ? message(result.error, 'Defaults activation reconciliation failed.')
      : null;
    if (result.state?.state === 'active') {
      setSetupError(null);
      setReconciliationError(failure);
      if (failure) {
        addToast(failure, 'error');
      } else if (!desktopShell) {
        completeBrowserSetup();
      } else {
        await loadPresentation();
      }
      return;
    }
    setReconciliationError(null);
    setSetupError(failure);
    if (failure) addToast(failure, 'error');
  }, [addToast, completeBrowserSetup, desktopShell, loadPresentation]);

  const recoverActivation = useCallback(async () => {
    if (activationInFlightRef.current) return;
    activationInFlightRef.current = true;
    setActivating(true);
    setSetupError(null);
    try {
      const result = await recoverDefaultsActivation({
        fetchAuthoritativeSetup: fetchDefaultsSetupState,
        reconcileActiveRuntime,
      });
      await applyRecoveryResult(result);
    } finally {
      activationInFlightRef.current = false;
      setActivating(false);
    }
  }, [applyRecoveryResult, reconcileActiveRuntime]);

  const activate = async () => {
    if (activationCommitted || !setup || setup.state !== 'review_required' || !reviewed) return;
    if (activationInFlightRef.current) return;
    activationInFlightRef.current = true;
    setActivating(true);
    setSetupError(null);
    try {
      const result = await activateDefaultsWithRecovery({
        submitActivation: () => activateDefaultsProfile(setup.recommended_default_profile.confirmation),
        fetchAuthoritativeSetup: fetchDefaultsSetupState,
        reconcileActiveRuntime,
      });
      await applyRecoveryResult(result);
    } finally {
      activationInFlightRef.current = false;
      setActivating(false);
    }
  };

  const savePresentation = async (nextSelection: ApiPresentationSelection) => {
    setPresentationSaving(true);
    setPresentationError(null);
    try {
      const next = await selectPresentation(nextSelection);
      setPresentation(next);
      setSelection(next.selection ?? nextSelection);
      setSetupDone(true);
      addToast('Presentation selection saved. The verified Shell is ready to launch.', 'success');
    } catch (error) {
      setPresentationError(message(error, 'Presentation selection could not be saved.'));
    } finally {
      setPresentationSaving(false);
    }
  };

  const launchPresentation = async () => {
    setPresentationLaunching(true);
    setPresentationError(null);
    try {
      const result = await launchSelectedPresentation();
      addToast(result.message || 'Selected Shell launched.', 'success');
      setComplete(true);
      window.setTimeout(() => navigate(panelRoutes.home), 500);
    } catch (error) {
      setPresentationError(message(error, 'Selected Shell launch was blocked.'));
    } finally {
      setPresentationLaunching(false);
    }
  };

  if (complete) {
    return <TobkiriLoader
      scope="screen"
      scene="startup"
      label="Runtime ready. Opening Tobkiri Launcher…"
    />;
  }

  if (setup?.state === 'active') {
    return <div className="min-h-screen bg-bg-main px-6 py-10"><div className="mx-auto max-w-4xl">
      <Header />
      {reconciliationError ? <div role="alert" className="rounded-xl border border-red-500/30 bg-red-500/10 p-6 text-sm text-red-500">
        <p className="flex items-center gap-2 font-medium text-text-main"><AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0 text-destructive" />Activation is verified; runtime surfaces need reconciliation.</p>
        <div className="mt-2 flex items-start gap-2">
          <p className="min-w-0 flex-1 break-words">{reconciliationError}</p>
          <CopyErrorButton label="Copy runtime reconciliation error" text={reconciliationError} />
        </div>
        <div className="mt-4"><Button variant="outline" onClick={() => void recoverActivation()} loading={activating}>Retry runtime reconciliation</Button></div>
      </div> : presentationLoading ? <div role="status" aria-busy="true" className="flex items-center gap-2 rounded-xl border border-border bg-bg-card p-6 text-sm text-text-muted">
        <TobkiriLoadingMark />
        Loading selected presentation…
      </div> : presentationError ? <div role="alert" className="rounded-xl border border-destructive/40 bg-destructive/5 p-6 text-sm text-destructive">
        <p>{presentationError}</p>
        <div className="mt-4"><Button variant="outline" onClick={() => void loadPresentation()}>Retry</Button></div>
      </div> : presentation ? <PresentationSelector
        state={presentation}
        selection={selection}
        saving={presentationSaving}
        launching={presentationLaunching}
        error={presentationError}
        onSelectionChange={setSelection}
        onSave={savePresentation}
        onLaunch={launchPresentation}
      /> : <div role={presentationError ? 'alert' : 'status'} className="rounded-xl border border-border bg-bg-card p-6 text-sm text-text-muted">
        <div className="flex items-start gap-2">
          {presentationError ? <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" /> : null}
          <span className="min-w-0 flex-1 break-words">{presentationError ?? 'Loading selected presentation…'}</span>
          {presentationError ? <CopyErrorButton label="Copy presentation error" text={presentationError} /> : null}
        </div>
        {presentationError && <div className="mt-4"><Button variant="outline" onClick={() => void loadPresentation()} loading={presentationLoading}>Retry</Button></div>}
      </div>}
    </div></div>;
  }

  return <div className="min-h-screen bg-bg-main px-6 py-10"><div className="mx-auto max-w-3xl">
    <Header />
    <DefaultsReview
      setup={setup}
      reviewed={reviewed}
      activating={activating}
      activationCommitted={activationCommitted}
      error={setupError}
      reconfirmationRequired={profileReconfirmationRequired}
      onRecover={() => void recoverActivation()}
      onReviewedChange={setReviewed}
      onActivate={() => void activate()}
    />
  </div></div>;
}

function Header() {
  return <div className="mb-8 flex items-center gap-3 text-sm font-semibold text-text-main">
    <img src="/panel/assets/tobkiri-launcher-icon.png" alt="Tobkiri" data-asset-trust="bundled" className="h-9 w-9 rounded-lg border border-border" />
    {LAUNCHER_DISPLAY_NAME}
  </div>;
}
