import {Component, Suspense, type ErrorInfo, type ReactNode} from 'react';
import {useLocation} from 'react-router';
import {AlertCircle} from 'lucide-react';

import {Button} from '@/src/components/ui/Button';
import {CopyErrorButton} from '@/src/components/ui/CopyErrorButton';

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
  {error: Error | null}
> {
  state = {error: null as Error | null};

  static getDerivedStateFromError(error: Error) {
    return {error};
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Route render failed', {error, componentStack: info.componentStack});
  }

  componentDidUpdate(previous: {children: ReactNode; routeKey: string}) {
    if (previous.routeKey !== this.props.routeKey && this.state.error) {
      this.setState({error: null});
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="w-full max-w-xl rounded-xl border border-red-200 bg-red-50 p-5 dark:border-red-900/40 dark:bg-red-950/20" role="alert">
          <h2 className="flex items-center gap-2 font-semibold text-text-main"><AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0 text-destructive" data-error-icon="route-load" />This page could not be loaded</h2>
          <div className="mt-2 flex items-start gap-2">
            <p className="min-w-0 flex-1 break-words text-sm text-text-muted">{this.state.error.message}</p>
            <CopyErrorButton label="Copy page load error" text={this.state.error.message} />
          </div>
          <div className="mt-4 flex justify-end">
            <Button size="sm" onClick={() => window.location.reload()}>Reload page</Button>
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
