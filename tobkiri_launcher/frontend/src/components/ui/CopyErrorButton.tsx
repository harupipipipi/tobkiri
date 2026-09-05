import {Copy} from 'lucide-react';
import {useEffect, useId, useRef, useState} from 'react';

import {copyTextToClipboard} from '@/src/lib/clipboard';
import {cn} from '@/src/lib/utils';

import {Button} from './Button';

type CopyFeedback = 'idle' | 'copied' | 'failed';

export interface CopyErrorButtonProps {
  /** The complete user-visible diagnostic text to place on the clipboard. */
  text: string;
  /** Adds context to the accessible label when more than one error is visible. */
  label?: string;
  className?: string;
}

const feedbackText: Record<CopyFeedback, string> = {
  idle: '',
  copied: 'Error details copied to the clipboard.',
  failed: 'Could not copy error details. Select the text and copy it manually.',
};

const visibleFeedback: Record<Exclude<CopyFeedback, 'idle'>, string> = {
  copied: 'Copied',
  failed: 'Copy failed',
};

/**
 * A compact, accessible copy action shared by every Launcher error surface.
 *
 * The copy glyph remains stable after an attempt. A compact visible status and
 * live announcement communicate the result without competing with each
 * surface's existing severity icon. Failure deliberately does not create
 * another error toast, which would obscure the diagnostic being copied.
 */
export function CopyErrorButton({
  text,
  label = 'Copy error details',
  className,
}: CopyErrorButtonProps) {
  const [feedback, setFeedback] = useState<CopyFeedback>('idle');
  const statusId = useId();
  const copyAttempt = useRef(0);

  useEffect(() => {
    copyAttempt.current += 1;
    setFeedback('idle');
    return () => {
      copyAttempt.current += 1;
    };
  }, [text]);

  useEffect(() => {
    if (feedback === 'idle') return undefined;
    const timer = window.setTimeout(() => setFeedback('idle'), 3_000);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  const copy = async () => {
    const attempt = ++copyAttempt.current;
    const copied = await copyTextToClipboard(text);
    if (attempt === copyAttempt.current) {
      setFeedback(copied ? 'copied' : 'failed');
    }
  };

  const title = feedback === 'idle' ? label : feedbackText[feedback];

  return (
    <span className="inline-flex shrink-0 items-center gap-1.5">
      <Button
        aria-describedby={statusId}
        aria-label={title}
        className={cn(
          className,
          'h-7 w-7 rounded-md border border-border bg-bg-main p-0 text-text-muted shadow-none hover:bg-bg-hover hover:text-text-main',
          feedback === 'copied' && 'border-emerald-300 text-emerald-700 dark:border-emerald-800/60 dark:text-emerald-300',
          feedback === 'failed' && 'border-destructive/60 text-destructive',
        )}
        onClick={() => void copy()}
        size="icon"
        title={title}
        type="button"
        variant="outline"
      >
        <Copy aria-hidden="true" className="h-3.5 w-3.5" />
      </Button>
      <span
        className={cn(
          'whitespace-nowrap text-xs font-medium',
          feedback === 'idle' && 'sr-only',
          feedback === 'copied' && 'text-emerald-700 dark:text-emerald-300',
          feedback === 'failed' && 'text-destructive',
        )}
        id={statusId}
        role="status"
        aria-live="polite"
      >
        {feedback === 'idle' ? '' : visibleFeedback[feedback]}
      </span>
    </span>
  );
}
