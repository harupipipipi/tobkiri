import type { RefObject } from "react";
import { AlertTriangle, Trash2, X } from "lucide-react";

import { ModalFoundation } from "../ModalFoundation";
import { cn } from "../../lib/cn";
import type { DesktopInstance } from "../../features/sandboxes/types";
import type {
  DesktopLifecycleAction,
  DesktopLifecycleFeedback,
} from "./desktopLifecycle";

type ConfirmationAction = Extract<DesktopLifecycleAction, "stop" | "delete">;

export function DesktopLifecycleConfirmation({
  action,
  target,
  feedback,
  confirmButtonRef,
  onClose,
  onConfirm,
}: {
  action: ConfirmationAction;
  target: DesktopInstance;
  feedback?: DesktopLifecycleFeedback;
  confirmButtonRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const pending = feedback?.phase === "pending";
  const failed = feedback?.phase === "failed";
  const deleting = action === "delete";
  const actionTitle = deleting ? "Delete Desktop" : "Stop Desktop";
  const progressText = deleting
    ? "Deleting this desktop and checking the latest server state…"
    : "Stopping this desktop and checking the latest server state…";
  const explanation = deleting
    ? "This removes the desktop session and clears its cached frame and control lease."
    : "This stops the desktop session and releases its cached frame and active control lease.";

  return (
    <ModalFoundation
      variant="alertdialog"
      title={`${actionTitle}: ${target.name}`}
      description={explanation}
      onClose={onClose}
      dismissible={!pending}
      initialFocusRef={confirmButtonRef}
      backdropClassName="absolute inset-0 rumi-layer-modal flex items-center justify-center bg-black/60 p-4"
      panelClassName={cn(
        "w-[min(420px,100%)] rounded-lg border bg-[#0b0b0d] shadow-2xl outline-none",
        deleting ? "border-red-500/25" : "border-amber-500/25",
      )}
      aria-busy={pending || undefined}
      data-desktop-lifecycle-action={action}
      data-desktop-seat-id={target.seat_id}
    >
      <div className={cn(
        "flex items-start justify-between gap-3 border-b px-4 py-3",
        deleting ? "border-red-500/20" : "border-amber-500/20",
      )}>
        <div className="flex min-w-0 items-center gap-2">
          <span className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-md border",
            deleting
              ? "border-red-500/25 bg-red-500/10 text-red-200"
              : "border-amber-500/25 bg-amber-500/10 text-amber-100",
          )}>
            <AlertTriangle size={15} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-zinc-100">{actionTitle}</p>
            <p className="truncate text-xs text-zinc-500">{target.name}</p>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          disabled={pending}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-45"
          aria-label={`Close ${action} confirmation`}
        >
          <X size={15} />
        </button>
      </div>
      <div className="px-4 py-4 text-sm leading-6 text-zinc-300">
        {explanation}
        {feedback && (
          <div
            className={cn(
              "mt-3 rounded-md border px-3 py-2 text-xs",
              failed
                ? "border-red-500/25 bg-red-500/10 text-red-100"
                : deleting
                  ? "border-red-500/25 bg-red-500/10 text-red-100"
                  : "border-amber-500/25 bg-amber-500/10 text-amber-100",
            )}
            role={failed ? "alert" : "status"}
            aria-live={failed ? "assertive" : "polite"}
          >
            <p>{pending ? progressText : feedback.error}</p>
            <p className="mt-1 break-all text-[10px] opacity-70">
              Operation {feedback.operationId}
            </p>
          </div>
        )}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-zinc-800/70 px-4 py-3">
        <button
          type="button"
          onClick={onClose}
          disabled={pending}
          className="h-8 rounded-md border border-zinc-800 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-45"
        >
          Cancel
        </button>
        <button
          type="button"
          ref={confirmButtonRef}
          onClick={onConfirm}
          disabled={pending}
          className={cn(
            "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-60",
            deleting
              ? "bg-red-500 text-white hover:bg-red-400"
              : "bg-amber-400 text-zinc-950 hover:bg-amber-300",
          )}
        >
          {deleting && <Trash2 size={13} />}
          <span>{pending ? (deleting ? "Deleting…" : "Stopping…") : failed ? "Retry" : deleting ? "Delete" : "Stop"}</span>
        </button>
      </div>
    </ModalFoundation>
  );
}
