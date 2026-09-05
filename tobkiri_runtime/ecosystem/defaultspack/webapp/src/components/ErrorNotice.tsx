import { AlertTriangle, CircleAlert, Copy } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";

import { cn } from "../lib/cn";

type CopyTextEnvironment = {
  clipboard?: Pick<Clipboard, "writeText"> | null;
  document?: Document | null;
};

type ErrorCopyActionProps = {
  className?: string;
  copyText: string;
  failureMessage?: string;
  label?: string;
  onCopy?: (text: string) => Promise<boolean> | boolean;
  successMessage?: string;
};

export type ErrorNoticeProps = {
  announce?: boolean;
  children?: ReactNode;
  className?: string;
  copyLabel?: string;
  copyText?: string;
  errorIcon?: string;
  iconClassName?: string;
  message: string;
  messageClassName?: string;
  severity?: "error" | "warning";
  title?: string;
  titleClassName?: string;
  trailing?: ReactNode;
};

/** Returns the exact user-visible notice text used by the default copy action. */
export function errorNoticeCopyText(title: string | undefined, message: string): string {
  return title ? `${title}\n\n${message}` : message;
}

function defaultCopyEnvironment(): CopyTextEnvironment {
  return {
    clipboard: typeof navigator === "undefined" ? null : navigator.clipboard,
    document: typeof document === "undefined" ? null : document,
  };
}

/**
 * Copies text with the Clipboard API when available and a selection fallback
 * for desktop WebViews that expose but reject the asynchronous API.
 */
export async function copyTextWithFallback(
  text: string,
  environment: CopyTextEnvironment = defaultCopyEnvironment(),
): Promise<boolean> {
  try {
    if (environment.clipboard?.writeText) {
      await environment.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Some WebViews expose Clipboard API but reject it outside a trusted gesture.
  }

  const sourceDocument = environment.document;
  if (!sourceDocument?.body || typeof sourceDocument.execCommand !== "function") {
    return false;
  }

  const textarea = sourceDocument.createElement("textarea");
  const activeElement = sourceDocument.activeElement;
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0;pointer-events:none";

  try {
    sourceDocument.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    return sourceDocument.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
    if (typeof HTMLElement !== "undefined" && activeElement instanceof HTMLElement) {
      activeElement.focus({ preventScroll: true });
    }
  }
}

/**
 * A stable copy control for notices. Feedback is announced in a live region;
 * it never replaces the double-square Copy glyph with a success or error icon.
 */
export function ErrorCopyAction({
  className,
  copyText,
  failureMessage = "コピーできませんでした。エラー本文を選択してコピーしてください。",
  label = "エラーをコピー",
  onCopy = copyTextWithFallback,
  successMessage = "エラーをクリップボードにコピーしました。",
}: ErrorCopyActionProps) {
  const [feedback, setFeedback] = useState<"idle" | "copied" | "failed">("idle");
  const feedbackId = useId();

  useEffect(() => {
    setFeedback("idle");
  }, [copyText]);

  useEffect(() => {
    if (feedback === "idle") return undefined;
    const timer = window.setTimeout(() => setFeedback("idle"), 3_000);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  const feedbackMessage = feedback === "copied"
    ? successMessage
    : feedback === "failed"
      ? failureMessage
      : "";

  return (
    <span className="inline-flex shrink-0 items-center">
      <button
        aria-describedby={feedbackId}
        aria-label={label}
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-zinc-700 bg-zinc-950 text-zinc-300 hover:border-zinc-500 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-300",
          feedback === "copied" && "border-emerald-500/60 text-emerald-200",
          feedback === "failed" && "border-red-400/60 text-red-100",
          className,
        )}
        data-copy-action=""
        onClick={() => {
          void Promise.resolve(onCopy(copyText)).then(
            (copied) => setFeedback(copied ? "copied" : "failed"),
            () => setFeedback("failed"),
          );
        }}
        title={label}
        type="button"
      >
        <Copy aria-hidden="true" data-copy-icon="" size={14} />
      </button>
      <span
        role="status"
        aria-live="polite"
        className="sr-only"
        id={feedbackId}
      >
        {feedbackMessage}
      </span>
    </span>
  );
}

/**
 * Semantic notice for errors and warnings with a separate, stable copy action.
 */
export function ErrorNotice({
  announce = true,
  children,
  className,
  copyLabel,
  copyText,
  errorIcon,
  iconClassName,
  message,
  messageClassName,
  severity = "error",
  title,
  titleClassName,
  trailing,
}: ErrorNoticeProps) {
  const SeverityIcon = severity === "warning" ? AlertTriangle : CircleAlert;
  const iconId = errorIcon ?? severity;
  const liveMode = severity === "warning" ? "polite" : "assertive";
  const noticeRole = severity === "warning" ? "status" : "alert";

  return (
    <div
      aria-live={announce ? liveMode : undefined}
      className={cn(
        "flex items-start gap-2 rounded-lg border p-3",
        severity === "warning"
          ? "border-amber-500/30 bg-amber-500/10 text-amber-100"
          : "border-red-500/30 bg-red-500/10 text-red-100",
        className,
      )}
      data-error-notice={iconId}
      role={announce ? noticeRole : undefined}
    >
      <SeverityIcon
        aria-hidden="true"
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          severity === "warning" ? "text-amber-300" : "text-red-300",
          iconClassName,
        )}
        data-error-icon={iconId}
      />
      <div className="min-w-0 flex-1">
        {title ? (
          <p className={cn("font-semibold", titleClassName)}>{title}</p>
        ) : null}
        <p className={cn(title && "mt-1", "break-words", messageClassName)}>{message}</p>
        {children}
      </div>
      <ErrorCopyAction
        copyText={copyText ?? errorNoticeCopyText(title, message)}
        label={copyLabel}
      />
      {trailing}
    </div>
  );
}
