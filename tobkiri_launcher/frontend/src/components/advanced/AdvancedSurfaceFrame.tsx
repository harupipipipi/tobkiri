import type {ReactNode} from 'react';
import {AlertTriangle, CheckCircle2, Clock3, RefreshCw, ShieldAlert} from 'lucide-react';

import {Badge} from '@/src/components/ui/Badge';
import {Button} from '@/src/components/ui/Button';
import {TobkiriLoadingMark} from '@/src/components/ui/TobkiriLoader';
import {
  advancedActionMetadata,
  type LauncherAdvancedAction,
  type LauncherAdvancedViewDescriptor,
  type LauncherViewSupport,
} from '@/src/lib/advancedSurfaces';
import type {RuntimeSurfaceLoadStatus} from '@/src/hooks/useRuntimeSurface';
import type {RuntimeSurfaceErrorCode} from '@/src/lib/runtimeSurface';
import {cn} from '@/src/lib/utils';

export interface SurfaceStateNotice {
  status: RuntimeSurfaceLoadStatus;
  stale: boolean;
  error: {code: RuntimeSurfaceErrorCode; message: string} | null;
}

function supportVariant(support: LauncherViewSupport): 'default' | 'secondary' | 'outline' | 'success' | 'warning' {
  if (support === 'mapped' || support === 'rebuilt') return 'success';
  if (support === 'partial' || support === 'launcher_local') return 'warning';
  return 'secondary';
}

function supportLabel(support: LauncherViewSupport, action: LauncherAdvancedAction): string {
  if (support === 'launcher_local') return 'Launcher local';
  if (support === 'rebuilt') return 'Rebuilt';
  if (support === 'mapped') return 'Mapped';
  if (support === 'partial' && action === 'contract_invoke') return 'Partial / contract-invokable';
  if (support === 'partial') return 'Partial / read-only';
  return 'Retired / contract required';
}

function actionVariant(action: LauncherAdvancedAction): 'default' | 'secondary' | 'outline' | 'success' | 'warning' {
  if (action === 'contract_invoke' || action === 'pack_lifecycle') return 'warning';
  if (action === 'read_only' || action === 'none') return 'secondary';
  return 'outline';
}

function actionStateCopy(
  action: LauncherAdvancedAction,
  status: string,
  stale: boolean,
): string {
  if (action === 'contract_invoke') {
    return status === 'ready' && !stale
      ? 'Only authoritative invokable Contract operations can be submitted from this surface.'
      : 'Contract invocation controls are locked until a fresh authoritative operation snapshot is accepted.';
  }
  if (action === 'read_only') return 'No runtime invocation controls are available on this read-only projection.';
  if (action === 'pack_lifecycle') return 'Pack lifecycle controls are shown only when their authoritative catalog bindings are current.';
  if (action === 'local') return 'Only Launcher-local presentation controls are available here.';
  return 'No actions are exposed by this surface.';
}

