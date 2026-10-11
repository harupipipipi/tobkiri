import { CheckCircle2, Circle, Play, RefreshCw, XCircle } from "lucide-react";

import type { ChangeRequestRecord } from "../../lib/changeRequests";
import { ErrorCopyAction } from "../ErrorNotice";

export function ChangeReviewChecksTab({
  review,
  actionBusy,
  checkCommand,
  onCheckCommandChange,
  onReloadChecks,
  onRunCheck,
}: {
  review: ChangeRequestRecord | null;
  actionBusy: string | null;
  checkCommand: string;
  onCheckCommandChange: (command: string) => void;
  onReloadChecks: () => void;
  onRunCheck: (command: string) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <button type="button" onClick={onReloadChecks} disabled={!review || actionBusy === "checks"} className="flex h-7 items-center gap-1 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">
          <RefreshCw size={12} /> Reload
        </button>
        <input
          value={checkCommand}
          onChange={(event) => onCheckCommandChange(event.target.value)}
          placeholder="python -m pytest"
          className="h-7 min-w-0 flex-1 rounded-md border border-zinc-800 bg-black/30 px-2 font-mono text-[11px] text-zinc-200 outline-none placeholder:text-zinc-700 focus:border-zinc-600"
        />
        <button type="button" onClick={() => onRunCheck(checkCommand)} disabled={!review || !checkCommand.trim() || actionBusy?.startsWith("check-")} className="flex h-7 items-center gap-1 rounded-md bg-zinc-100 px-2 text-[11px] font-semibold text-zinc-950 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-600">
          <Play size={12} /> Run
        </button>
      </div>
      {(review?.suggested_checks ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {(review?.suggested_checks ?? []).map((check) => (
            <button key={check.id} type="button" onClick={() => onRunCheck(check.command)} disabled={actionBusy?.startsWith("check-")} className="rounded-md border border-zinc-800 px-2 py-1 text-left text-[11px] text-zinc-300 hover:bg-zinc-900 disabled:opacity-40">
              <span className="font-mono">{check.command}</span>
            </button>
          ))}
        </div>
      )}
      <div className="space-y-1.5">
        {(review?.checks ?? []).map((check) => {
          const passed = check.status === "passed";
          const failed = check.status === "failed" || check.status === "timed_out";
          const checkLabel = check.command || check.name || check.id;
          const output = check.log_tail || check.stderr_tail || check.stdout_tail;
          return (
            <div key={check.id} className={`rounded-md border p-2 ${failed ? "border-red-500/30 bg-red-500/[0.06]" : "border-zinc-800 bg-black/20"}`}>
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  {passed ? <CheckCircle2 size={13} className="text-emerald-300" /> : failed ? <XCircle size={13} className="text-red-300" /> : <Circle size={13} className="text-zinc-500" />}
                  <span className="truncate font-mono text-[11px] text-zinc-200">{checkLabel}</span>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <span className="rounded border border-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">{check.status ?? "queued"}</span>
                  {failed && (
                    <ErrorCopyAction
                      copyText={`${checkLabel}\n\n${output || String(check.status ?? "failed")}`}
                      label="失敗したチェックをコピー"
                    />
                  )}
                </div>
              </div>
              {output && <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 text-[10px] text-zinc-500">{output}</pre>}
            </div>
          );
        })}
        {review && (review.checks ?? []).length === 0 && <p className="rounded-md border border-zinc-800 bg-black/20 px-2 py-4 text-center text-[11px] text-zinc-600">No checks have run</p>}
        {!review && <p className="rounded-md border border-zinc-800 bg-black/20 px-2 py-4 text-center text-[11px] text-zinc-600">Create or select a review to run checks</p>}
      </div>
    </div>
  );
}
