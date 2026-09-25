import {useEffect, useRef, useState, type FocusEvent} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleAlert,
  Info,
  X,
  type LucideIcon,
} from 'lucide-react';

import {viewerLayers} from '@/src/lib/layers';
import {cn} from '@/src/lib/utils';
import {useAppStore, type Toast, type ToastType} from '@/src/store';

import {CopyErrorButton} from './CopyErrorButton';

const TOAST_PRESENTATION: Record<ToastType, {
  Icon: LucideIcon;
  label: string;
  className: string;
}> = {
  success: {Icon: CheckCircle2, label: 'Success', className: 'border-green-300 bg-green-700'},
  info: {Icon: Info, label: 'Information', className: 'border-sky-300 bg-sky-700'},
  warning: {Icon: AlertTriangle, label: 'Warning', className: 'border-amber-200 bg-amber-700'},
  error: {Icon: CircleAlert, label: 'Error', className: 'border-red-300 bg-red-700'},
};

function ToastCard({toast}: {toast: Toast}) {
  const removeToast = useAppStore((state) => state.removeToast);
  const [hovered, setHovered] = useState(false);
  const [focusWithin, setFocusWithin] = useState(false);
  const [actionPending, setActionPending] = useState(false);
  const [actionFailed, setActionFailed] = useState(false);
  const [remainingMs, setRemainingMs] = useState(toast.durationMs);
  const deadlineRef = useRef(0);
  const presentation = TOAST_PRESENTATION[toast.type];
  const ToastIcon = presentation.Icon;
  const isUrgent = toast.type === 'error';
  const paused = hovered || focusWithin || actionPending || actionFailed;

  useEffect(() => {
    setRemainingMs(toast.durationMs);
    setHovered(false);
    setFocusWithin(false);
    setActionFailed(false);
  }, [toast.durationMs, toast.id, toast.revision]);

  useEffect(() => {
    if (toast.persistent || paused) return undefined;
    deadlineRef.current = Date.now() + remainingMs;
    const timer = window.setTimeout(() => removeToast(toast.id), remainingMs);
    return () => window.clearTimeout(timer);
  }, [paused, remainingMs, removeToast, toast.id, toast.persistent]);

  const captureRemainingTime = () => {
    if (!toast.persistent && !paused) {
      setRemainingMs(Math.max(0, deadlineRef.current - Date.now()));
    }
  };

  const handleMouseEnter = () => {
    captureRemainingTime();
    setHovered(true);
  };

  const handleBlur = (event: FocusEvent<HTMLElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setFocusWithin(false);
  };

  const runAction = async () => {
    if (!toast.action || actionPending) return;
    setActionFailed(false);
    setActionPending(true);
    try {
      await toast.action.onAction();
      removeToast(toast.id);
    } catch {
      setActionPending(false);
      setActionFailed(true);
    }
  };

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
      <ToastIcon size={20} className="shrink-0" aria-hidden="true" />
      <div
        className="min-w-0 flex-1"
        role={isUrgent ? 'alert' : 'status'}
        aria-live={isUrgent ? 'assertive' : 'polite'}
        aria-atomic="true"
      >
        <span className="font-semibold">{presentation.label}: </span>
        <span>{toast.message}</span>
        {actionFailed ? (
          <span className="block font-medium">Action failed. Try again or dismiss.</span>
        ) : null}
      </div>
      {toast.action ? (
        <button
          type="button"
          disabled={actionPending}
          onClick={() => void runAction()}
          className="shrink-0 rounded px-2 py-1 font-semibold underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white disabled:opacity-70"
        >
          {actionPending
            ? 'Working…'
            : actionFailed
              ? `Retry ${toast.action.label}`
              : toast.action.label}
        </button>
      ) : null}
      {toast.type === 'error' ? (
        <CopyErrorButton
          label="Copy error notification"
          text={toast.message}
        />
      ) : null}
      <button
        type="button"
        onClick={() => removeToast(toast.id)}
        className="shrink-0 rounded p-1 hover:bg-black/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
        aria-label={`Dismiss ${presentation.label.toLowerCase()} notification`}
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </article>
  );
}

/** Render the bounded notification queue without introducing a nested live region. */
export function ToastContainer() {
  const toasts = useAppStore((state) => state.toasts);

  return (
    <section
      className={cn(
        'pointer-events-none fixed bottom-4 right-4 flex max-w-[calc(100vw-2rem)] flex-col gap-2',
        viewerLayers.toast,
      )}
      aria-label="Notifications"
    >
      {toasts.map((toast) => <ToastCard key={toast.id} toast={toast} />)}
    </section>
  );
}
