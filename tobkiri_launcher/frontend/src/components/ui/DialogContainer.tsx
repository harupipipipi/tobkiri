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
  const isConfirmingRef = useRef(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [confirmationError, setConfirmationError] = useState<string | null>(null);
  isConfirmingRef.current = isConfirming;

  useEffect(() => {
    if (!dialog) {
      setIsConfirming(false);
      setConfirmationError(null);
      return;
    }

    setConfirmationError(null);
    previousFocusRef.current = document.activeElement as HTMLElement | null;

    const timer = setTimeout(() => {
      dialogRef.current?.focus();
    }, 0);

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (!isConfirmingRef.current) {
          closeDialog();
        }
        return;
      }

      if (e.key === 'Tab') {
        const focusableElements = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusableElements || focusableElements.length === 0) return;

        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];

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

  const handleClose = useCallback(() => {
    if (!isConfirming) {
      closeDialog();
    }
  }, [closeDialog, isConfirming]);

  const handleConfirm = useCallback(async () => {
    if (!dialog || isConfirmingRef.current) return;
    isConfirmingRef.current = true;
    setIsConfirming(true);
    setConfirmationError(null);
    try {
      await dialog.onConfirm();
      closeDialog();
    } catch (error) {
      setConfirmationError(
        formatUserFacingError(
          error,
          'The confirmation could not be completed.',
          'dialog.confirm',
        ),
      );
    } finally {
      isConfirmingRef.current = false;
      setIsConfirming(false);
    }
  }, [dialog, closeDialog]);

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
        aria-describedby={confirmationError ? 'dialog-description dialog-error' : 'dialog-description'}
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
          <Button variant="outline" onClick={handleClose} disabled={isConfirming}>
            {dialog.cancelText || t('dialog.cancel')}
          </Button>
          <Button onClick={handleConfirm} loading={isConfirming}>
            {isConfirming
              ? (dialog.confirmPendingText || t('dialog.pending'))
              : (dialog.confirmText || t('dialog.confirm'))}
          </Button>
        </div>
      </div>
    </div>
  );
}
