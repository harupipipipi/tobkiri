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

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('[ErrorBoundary] Caught error:', error, errorInfo);
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
        </div>
      );
    }

    return this.props.children;
  }
}
