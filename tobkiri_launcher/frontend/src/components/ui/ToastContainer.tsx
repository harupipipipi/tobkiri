import {useEffect, useRef, useState, type FocusEvent} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleAlert,
  Info,
  X,
  type LucideIcon,
} from 'lucide-react';

import {CopyErrorButton} from './CopyErrorButton';

export function ToastContainer() {
  const toasts = useAppStore(state => state.toasts);

  return (
    <article
      className={cn(
        'pointer-events-auto flex max-w-md items-center gap-3 rounded-md border px-4 py-3 text-sm text-white shadow-lg transition-all animate-in slide-in-from-bottom-5 motion-reduce:animate-none motion-reduce:transition-none',
        presentation.className,
      )}
      data-toast-id={toast.id}
      data-toast-paused={paused ? 'true' : 'false'}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setHovered(false)}
      onFocusCapture={() => {
        captureRemainingTime();
        setFocusWithin(true);
      }}
      onBlurCapture={handleBlur}
    >
      {toasts.map(toast => (
        <div
          key={toast.id}
          className={cn(
            "flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium shadow-[var(--shadow-lg)]",
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
