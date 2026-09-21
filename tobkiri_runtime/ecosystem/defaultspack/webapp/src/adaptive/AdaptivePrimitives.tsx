import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";

import { ErrorNotice } from "../components/ErrorNotice";
import type { AdaptiveTone } from "../lib/adaptiveApi";
import type { AdaptiveResourceStatus } from "./useAdaptiveResource";

export const adaptivePageClass =
  "min-h-0 w-full bg-[#09090b] text-zinc-100";

export const adaptivePanelClass =
  "overflow-hidden rounded-lg border border-zinc-800/80 bg-[#0b0b0f]/95 shadow-[0_18px_46px_rgba(0,0,0,0.22)]";

export const adaptiveSectionClass =
  "border-t border-zinc-800/70 p-3 first:border-t-0";

export const adaptiveControlClass =
  "inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/60 px-2.5 text-xs font-medium text-zinc-300 transition hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 disabled:cursor-not-allowed disabled:opacity-50";

export const adaptivePrimaryControlClass =
  "inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-cyan-400/40 bg-cyan-400/10 px-2.5 text-xs font-semibold text-cyan-100 transition hover:bg-cyan-400/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/70 disabled:cursor-not-allowed disabled:opacity-50";

const toneClasses: Record<AdaptiveTone, string> = {
  neutral: "border-zinc-700/80 bg-zinc-800/45 text-zinc-200",
  good: "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
  warning: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  danger: "border-rose-500/30 bg-rose-500/10 text-rose-200",
  info: "border-cyan-500/30 bg-cyan-500/10 text-cyan-200",
};

export function toneForRisk(risk: string | undefined): AdaptiveTone {
  const normalized = risk?.toLowerCase();
  if (normalized === "high" || normalized === "blocked") return "danger";
  if (normalized === "medium" || normalized === "needs_review") return "warning";
  if (normalized === "low" || normalized === "done" || normalized === "enabled") return "good";
  if (normalized === "running" || normalized === "recommended") return "info";
  return "neutral";
}

export function ToneBadge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: AdaptiveTone;
}) {
  return (
    <span className={`inline-flex min-h-6 items-center rounded-md border px-2 py-0.5 text-[11px] font-semibold ${toneClasses[tone]}`}>
      {children}
    </span>
  );
}

export function SurfaceHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 p-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-zinc-500">{eyebrow}</p>
        <h1 className="mt-1 text-base font-semibold text-zinc-50">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-xs leading-5 text-zinc-400">{description}</p> : null}
      </div>
      {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
    </div>
  );
}

export function ResourceBanner({
  status,
  error,
  onRefresh,
}: {
  status: AdaptiveResourceStatus;
  error: string | null;
  onRefresh?: () => void;
}) {
  if (status === "live") {
    return (
      <div className="flex items-center gap-2 border-t border-zinc-800/70 bg-emerald-500/5 px-3 py-2 text-[11px] text-emerald-200">
        <CheckCircle2 size={13} aria-hidden="true" />
        <span>Live adaptive state loaded.</span>
      </div>
    );
  }
  if (status === "loading") {
    return (
      <div className="flex items-center gap-2 border-t border-zinc-800/70 bg-cyan-500/5 px-3 py-2 text-[11px] text-cyan-200">
        <Loader2 size={13} className="animate-spin" aria-hidden="true" />
        <span>Loading adaptive state.</span>
      </div>
    );
  }
  const isError = status === "error";
  const message = isError
    ? `Adaptive API error.${error ? ` ${error}` : ""}`
    : `Local placeholder adaptive state.${error ? ` ${error}` : ""}`;
  if (isError) {
    return (
      <ErrorNotice
        className="rounded-none border-x-0 border-b-0 border-t px-3 py-2 text-[11px]"
        copyLabel="Copy adaptive API error"
        copyText={message}
        errorIcon="adaptive-api"
        message={message}
        messageClassName="whitespace-normal"
        trailing={onRefresh ? (
          <button type="button" className={adaptiveControlClass} onClick={onRefresh} aria-label="Refresh adaptive state">
            Retry
          </button>
        ) : undefined}
      />
    );
  }
  return (
    <div className="flex flex-col gap-2 border-t border-zinc-800/70 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-100 sm:flex-row sm:items-center sm:justify-between">
      <span className="flex min-w-0 items-center gap-2">
        <AlertTriangle size={13} className="shrink-0" aria-hidden="true" />
        <span className="min-w-0 whitespace-normal break-words" title={error ?? undefined}>
          {message}
        </span>
      </span>
      {onRefresh ? (
        <button type="button" className={adaptiveControlClass} onClick={onRefresh} aria-label="Refresh adaptive state">
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function AdaptiveEmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="border-t border-zinc-800/70 p-6 text-center text-sm leading-6 text-zinc-500">
      {children}
    </div>
  );
}

export function MetricTile({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: AdaptiveTone;
}) {
  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-2">
      <p className="text-[10px] uppercase tracking-wide text-zinc-500">{label}</p>
      <p className={`mt-1 text-lg font-semibold ${tone === "danger" ? "text-rose-200" : tone === "warning" ? "text-amber-200" : tone === "good" ? "text-emerald-200" : tone === "info" ? "text-cyan-200" : "text-zinc-100"}`}>
        {value}
      </p>
    </div>
  );
}

export function ProgressBar({
  value,
  max,
  label,
  tone = "info",
}: {
  value: number;
  max: number;
  label: string;
  tone?: AdaptiveTone;
}) {
  const ratio = max > 0 ? Math.max(0, Math.min(100, Math.round((value / max) * 100))) : 0;
  const fill = tone === "danger" ? "bg-rose-300" : tone === "warning" ? "bg-amber-300" : tone === "good" ? "bg-emerald-300" : "bg-cyan-300";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2 text-[11px] text-zinc-400">
        <span>{label}</span>
        <span className="font-mono">{ratio}%</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-zinc-800" role="progressbar" aria-label={label} aria-valuenow={ratio} aria-valuemin={0} aria-valuemax={100}>
        <div className={`h-full ${fill}`} style={{ width: `${ratio}%` }} />
      </div>
    </div>
  );
}

export function readableCapability(label?: string | null, fallback = "Adaptive capability"): string {
  const trimmed = label?.trim();
  return trimmed || fallback;
}
