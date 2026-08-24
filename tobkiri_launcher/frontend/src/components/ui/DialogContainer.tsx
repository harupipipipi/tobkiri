import { useEffect, useRef, useCallback, useState } from 'react';
import {AlertCircle} from 'lucide-react';
import { useAppStore } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { viewerLayers } from '@/src/lib/layers';
import { cn } from '@/src/lib/utils';
import { Button } from './Button';
import {CopyErrorButton} from './CopyErrorButton';
import { formatUserFacingError } from '@/src/lib/userFacingError';

export function DialogContainer() {
  const t = useT();
  const dialog = useAppStore(state => state.dialog);
  const closeDialog = useAppStore(state => state.closeDialog);
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const stateRef = useRef<DialogConfirmationState>({status: 'idle'});
  const attemptRef = useRef(0);
  const errorRef = useRef<HTMLDivElement>(null);
  const [confirmationState, setConfirmationState] = useState<DialogConfirmationState>({status: 'idle'});
  const [copied, setCopied] = useState(false);
  stateRef.current = confirmationState;
  const isConfirming = confirmationState.status === 'pending';
  const isConflictRefresh = confirmationState.status === 'pending'
    && confirmationState.phase === 'conflict_refresh';
  const failure = 'failure' in confirmationState ? confirmationState.failure : null;
  const failureSource = 'source' in confirmationState ? confirmationState.source : null;

  useEffect(() => {
    attemptRef.current += 1;
    if (!dialog) {
      setConfirmationState({status: 'idle'});
      setCopied(false);
      return;
    }

    setConfirmationState({status: 'idle'});
    setCopied(false);
    previousFocusRef.current = document.activeElement as HTMLElement | null;

    const timer = setTimeout(() => {
      dialogRef.current?.focus();
    }, 0);

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (stateRef.current.status !== 'pending') {
          closeDialog();
        } else if (stateRef.current.phase === 'conflict_refresh') {
          attemptRef.current += 1;
          closeDialog();
        } else if (dialog.pendingCancellation) {
          attemptRef.current += 1;
          void dialog.pendingCancellation.cancel();
          closeDialog();
        }
        return;
      }

      if (e.key === 'Tab') {
        const focusableElements = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, summary, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusableElements || focusableElements.length === 0) return;

        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];
        const activeIndex = [...focusableElements].indexOf(
          document.activeElement as HTMLElement,
        );

        if (activeIndex === -1) {
          e.preventDefault();
          (e.shiftKey ? lastElement : firstElement).focus();
          return;
        }

        if (e.shiftKey) {
          if (document.activeElement === firstElement) {
            e.preventDefault();
            lastElement.focus();
          }
        } else {
          if (document.activeElement === lastElement) {
            e.preventDefault();
            firstElement.focus();
          }
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);

    return () => {
      clearTimeout(timer);
      window.removeEventListener('keydown', handleKeyDown);
      previousFocusRef.current?.focus();
    };
  }, [dialog, closeDialog]);

  useEffect(() => {
    if (failure) errorRef.current?.focus();
  }, [failure]);

  const handleClose = useCallback(() => {
    if (!isConfirming || isConflictRefresh) {
      if (isConflictRefresh) attemptRef.current += 1;
      closeDialog();
    } else if (dialog?.pendingCancellation) {
      attemptRef.current += 1;
      void dialog.pendingCancellation.cancel();
      closeDialog();
    }
  }, [closeDialog, dialog, isConfirming, isConflictRefresh]);

  const handleConfirm = useCallback(async () => {
    if (!dialog || stateRef.current.status === 'pending') return;
    const attempt = attemptRef.current + 1;
    attemptRef.current = attempt;
    stateRef.current = {status: 'pending', phase: 'confirm'};
    setConfirmationState({status: 'pending', phase: 'confirm'});
    setCopied(false);
    try {
      await dialog.onConfirm();
      if (
        attemptRef.current !== attempt
        || useAppStore.getState().dialog !== dialog
      ) return;
      setConfirmationState({status: 'success'});
      if (useAppStore.getState().dialog === dialog) closeDialog();
    } catch (error) {
      if (
        attemptRef.current !== attempt
        || useAppStore.getState().dialog !== dialog
      ) return;
      const nextFailure = classifyConfirmationFailure(error);
      setConfirmationState({status: nextFailure.status, failure: nextFailure, source: 'confirm'});
    }
  }, [dialog, closeDialog]);

  const handleConflict = useCallback(async () => {
    if (!dialog?.onConflict || stateRef.current.status === 'pending') return;
    const attempt = attemptRef.current + 1;
    attemptRef.current = attempt;
    stateRef.current = {status: 'pending', phase: 'conflict_refresh'};
    setConfirmationState({status: 'pending', phase: 'conflict_refresh'});
    try {
      await dialog.onConflict();
      if (attemptRef.current === attempt && useAppStore.getState().dialog === dialog) closeDialog();
    } catch (error) {
      if (
        attemptRef.current !== attempt
        || useAppStore.getState().dialog !== dialog
      ) return;
      const nextFailure = classifyConfirmationFailure(error, {preDispatchRetrySafe: true});
      setConfirmationState({
        status: nextFailure.status,
        failure: nextFailure,
        source: 'conflict_refresh',
      });
    }
  }, [closeDialog, dialog]);

  const copyTechnicalDetails = useCallback(async () => {
    if (!failure || !navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(failure.technicalDetails);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }, [failure]);

  if (!dialog) return null;

  return (
    <div
      className={cn("fixed inset-0 flex items-center justify-center bg-black/50", viewerLayers.dialog)}
      onClick={handleClose}
      role="presentation"
    >
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        aria-describedby="dialog-description dialog-context"
        aria-busy={isConfirming}
        tabIndex={-1}
        className="w-full max-w-md rounded-xl border border-border bg-bg-card p-6 shadow-[var(--shadow-lg)] outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="dialog-title" className="text-lg font-semibold text-text-main">{dialog.title}</h2>
        <p id="dialog-description" className="mt-2 text-sm text-text-muted">{dialog.message}</p>
        {confirmationError ? (
          <div
            id="dialog-error"
            role="alert"
            aria-live="assertive"
            className="mt-3 flex items-center gap-2 rounded-md border border-destructive/35 bg-destructive/8 px-3 py-2 text-sm text-destructive"
          >
            <AlertCircle aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1 break-words">{confirmationError}</span>
            <CopyErrorButton label="Copy confirmation error" text={confirmationError} />
          </div>
        ) : null}
        <div className="mt-6 flex justify-end gap-3">
          {isConflictRefresh ? (
            <>
              <Button variant="outline" onClick={handleClose}>Close</Button>
              <Button loading disabled>Refreshing status…</Button>
            </>
          ) : failureSource === 'conflict_refresh' && failure?.retryAllowed ? (
            <>
              <Button variant="outline" onClick={handleClose}>Close</Button>
              <Button onClick={() => void handleConflict()}>Retry status</Button>
            </>
          ) : failure?.status === 'conflict' && dialog.onConflict ? (
            <>
              <Button variant="outline" onClick={handleClose}>Close</Button>
              <Button onClick={() => void handleConflict()}>Refresh status</Button>
            </>
          ) : (
            <>
              <Button
                variant="outline"
                onClick={handleClose}
                disabled={isConfirming && !isConflictRefresh && !dialog.pendingCancellation}
              >
                {failure && !failure.retryAllowed ? 'Close' : (dialog.cancelText || t('dialog.cancel'))}
              </Button>
              {(!failure || failure.retryAllowed) ? (
                <Button onClick={handleConfirm} loading={isConfirming}>
                  {isConfirming
                    ? (dialog.confirmPendingText || t('dialog.pending'))
                    : (failure ? 'Retry' : (dialog.confirmText || t('dialog.confirm')))}
                </Button>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
