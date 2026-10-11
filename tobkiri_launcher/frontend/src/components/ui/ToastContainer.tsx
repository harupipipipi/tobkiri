import { useAppStore } from '@/src/store';
import { cn } from '@/src/lib/utils';
import { viewerLayers } from '@/src/lib/layers';
import { CheckCircle2, XCircle } from 'lucide-react';

import {CopyErrorButton} from './CopyErrorButton';

export function ToastContainer() {
  const toasts = useAppStore(state => state.toasts);

  return (
    <div
      className={cn("pointer-events-none fixed inset-x-4 top-4 mx-auto flex w-auto max-w-xl flex-col gap-2", viewerLayers.toast)}
      aria-live="polite"
      aria-atomic="false"
      role="status"
    >
      {toasts.map(toast => (
        <div
          key={toast.id}
          className={cn(
            "toast-notice pointer-events-auto flex items-center gap-3 rounded-lg border px-4 py-3 text-sm font-medium shadow-[var(--shadow-lg)]",
            toast.type === 'success'
              ? 'border-success/35 bg-bg-card text-success'
              : 'border-destructive/35 bg-bg-card text-destructive'
          )}
          role="alert"
        >
          {toast.type === 'success' ? <CheckCircle2 aria-hidden="true" className="h-4 w-4 shrink-0" /> : <XCircle aria-hidden="true" className="h-4 w-4 shrink-0" />}
          <span className="min-w-0 flex-1 break-words">{toast.message}</span>
          {toast.type === 'error' ? (
            <CopyErrorButton
              label="Copy error notification"
              text={toast.message}
            />
          ) : null}
        </div>
      ))}
    </div>
  );
}
