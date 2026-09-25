/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import {Component, createRef, type ErrorInfo, type ReactNode} from 'react';

import {translate} from '@/src/lib/i18n';
import {
  crashDraftExport,
  createSafeCrashDiagnostic,
  recoverableDraftSnapshot,
  recordCrash,
  reportSafeCrashDiagnostic,
  resetAffectedClientState,
  type CrashDraftSnapshot,
  type SafeCrashDiagnostic,
} from '@/src/lib/crashRecovery';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

type DiagnosticStatus = 'saving' | 'saved' | 'not_saved';
type CopyStatus = 'idle' | 'copied' | 'failed';

interface State {
  hasError: boolean;
  diagnostic: SafeCrashDiagnostic | null;
  diagnosticStatus: DiagnosticStatus;
  copyStatus: CopyStatus;
  draft: CrashDraftSnapshot | null;
  crashCount: number;
}

export class ErrorBoundary extends Component<Props, State> {
  private headingRef = createRef<HTMLHeadingElement>();

  constructor(props: Props) {
    super(props);
    this.state = {
      hasError: false,
      diagnostic: null,
      diagnosticStatus: 'saving',
      copyStatus: 'idle',
      draft: null,
      crashCount: 0,
    };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return {
      hasError: true,
      diagnostic: createSafeCrashDiagnostic(error),
      diagnosticStatus: 'saving',
      copyStatus: 'idle',
      draft: null,
      crashCount: 0,
    };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    const diagnostic = createSafeCrashDiagnostic(error, errorInfo.componentStack);
    const saved = reportSafeCrashDiagnostic(diagnostic);
    this.setState({
      diagnostic,
      diagnosticStatus: saved ? 'saved' : 'not_saved',
      draft: recoverableDraftSnapshot(),
      crashCount: recordCrash(),
    });
  }

  componentDidMount(): void {
    if (this.state.hasError) this.headingRef.current?.focus();
  }

  componentDidUpdate(_previousProps: Props, previousState: State): void {
    if (!previousState.hasError && this.state.hasError) this.headingRef.current?.focus();
  }

  private retrySurface = (): void => {
    this.setState({
      hasError: false,
      diagnosticStatus: 'saving',
      copyStatus: 'idle',
      draft: null,
      crashCount: 0,
    });
  };

  private returnHome = (): void => {
    window.history.replaceState({}, document.title, '/panel/');
    window.dispatchEvent(new PopStateEvent('popstate'));
    this.retrySurface();
  };

  private resetSurface = (): void => {
    resetAffectedClientState();
    window.history.replaceState({}, document.title, '/panel/?recovery=reset');
    window.dispatchEvent(new PopStateEvent('popstate'));
    this.retrySurface();
  };

  private exportDraft = (): void => {
    if (!this.state.draft) return;
    const blob = new Blob([crashDraftExport(this.state.draft)], {type: 'application/json'});
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = href;
    anchor.download = 'tobkiri-recoverable-drafts.json';
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(href), 0);
  };

  private copyDiagnostic = async (): Promise<void> => {
    if (!this.state.diagnostic) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(this.state.diagnostic, null, 2));
      this.setState({copyStatus: 'copied'});
    } catch {
      this.setState({copyStatus: 'failed'});
    }
  };

  private diagnosticStatusCopy(): string {
    const reference = this.state.diagnostic?.reference ?? 'unavailable';
    if (this.state.diagnosticStatus === 'saved') {
      return translate('recovery.diagnostic_saved', {reference});
    }
    if (this.state.diagnosticStatus === 'not_saved') {
      return translate('recovery.diagnostic_not_saved', {reference});
    }
    return translate('recovery.diagnostic_saving', {reference});
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    if (this.props.fallback) return this.props.fallback;

    const repeated = this.state.crashCount >= 2;
    return (
      <main
        aria-labelledby="viewer-recovery-heading"
        className="flex min-h-screen items-center justify-center bg-bg-main p-6"
      >
        <section className="w-full max-w-2xl rounded-2xl border border-border bg-bg-card p-6 shadow-xl">
          <p className="text-xs font-semibold uppercase tracking-wider text-destructive">
            {translate('recovery.eyebrow')}
          </p>
          <h1
            id="viewer-recovery-heading"
            ref={this.headingRef}
            tabIndex={-1}
            className="mt-3 text-2xl font-bold text-text-main outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]"
          >
            {translate('recovery.heading')}
          </h1>
          <p className="mt-3 text-sm leading-6 text-text-muted" role="alert">
            {translate('recovery.description')}
          </p>
          <p className="mt-3 rounded-lg border border-border bg-bg-main p-3 text-sm text-text-muted" role="status">
            {this.diagnosticStatusCopy()}
          </p>
          {repeated ? (
            <p className="mt-3 rounded-lg border border-amber-300/70 bg-amber-50/70 p-3 text-sm text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-100" role="alert">
              {translate('recovery.repeated', {count: String(this.state.crashCount)})}
            </p>
          ) : null}
          {this.state.draft ? (
            <div className="mt-3 rounded-lg border border-sky-300/70 bg-sky-50/70 p-3 text-sm text-sky-900 dark:border-sky-800/60 dark:bg-sky-950/20 dark:text-sky-100">
              <p>{translate('recovery.draft_saved', {count: String(this.state.draft.drafts.length)})}</p>
              <button type="button" onClick={this.exportDraft} className="mt-2 min-h-11 underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
                {translate('recovery.export_draft')}
              </button>
            </div>
          ) : (
            <p className="mt-3 text-sm text-text-muted">{translate('recovery.no_draft')}</p>
          )}
          <details className="mt-4 text-sm text-text-muted">
            <summary className="min-h-11 cursor-pointer py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
              {translate('recovery.technical_details')}
            </summary>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-bg-main p-3 text-xs">
              {JSON.stringify(this.state.diagnostic ?? {code: 'viewer.render_crash'}, null, 2)}
            </pre>
            <button type="button" onClick={() => void this.copyDiagnostic()} className="mt-2 min-h-11 rounded-md border border-border px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
              {translate('recovery.copy_diagnostic')}
            </button>
            {this.state.copyStatus !== 'idle' ? (
              <span className="ml-3" role="status">
                {translate(this.state.copyStatus === 'copied' ? 'recovery.copy_succeeded' : 'recovery.copy_failed')}
              </span>
            ) : null}
          </details>
          <div className="mt-6 flex flex-wrap gap-2">
            <button type="button" onClick={this.retrySurface} className="min-h-11 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
              {translate('recovery.retry')}
            </button>
            <button type="button" onClick={this.returnHome} className="min-h-11 rounded-md border border-border px-4 py-2 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
              {translate('recovery.home')}
            </button>
            <button type="button" onClick={this.resetSurface} className="min-h-11 rounded-md border border-amber-400/60 px-4 py-2 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
              {translate('recovery.reset')}
            </button>
            <button type="button" onClick={() => window.location.reload()} className="min-h-11 rounded-md border border-border px-4 py-2 text-sm text-text-main focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring-color)]">
              {translate('recovery.reload')}
            </button>
          </div>
        </section>
      </main>
    );
  }
}