function StatusNotice({
  descriptor,
  state,
  onRetry,
}: {
  descriptor: LauncherAdvancedViewDescriptor;
  state: SurfaceStateNotice;
  onRetry: () => void;
}): ReactNode {
  if (state.status === 'loading' && !state.stale) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-border bg-bg-card px-4 py-4 text-sm text-text-muted" role="status" aria-live="polite">
        <TobkiriLoadingMark />
        Loading the canonical v4 projection…
      </div>
    );
  }
  if (state.status === 'ready' || state.status === 'idle') return null;

  const isBlocked = state.status === 'digest_mismatch'
    || state.status === 'approval_denied'
    || state.status === 'stale'
    || state.status === 'profile_not_active';
  const title = state.status === 'unavailable'
    ? 'Canonical v4 surface is not published'
    : state.status === 'profile_not_active'
      ? 'Active Profile is unavailable'
    : state.status === 'timeout'
      ? 'Canonical v4 request timed out'
      : isBlocked
        ? advancedActionMetadata(descriptor).action === 'read_only'
          ? 'Surface is read-only and fail-closed'
          : 'Action is locked and fail-closed'
        : 'Canonical v4 surface could not be loaded';
  const message = state.error?.message ?? 'No new data was accepted.';

  return (
    <div
      className={cn(
        'flex flex-wrap items-start justify-between gap-3 rounded-xl border px-4 py-4',
        isBlocked || state.status === 'approval_denied'
          ? 'border-amber-300/70 bg-amber-50/70 dark:border-amber-800/60 dark:bg-amber-950/20'
          : 'border-destructive/40 bg-destructive/5',
      )}
      role="alert"
    >
      <div className="flex min-w-0 items-start gap-3">
        {state.status === 'timeout' ? <Clock3 className="mt-0.5 size-5 shrink-0 text-amber-600" aria-hidden="true" /> : isBlocked ? <ShieldAlert className="mt-0.5 size-5 shrink-0 text-amber-600" aria-hidden="true" /> : <AlertTriangle className="mt-0.5 size-5 shrink-0 text-destructive" aria-hidden="true" />}
        <div className="min-w-0">
          <p className="text-sm font-semibold text-text-main">{title}</p>
          <p className="mt-1 text-sm leading-6 text-text-muted">{message}</p>
          {state.stale ? <p className="mt-1 text-xs text-text-muted">Showing the last accepted snapshot. Actions are disabled until the authoritative surface is fresh.</p> : null}
        </div>
      </div>
      <Button type="button" variant="outline" size="sm" onClick={onRetry} disabled={state.status === 'loading'}>
        <RefreshCw className="h-4 w-4" aria-hidden="true" />
        Retry
      </Button>
    </div>
  );
}

export function AdvancedSurfaceFrame({
  descriptor,
  state,
  onRetry,
  children,
  className,
}: {
  descriptor: LauncherAdvancedViewDescriptor;
  state: SurfaceStateNotice;
  onRetry: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex-1 overflow-y-auto page-enter', className)}>
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6 lg:p-8">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight text-text-main">{descriptor.label}</h1>
              <Badge variant={supportVariant(descriptor.support)}>{supportLabel(descriptor.support, descriptor.actions)}</Badge>
            </div>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-text-muted">{descriptor.summary}</p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onRetry} disabled={state.status === 'loading'}>
            {state.status === 'loading' ? <TobkiriLoadingMark /> : <RefreshCw className="h-4 w-4" aria-hidden="true" />}
            Refresh
          </Button>
        </header>

        <section
          className="rounded-xl border border-border bg-bg-card px-4 py-4 sm:px-5"
          aria-label={`${descriptor.label} capability and action metadata`}
          data-advanced-action={descriptor.actions}
        >
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={actionVariant(descriptor.actions)}>Action: {descriptor.actions}</Badge>
            <Badge variant="outline">Capability: {descriptor.capability}</Badge>
          </div>
          <p className="mt-2 text-sm leading-6 text-text-muted">{advancedActionMetadata(descriptor).sideEffects}</p>
          <p className="mt-1 text-sm leading-6 text-text-muted">{advancedActionMetadata(descriptor).approval}</p>
          <p className="mt-1 text-xs leading-5 text-text-muted">{actionStateCopy(descriptor.actions, state.status, state.stale)}</p>
        </section>

        <StatusNotice descriptor={descriptor} state={state} onRetry={onRetry} />
        {state.status === 'ready' && !state.error ? (
          <div className="flex items-center gap-2 text-xs text-emerald-700 dark:text-emerald-300" role="status" aria-live="polite">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            Canonical v4 projection accepted. {actionStateCopy(descriptor.actions, state.status, state.stale)}
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}

export function EmptySurfacePanel({
  title,
  message,
  icon,
}: {
  title: string;
  message: string;
  icon?: ReactNode;
}) {
  return (
    <section className="flex min-h-48 flex-col items-center justify-center rounded-xl border border-dashed border-border bg-bg-card px-5 py-10 text-center">
      {icon ? <div className="mb-3 text-text-muted" aria-hidden="true">{icon}</div> : null}
      <h2 className="text-base font-semibold text-text-main">{title}</h2>
      <p className="mt-2 max-w-xl text-sm leading-6 text-text-muted">{message}</p>
    </section>
  );
}
