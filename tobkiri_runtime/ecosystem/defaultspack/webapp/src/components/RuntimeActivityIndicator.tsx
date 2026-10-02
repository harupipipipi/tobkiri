import { useEffect, useState } from "react";

import { cn } from "../lib/cn";
import { elapsedDurationLabel } from "../lib/duration";

/** Tick only while an actual runtime activity is visible. */
export function useActivityClock(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return undefined;
    let interval: number | undefined;
    const refresh = () => {
      if (interval !== undefined) window.clearInterval(interval);
      interval = undefined;
      if (document.visibilityState !== "visible") return;
      setNow(Date.now());
      interval = window.setInterval(() => setNow(Date.now()), 1000);
    };
    refresh();
    document.addEventListener("visibilitychange", refresh);
    return () => {
      if (interval !== undefined) window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [enabled]);
  return now;
}

function motionAllowed(): boolean {
  return typeof window !== "undefined" && typeof document !== "undefined"
    && document.visibilityState === "visible"
    && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Beautiful UI's pixel-grid loading pattern, driven by runtime state only. */
export function RuntimeActivityIndicator({
  label,
  startedAt,
  compact = false,
}: { label: string; startedAt?: number | null; compact?: boolean }) {
  const now = useActivityClock(Boolean(startedAt));
  const elapsed = startedAt ? elapsedDurationLabel(startedAt, now) : "";
  const [animate, setAnimate] = useState(motionAllowed);
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const refresh = () => setAnimate(motionAllowed());
    refresh();
    query.addEventListener("change", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      query.removeEventListener("change", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);
  return (
    <div
      className={cn("rumi-activity-loading flex max-w-full items-center gap-3 rounded-xl border border-[var(--rumi-border-subtle)] px-3 py-2.5 text-zinc-300", compact ? "w-fit" : "w-[min(680px,calc(100vw-48px))]")}
      role="status"
      aria-live="polite"
      aria-label={label}
      data-runtime-activity="running"
    >
      <span className={cn("rumi-loading-pixels", animate && "is-animated")} aria-hidden="true">
        {Array.from({ length: 9 }, (_, index) => <span key={index} />)}
      </span>
      <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="min-w-0 flex-1 break-words text-[13px] font-medium leading-5 text-zinc-200">{label}</span>
        {elapsed && <span aria-hidden="true" className="shrink-0 font-mono text-[11px] tabular-nums text-zinc-500">{elapsed}</span>}
      </span>
    </div>
  );
}
