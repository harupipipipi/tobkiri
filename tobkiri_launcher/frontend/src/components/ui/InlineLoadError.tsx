import { AlertTriangle, RefreshCw } from 'lucide-react';

import { Button } from './Button';
import {CopyErrorButton} from './CopyErrorButton';
import { TobkiriLoadingMark } from './TobkiriLoader';

interface InlineLoadErrorProps {
  title: string;
  message: string;
  onRetry: () => void;
  retrying?: boolean;
  stale?: boolean;
}

export function InlineLoadError({
  title,
  message,
  onRetry,
  retrying = false,
  stale = false,
}: InlineLoadErrorProps) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-start justify-between gap-3 border border-destructive/40 bg-destructive/5 p-4"
    >
      <div className="flex min-w-0 items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <div className="min-w-0">
          <div className="text-sm font-semibold text-text-main">{title}</div>
          <p className="mt-1 break-words text-sm text-text-muted">{message}</p>
          {stale ? <p className="mt-1 text-xs text-text-muted">Showing the last successfully loaded data.</p> : null}
        </div>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry} disabled={retrying}>
        {retrying ? <TobkiriLoadingMark /> : <RefreshCw className="h-4 w-4" />}
        Retry
      </Button>
      <CopyErrorButton label={`Copy ${title} error`} text={message} />
    </div>
  );
}
