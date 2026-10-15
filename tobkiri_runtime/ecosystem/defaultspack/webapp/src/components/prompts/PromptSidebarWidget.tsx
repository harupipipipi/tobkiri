import { AlertTriangle, ChevronDown, Eye, EyeOff, FileText, RefreshCw, ShieldCheck, ToggleLeft, ToggleRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { PromptUsageSegment, PromptUsageSummary } from "../../lib/api";
import { cn } from "../../lib/cn";
import { ErrorNotice } from "../ErrorNotice";
import { allPromptUsageSegments, promptSegmentKindLabel, promptSegmentTitle, sourceLine, tokenText, tokenizerLabel, tokenizerNeedsWarning, tokenizerWarningText } from "./promptSegmentView";

type PromptSidebarWidgetProps = {
  profileId?: string;
  conversationId?: string | null;
  modelProfileId?: string;
  modelLabel?: string;
  initialUsage?: PromptUsageSummary | null;
  loadPromptActive: (params: { profile_id?: string; conversation_id?: string; include_text?: boolean; model_profile_id?: string; model?: string }) => Promise<PromptUsageSummary>;
  togglePromptEdge: (payload: { profile_id?: string; conversation_id?: string; edge_id: string; enabled: boolean; model_profile_id?: string; model?: string }) => Promise<PromptUsageSummary>;
  showChatPromptUsage?: boolean;
  onToggleChatPromptUsage?: (visible: boolean) => void;
};

function segmentTokenCount(segment: PromptUsageSegment): number {
  const tokens = Number(segment.tokens ?? 0);
  return Number.isFinite(tokens) && tokens > 0 ? tokens : 0;
}

function segmentStatusTone(status: string | undefined): string {
  if (status === "active") return "bg-emerald-400";
  if (status === "gated") return "bg-sky-300";
  if (status === "budget-dropped") return "bg-amber-300";
  return "bg-zinc-600";
}

function canToggleSegment(segment: PromptUsageSegment): boolean {
  return Boolean(String(segment.edge_id ?? "").trim()) && segment.allow_disable !== false;
}

function isAuthoredPromptSegment(segment: PromptUsageSegment): boolean {
  const kind = String(segment.kind || "").replace(/_/g, "-");
  const sourceType = String(segment.source_type || "").replace(/-/g, "_");
  if (kind === "tool-schema" || sourceType === "tool_schema") return false;
  if (sourceType === "profile_policy" || String(segment.port || "") === "policy") return false;
  return Boolean(String(segment.prompt_id || segment.id || "").trim());
}

function segmentSortGroup(segment: PromptUsageSegment): number {
  if (isAuthoredPromptSegment(segment)) return 0;
  if (String(segment.kind || segment.source_type || "").includes("skill")) return 1;
  return 2;
}

function compactSegmentReason(segment: PromptUsageSegment): string {
  return String(segment.explanation || segment.reason || "Included by the active AI Input Graph.").trim();
}

export function PromptSidebarWidget({
  profileId,
  conversationId,
  modelProfileId,
  modelLabel,
  initialUsage = null,
  loadPromptActive,
  togglePromptEdge,
  showChatPromptUsage = true,
  onToggleChatPromptUsage,
}: PromptSidebarWidgetProps) {
  const [summary, setSummary] = useState<PromptUsageSummary | null>(initialUsage);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [busyEdge, setBusyEdge] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const segments = useMemo(() => (
    allPromptUsageSegments(summary)
      .filter((segment) => String(segment.label || segment.prompt_id || segment.id || "").trim())
      .sort((left, right) => {
        const statusOrder = (left.status === "active" ? 0 : 1) - (right.status === "active" ? 0 : 1);
        if (statusOrder !== 0) return statusOrder;
        const groupOrder = segmentSortGroup(left) - segmentSortGroup(right);
        if (groupOrder !== 0) return groupOrder;
        return segmentTokenCount(right) - segmentTokenCount(left) || promptSegmentTitle(left).localeCompare(promptSegmentTitle(right));
      })
  ), [summary]);
  const activeCount = segments.filter((segment) => segment.status === "active").length;
  const inactiveCount = segments.length - activeCount;
  const totalTokens = Number(summary?.token_estimate?.total ?? segments.reduce((sum, segment) => sum + segmentTokenCount(segment), 0));
  const summaryTokenizer = summary?.token_estimate?.tokenizer ?? null;
  const summaryTokenizerWarning = tokenizerWarningText(summaryTokenizer, "モデルの tokenizer が見つからないため、デフォルトの tokenizer を使用しています。大きくズレる可能性があります。");

  const load = () => {
    setLoading(true);
    void loadPromptActive({
      profile_id: profileId,
      conversation_id: conversationId ?? undefined,
      model_profile_id: modelProfileId || undefined,
      model: modelProfileId || undefined,
    })
      .then((result) => {
        setSummary(result);
        setError(null);
      })
      .catch((loadError) => {
        setError(loadError instanceof Error ? loadError.message : "Prompt summary could not be loaded.");
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    setSummary(initialUsage);
  }, [initialUsage]);

  useEffect(load, [profileId, conversationId, modelProfileId]);

  const toggleExpanded = (segmentId: string) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(segmentId)) {
        next.delete(segmentId);
      } else {
        next.add(segmentId);
      }
      return next;
    });
  };

  const toggleSegment = (segment: PromptUsageSegment) => {
    const edgeId = String(segment.edge_id ?? "").trim();
    if (!edgeId || segment.allow_disable === false) return;
    setBusyEdge(edgeId);
    void togglePromptEdge({
      profile_id: profileId,
      conversation_id: conversationId ?? undefined,
      edge_id: edgeId,
      enabled: segment.status !== "active",
      model_profile_id: modelProfileId || undefined,
      model: modelProfileId || undefined,
    })
      .then((result) => {
        setSummary(result);
        setError(null);
      })
      .catch((toggleError) => {
        setError(toggleError instanceof Error ? toggleError.message : "Prompt toggle failed.");
      })
      .finally(() => setBusyEdge(null));
  };

  return (
    <section className="space-y-3" aria-label="Current prompts">
      <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <FileText size={15} className="shrink-0 text-cyan-300" />
              <h4 className="truncate text-sm font-semibold text-zinc-100">現在のプロンプト</h4>
            </div>
            <p className="mt-1 text-[11px] leading-5 text-zinc-500">
              {activeCount} active
              {inactiveCount > 0 ? ` / ${inactiveCount} inactive` : ""}
              {" · "}
              {tokenText(totalTokens)}
            </p>
            <div className="mt-1 flex min-w-0 items-center gap-1 text-[10px] text-zinc-600">
              <span className="min-w-0 truncate">{modelLabel || modelProfileId || "current model"}</span>
              {tokenizerNeedsWarning(summaryTokenizer) && (
                <span title={summaryTokenizerWarning} aria-label={summaryTokenizerWarning}>
                  <AlertTriangle size={12} className="shrink-0 text-amber-300" />
                </span>
              )}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={load}
              disabled={loading}
              className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-50"
              aria-label="Refresh current prompts"
              title="更新"
            >
              <RefreshCw size={14} className={cn(loading && "animate-spin")} />
            </button>
          </div>
        </div>
        {error && (
          <ErrorNotice
            className="mt-2 px-2 py-1.5 text-[11px] leading-5"
            copyLabel="プロンプト概要エラーをコピー"
            message={error}
            severity="warning"
          />
        )}
        {onToggleChatPromptUsage && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-zinc-800/80 bg-black/20 px-2.5 py-2">
            <div className="min-w-0">
              <div className="truncate text-[11px] font-medium text-zinc-300">チャット内の Prompt used</div>
              <div className="mt-0.5 text-[10px] text-zinc-600">
                {showChatPromptUsage ? "メッセージ下に表示中" : "メッセージ下では非表示"}
              </div>
            </div>
            <button
              type="button"
              onClick={() => onToggleChatPromptUsage(!showChatPromptUsage)}
              className={cn(
                "inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-medium",
                showChatPromptUsage
                  ? "border-cyan-500/35 bg-cyan-500/10 text-cyan-100"
                  : "border-zinc-800 text-zinc-500 hover:border-zinc-700 hover:text-zinc-200",
              )}
              aria-pressed={showChatPromptUsage}
              aria-label={showChatPromptUsage ? "Hide Prompt used in chat" : "Show Prompt used in chat"}
              title={showChatPromptUsage ? "Prompt usedをチャット内で非表示" : "Prompt usedをチャット内に表示"}
            >
              {showChatPromptUsage ? <Eye size={12} /> : <EyeOff size={12} />}
              {showChatPromptUsage ? "On" : "Off"}
            </button>
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        {segments.map((segment) => {
          const id = String(segment.id || segment.prompt_id || promptSegmentTitle(segment));
          const expanded = expandedIds.has(id);
          const edgeId = String(segment.edge_id ?? "").trim();
          const toggleable = canToggleSegment(segment);
          return (
            <div key={`${id}-${segment.status}`} className="rounded-lg border border-zinc-800/80 bg-zinc-950/45">
              <div className="flex min-w-0 items-center gap-1.5 px-2 py-1.5">
                <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", segmentStatusTone(segment.status))} />
                <button
                  type="button"
                  onClick={() => toggleExpanded(id)}
                  className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                  aria-expanded={expanded}
                >
                  <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-zinc-200">
                    {promptSegmentTitle(segment)}:
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-zinc-500">{segmentTokenCount(segment).toLocaleString()}</span>
                  <ChevronDown size={13} className={cn("shrink-0 text-zinc-600 transition-transform", expanded && "rotate-180")} />
                </button>
              </div>
              {expanded && (
                <div className="border-t border-zinc-800/70 px-2 py-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded border border-zinc-800 bg-zinc-900/70 px-1.5 py-0.5 text-[10px] text-zinc-400">
                      {segment.status ?? "available"}
                    </span>
                    <span className="rounded border border-zinc-800 bg-zinc-900/70 px-1.5 py-0.5 text-[10px] text-zinc-400">
                      {promptSegmentKindLabel(segment)}
                    </span>
                    <span className="rounded border border-zinc-800 bg-zinc-900/70 px-1.5 py-0.5 font-mono text-[10px] text-zinc-500">
                      {tokenText(segment.tokens)}
                    </span>
                    {tokenizerNeedsWarning(segment.tokenizer) && (
                      <span
                        className="inline-flex items-center gap-1 rounded border border-amber-500/25 bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-100"
                        title={tokenizerWarningText(segment.tokenizer, summaryTokenizerWarning)}
                      >
                        <AlertTriangle size={10} />
                        {tokenizerLabel(segment.tokenizer)}
                      </span>
                    )}
                  </div>
                  <p className="mt-2 text-[11px] leading-5 text-zinc-400">{compactSegmentReason(segment)}</p>
                  <div className="mt-2 flex min-w-0 items-center gap-1.5 text-[10px] text-zinc-600">
                    <ShieldCheck size={11} className="shrink-0 text-emerald-300/80" />
                    <span className="min-w-0 truncate">{sourceLine(segment)}</span>
                  </div>
                  {(segment.preview || segment.text) && (
                    <pre className="mt-2 max-h-24 overflow-auto whitespace-pre-wrap rounded-md border border-zinc-800 bg-black/25 p-2 font-mono text-[10px] leading-5 text-zinc-300">
                      {segment.preview || segment.text}
                    </pre>
                  )}
                  <div className="mt-2 flex items-center justify-between gap-2">
                    <button
                      type="button"
                      onClick={() => toggleSegment(segment)}
                      disabled={!toggleable || busyEdge === edgeId}
                      className={cn(
                        "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-medium",
                        toggleable
                          ? "border-zinc-800 text-zinc-300 hover:border-zinc-600 hover:text-zinc-100"
                          : "cursor-not-allowed border-zinc-900 text-zinc-700",
                      )}
                      title={toggleable ? "AI Input Graph disabled_edgesで切り替え" : "このプロンプトは無効化できません"}
                    >
                      {segment.status === "active" ? <ToggleRight size={13} /> : <ToggleLeft size={13} />}
                      {segment.status === "active" ? "Disable" : "Enable"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {!segments.length && !loading && (
        <div className="rounded-lg border border-dashed border-zinc-800 px-3 py-6 text-center text-xs leading-5 text-zinc-500">
          この会話で使われるプロンプトはまだ読み込まれていません。
        </div>
      )}
    </section>
  );
}
