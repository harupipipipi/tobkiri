import {useState} from 'react';
import {AlertTriangle, CircleAlert, RefreshCw, ShieldCheck} from 'lucide-react';
import {Button} from '@/src/components/ui/Button';
import type {SetupVerificationState} from '@/src/lib/setupVerification';

interface SetupVerificationNoticeProps {
  state: SetupVerificationState;
  onRetry: () => void;
  onReauthorize: () => void;
}

function unavailableCopy(state: Extract<SetupVerificationState, {kind: 'unavailable'}>): string {
  switch (state.reason) {
    case 'offline':
      return 'Tobkiri appears offline. Your completed setup is preserved.';
    case 'timeout':
      return 'The runtime did not answer before the verification timeout. Your completed setup is preserved.';
    case 'runtime':
      return 'The runtime is temporarily unavailable. Your completed setup is preserved.';
  }
}

/** Show recoverable setup verification inside the existing Launcher shell. */
export function SetupVerificationNotice({
  state,
  onRetry,
  onReauthorize,
}: SetupVerificationNoticeProps) {
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  if (state.kind === 'selected' && state.source === 'backend') return null;
  if (state.kind === 'missing') return null;

  const pending = state.kind === 'unknown'
    || state.kind === 'loading'
    || (state.kind === 'selected' && state.source === 'cache');
  const diagnosticReference = 'diagnosticReference' in state
    ? state.diagnosticReference
    : null;
  const cached = state.kind === 'selected' ? state.binding : state.cached;
  const title = pending
    ? 'Verifying completed setup'
    : state.kind === 'reauth_required'
      ? 'Panel session expired'
      : state.kind === 'unavailable'
        ? 'Setup verification is unavailable'
        : state.kind === 'malformed'
          ? 'Setup verification returned an invalid response'
          : 'Setup verification could not be confirmed';
  const detail = pending
    ? 'The Launcher remains available while Tobkiri checks the selected Defaults Profile.'
    : state.kind === 'reauth_required'
      ? 'Reauthorize the local panel session, then verify again. Your completed setup is preserved.'
      : state.kind === 'unavailable'
        ? unavailableCopy(state)
        : 'The response was not authoritative, so no setup or permission state was changed.';

  return (
    <section
      aria-labelledby="setup-verification-title"
      aria-live={pending ? 'polite' : 'assertive'}
      className={`border-b px-6 py-3 text-sm ${pending
        ? 'border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-900/40 dark:bg-sky-950/20 dark:text-sky-200'
        : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-200'}`}
    >
      <div className="flex flex-wrap items-center gap-3">
        {pending
          ? <ShieldCheck className="h-4 w-4 shrink-0" aria-hidden="true" />
          : state.kind === 'malformed'
            ? <CircleAlert className="h-4 w-4 shrink-0" aria-hidden="true" />
            : <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />}
        <div className="min-w-0 flex-1">
          <p id="setup-verification-title" className="font-medium">{title}</p>
          <p className="text-xs opacity-85">{detail}</p>
        </div>
        {!pending && (
          <div className="flex flex-wrap items-center gap-2">
            {state.kind === 'reauth_required' && (
              <Button size="sm" onClick={onReauthorize}>Reauthorize</Button>
            )}
            <Button size="sm" variant="outline" onClick={onRetry}>
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Retry
            </Button>
            <Button
              size="sm"
              variant="ghost"
              aria-expanded={showDiagnostics}
              aria-controls="setup-verification-diagnostics"
              onClick={() => setShowDiagnostics((visible) => !visible)}
            >
              Diagnostics
            </Button>
          </div>
        )}
      </div>
      {showDiagnostics && diagnosticReference && (
        <dl
          id="setup-verification-diagnostics"
          className="mt-3 grid gap-1 rounded-md border border-current/20 px-3 py-2 text-xs"
        >
          <div className="flex gap-2"><dt>Reference</dt><dd className="font-mono">{diagnosticReference}</dd></div>
          <div className="flex min-w-0 gap-2">
            <dt>Last verified Profile</dt>
            <dd className="truncate font-mono">{cached?.profileRevision ?? 'not cached'}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}
