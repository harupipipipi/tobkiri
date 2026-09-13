import { AlertTriangle, CheckCircle2, Loader2, XCircle } from "lucide-react";

import { cn } from "../../lib/cn";
import type { RuntimeOperation } from "../../features/sandboxes/types";
import { ErrorNotice } from "../ErrorNotice";

type RuntimeSetupDialogProps = {
  operation: RuntimeOperation | null;
  cancelLoading?: boolean;
  onCancel?: () => void;
};

export function RuntimeSetupDialog({ operation, cancelLoading = false, onCancel }: RuntimeSetupDialogProps) {
  if (!operation) return null;
  const completed = operation.status === "completed";
  const failed = operation.status === "failed";
  const cancelled = operation.status === "cancelled";
  const running = !completed && !failed && !cancelled;
  const progress = typeof operation.progress === "number" ? Math.max(0, Math.min(100, Math.round(operation.progress))) : null;

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
      <div className="flex items-start gap-2">
        <div className={cn(
          "mt-0.5 flex h-7 w-7 items-center justify-center rounded-md border",
          completed && "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
          failed && "border-red-500/30 bg-red-500/10 text-red-200",
          cancelled && "border-zinc-700 bg-zinc-900 text-zinc-400",
          running && "border-amber-500/30 bg-amber-500/10 text-amber-200",
        )}>
          {completed ? <CheckCircle2 size={15} /> : failed ? <AlertTriangle size={15} /> : cancelled ? <XCircle size={15} /> : <Loader2 size={15} className="animate-spin" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-semibold text-zinc-100">{operation.step || operation.status}</p>
                <span className="rounded border border-zinc-800 bg-black/30 px-1.5 py-0.5 text-[10px] text-zinc-500">
                  {operation.operation_id}
                </span>
              </div>
            </div>
            {running && onCancel && (
              <button
                type="button"
                onClick={onCancel}
                disabled={cancelLoading}
                className="flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/70 px-2 text-[11px] font-medium text-zinc-400 transition-colors hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-100 disabled:cursor-wait disabled:opacity-60"
              >
                <XCircle size={13} />
                <span>{cancelLoading ? "Cancelling" : "Cancel"}</span>
              </button>
            )}
          </div>
          {operation.message && <p className="mt-1 text-xs text-zinc-400">{operation.message}</p>}
          {progress !== null && (
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-zinc-900">
              <div className="h-full rounded-full bg-emerald-400" style={{ width: `${progress}%` }} />
            </div>
          )}
          {operation.reboot_required && (
            <p className="mt-2 rounded-md border border-amber-500/25 bg-amber-500/10 px-2 py-1.5 text-xs text-amber-100">
              Windows reported a reboot is required before setup can continue.
            </p>
          )}
          {operation.error && (
            <ErrorNotice
              className="mt-2 px-2 py-1.5 text-xs"
              copyLabel="ランタイム設定エラーをコピー"
              message={typeof operation.error === "string" ? operation.error : operation.error.message}
            />
          )}
        </div>
      </div>
    </div>
  );
}
