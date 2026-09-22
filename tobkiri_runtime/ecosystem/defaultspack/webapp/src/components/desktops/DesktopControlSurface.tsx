import { Camera, Play, Power, RefreshCcw, Trash2, UserCheck, UserX } from "lucide-react";

import { cn } from "../../lib/cn";
import type { DesktopInstance } from "../../features/sandboxes/types";

type DesktopControlSurfaceProps = {
  desktop: DesktopInstance;
  hasLease: boolean;
  busy?: boolean;
  onTakeOver: () => void;
  onReturnToAI: () => void;
  onSnapshot: () => void;
  onRestart: () => void;
  onStop: () => void;
  onStart?: () => void;
  onDelete: () => void;
};

function actionButtonClassName(tone: "default" | "danger" = "default") {
  return cn(
    "flex h-8 min-w-8 items-center justify-center gap-1.5 rounded-md border px-2 text-[11px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500/70 disabled:cursor-not-allowed disabled:opacity-45",
    tone === "danger"
      ? "border-red-500/25 bg-red-500/10 text-red-200 hover:border-red-400/40 hover:bg-red-500/15"
      : "border-zinc-800 bg-zinc-950/60 text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100",
  );
}

export function DesktopControlSurface({
  desktop,
  hasLease,
  busy = false,
  onTakeOver,
  onReturnToAI,
  onSnapshot,
  onRestart,
  onStop,
  onStart,
  onDelete,
}: DesktopControlSurfaceProps) {
  const isRunning = desktop.status === "running";
  const isStopped = desktop.status === "stopped";
  const isDestroyed = desktop.status === "destroyed";

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {hasLease ? (
        <button
          type="button"
          data-desktop-seat-id={desktop.seat_id}
          data-desktop-action="return-to-ai"
          onClick={onReturnToAI}
          disabled={busy || isDestroyed}
          className={actionButtonClassName()}
          aria-label={`Return ${desktop.name} to AI control`}
        >
          <UserX size={13} />
          <span>Return to AI</span>
        </button>
      ) : (
        <button
          type="button"
          data-desktop-seat-id={desktop.seat_id}
          data-desktop-action="take-over"
          onClick={onTakeOver}
          disabled={busy || !isRunning}
          className={actionButtonClassName()}
          aria-label={`Take over ${desktop.name}`}
        >
          <UserCheck size={13} />
          <span>Take over</span>
        </button>
      )}
      <button
        type="button"
        data-desktop-seat-id={desktop.seat_id}
        data-desktop-action="snapshot"
        onClick={onSnapshot}
        disabled={busy || !isRunning}
        className={actionButtonClassName()}
        aria-label={`Snapshot ${desktop.name}`}
      >
        <Camera size={13} />
        <span>Snapshot</span>
      </button>
      {isStopped && onStart ? (
        <button
          type="button"
          data-desktop-seat-id={desktop.seat_id}
          data-desktop-action="start"
          onClick={onStart}
          disabled={busy || isDestroyed}
          className={actionButtonClassName()}
          aria-label={`Start ${desktop.name}`}
        >
          <Play size={13} />
          <span>Start</span>
        </button>
      ) : (
        <button
          type="button"
          data-desktop-seat-id={desktop.seat_id}
          data-desktop-action="restart"
          onClick={onRestart}
          disabled={busy || isDestroyed}
          className={actionButtonClassName()}
          aria-label={`Restart ${desktop.name}`}
        >
          <RefreshCcw size={13} />
          <span>Restart</span>
        </button>
      )}
      <button
        type="button"
        data-desktop-seat-id={desktop.seat_id}
        data-desktop-action="stop"
        onClick={onStop}
        disabled={busy || !isRunning}
        className={actionButtonClassName()}
        aria-label={`Stop ${desktop.name}`}
      >
        <Power size={13} />
        <span>Stop</span>
      </button>
      <button
        type="button"
        data-desktop-seat-id={desktop.seat_id}
        data-desktop-action="delete"
        onClick={onDelete}
        disabled={busy || isDestroyed}
        className={actionButtonClassName("danger")}
        aria-label={`Delete ${desktop.name}`}
      >
        <Trash2 size={13} />
        <span>Delete</span>
      </button>
    </div>
  );
}
