import {Component, Suspense, type ErrorInfo, type ReactNode} from 'react';
import {useLocation} from 'react-router';
import {AlertCircle} from 'lucide-react';

import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';
import {
  createSafeCrashDiagnostic,
  reportSafeCrashDiagnostic,
} from '@/src/lib/crashRecovery';

function RouteSkeleton() {
  return (
    <div className="flex flex-1 flex-col gap-5 overflow-hidden p-6" role="status" aria-label="Loading page">
      <div className="h-7 w-52 animate-pulse rounded bg-bg-hover" />
      <div className="h-4 w-80 max-w-full animate-pulse rounded bg-bg-hover" />
      <div className="grid flex-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((item) => (
          <div key={item} className="min-h-40 animate-pulse rounded-xl border border-border bg-bg-card" />
        ))}
      </div>
    </div>
  );
}

class RouteLoadErrorBoundary extends Component<
  {children: ReactNode; routeKey: string},
  {failed: boolean; diagnosticReference: string}
> {
  state = {failed: false, diagnosticReference: ''};

  static getDerivedStateFromError() {
    return {failed: true};
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    const diagnostic = createSafeCrashDiagnostic(error, info.componentStack);
    reportSafeCrashDiagnostic(diagnostic);
    this.setState({diagnosticReference: diagnostic.reference});
  }

  componentDidUpdate(previous: {children: ReactNode; routeKey: string}) {
    if (previous.routeKey !== this.props.routeKey && this.state.failed) {
      this.setState({failed: false, diagnosticReference: ''});
    }
  }

  private returnHome = () => {
    window.history.replaceState({}, document.title, '/panel/');
    window.dispatchEvent(new PopStateEvent('popstate'));
    this.setState({failed: false, diagnosticReference: ''});
  };

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="w-full max-w-xl rounded-xl border border-red-200 bg-red-50 p-5 dark:border-red-900/40 dark:bg-red-950/20" role="alert">
          <h2 className="flex items-center gap-2 font-semibold text-text-main">
            <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0 text-destructive" data-error-icon="route-load" />
            This page could not be loaded
          </h2>
          <div className="mt-2 flex items-start gap-2">
            <p className="min-w-0 flex-1 text-sm text-text-muted">
              The current page stopped rendering. Raw error details are not displayed.
              {this.state.diagnosticReference ? ` Diagnostic: ${this.state.diagnosticReference}` : ''}
            </p>
            <CopyErrorButton
              label="Copy page load error"
              text={this.state.diagnosticReference ? `Page load diagnostic: ${this.state.diagnosticReference}` : 'Page load error'}
            />
          </div>
          <div className="mt-4 flex flex-wrap justify-end gap-2">
            <Button size="sm" variant="outline" onClick={() => this.setState({failed: false})}>
              Retry page
            </Button>
            <Button size="sm" onClick={this.returnHome}>
              Return Home
            </Button>
          </div>
        </div>
      </div>
    );
  }
}

export function RouteBoundary({children}: {children: ReactNode}) {
  const location = useLocation();
  const routeKey = `${location.pathname}${location.search}`;
  return (
    <RouteLoadErrorBoundary routeKey={routeKey}>
      <Suspense fallback={<RouteSkeleton />}>{children}</Suspense>
    </RouteLoadErrorBoundary>
  );
}
