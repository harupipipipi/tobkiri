/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { Component, type ReactNode, type ErrorInfo } from 'react';
import {AlertCircle} from 'lucide-react';

import {CopyErrorButton} from './CopyErrorButton';

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
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      const diagnostic = this.state.error?.message
        || '想定外の状態を検知しました。設定や作業内容をできるだけ保ったまま、再読み込みで復帰を試せます。';

      return (
        <div className="flex min-h-screen items-center justify-center bg-bg-main p-8">
          <div className="max-w-md text-center" role="alert">
            <h1 className="mb-4 flex items-center justify-center gap-2 text-2xl font-bold text-text-main"><AlertCircle aria-hidden="true" className="h-6 w-6 shrink-0 text-destructive" data-error-icon="rendering" />描画を安全に立て直しています</h1>
            <div className="mb-6 flex items-start gap-2 text-sm text-text-muted">
              <p className="min-w-0 flex-1 break-words">
                {diagnostic}
              </p>
              <CopyErrorButton
                label="Copy rendering error"
                text={diagnostic}
              />
            </div>
            <button
              onClick={() => {
                this.setState({ hasError: false, error: null });
                window.location.reload();
              }}
              className="px-4 py-2 bg-accent text-accent-fg rounded-md hover:opacity-90 transition-opacity text-sm font-medium"
            >
              もう一度ひらく
            </button>
          </div>
        </section>
      </main>
    );
  }
}
