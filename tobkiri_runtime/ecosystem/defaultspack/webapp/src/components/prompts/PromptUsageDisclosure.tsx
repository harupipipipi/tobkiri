import { ChevronDown, FileText } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { PromptUsageSummary } from "../../lib/api";
import { cn } from "../../lib/cn";
import { ErrorNotice } from "../ErrorNotice";
import { PromptUsageSegmentCard } from "./PromptUsageSegmentCard";
import { allPromptUsageSegments, tokenText } from "./promptSegmentView";

type PromptUsageDisclosureProps = {
  usage?: PromptUsageSummary | null;
  loadPromptTrace?: (traceId: string, profileId?: string) => Promise<PromptUsageSummary>;
};

export function PromptUsageDisclosure({ usage, loadPromptTrace }: PromptUsageDisclosureProps) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<PromptUsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const traceId = String(usage?.trace_id ?? "").trim();
  const profileId = String(usage?.profile_id ?? "").trim();
  const segments = useMemo(() => allPromptUsageSegments(detail ?? usage), [detail, usage]);
  const activeCount = Number((detail ?? usage)?.active_count ?? segments.filter((segment) => segment.status === "active").length);
  const disabledCount = Number((detail ?? usage)?.disabled_count ?? segments.filter((segment) => segment.status !== "active").length);
  const totalTokens = Number((detail ?? usage)?.token_estimate?.total ?? 0);

  useEffect(() => {
    if (!open || !traceId || detail || segments.some((segment) => segment.text) || !loadPromptTrace) return;
    let cancelled = false;
    void loadPromptTrace(traceId, profileId || undefined)
      .then((result) => {
        if (!cancelled) {
          setDetail(result);
          setError(null);
        }
      })
      .catch((fetchError) => {
        if (!cancelled) setError(fetchError instanceof Error ? fetchError.message : "Prompt trace could not be loaded.");
      });
    return () => {
      cancelled = true;
    };
  }, [detail, loadPromptTrace, open, profileId, segments, traceId]);

  if (!usage || (!traceId && segments.length === 0)) return null;

  return (
    <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/55 text-zinc-300">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex min-h-10 w-full items-center justify-between gap-3 px-3 py-2 text-left"
        aria-expanded={open}
      >
        <span className="flex min-w-0 items-center gap-2">
          <FileText size={14} className="shrink-0 text-cyan-300" />
          <span className="truncate text-xs font-semibold text-zinc-100">Prompt used</span>
          <span className="shrink-0 rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
            {activeCount} segments
          </span>
          {disabledCount > 0 && (
            <span className="shrink-0 rounded border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-200">
              {disabledCount} not active
            </span>
          )}
          <span className="shrink-0 font-mono text-[10px] text-zinc-500">{tokenText(totalTokens)}</span>
        </span>
        <ChevronDown size={14} className={cn("shrink-0 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="border-t border-zinc-800 px-3 py-3">
          {error && (
            <ErrorNotice
              className="mb-3 px-2 py-1.5 text-[11px]"
              copyLabel="プロンプト詳細エラーをコピー"
              message={error}
              severity="warning"
            />
          )}
          <div className="grid gap-2">
            {segments.map((segment) => (
              <PromptUsageSegmentCard key={`${segment.id}-${segment.status}`} segment={segment} variant="disclosure" />
            ))}
          </div>
          {!segments.length && <div className="rounded-md border border-dashed border-zinc-800 px-3 py-4 text-center text-xs text-zinc-500">No prompt usage segments were recorded.</div>}
        </div>
      )}
    </div>
  );
}
