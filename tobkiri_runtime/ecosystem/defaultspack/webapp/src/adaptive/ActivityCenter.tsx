import { AlertTriangle, CheckCircle2, Clock3, ListFilter, RotateCw, ShieldAlert } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import type { AdaptiveActivityItem, AdaptiveActivityState } from "../lib/adaptiveApi";
import { fetchAdaptiveActivity } from "../lib/adaptiveApi";
import {
  AdaptiveEmptyState,
  AdaptiveStatusMessage,
  MetricTile,
  ResourceBanner,
  SurfaceHeader,
  ToneBadge,
  adaptiveControlClass,
  adaptivePageClass,
  adaptivePanelClass,
  adaptiveSectionClass,
  readableCapability,
  toneForRisk,
} from "./AdaptivePrimitives";
import { useAdaptiveTabs } from "./AdaptiveTabs";
import { demoActivityState } from "./demoData";
import { useAdaptiveResource } from "./useAdaptiveResource";

type ActivityFilter = "all" | "running" | "needs_review" | "blocked" | "done";

const filters: Array<{ id: ActivityFilter; label: string }> = [
  { id: "all", label: "All" },
  { id: "running", label: "Running" },
  { id: "needs_review", label: "Needs review" },
  { id: "blocked", label: "Blocked" },
  { id: "done", label: "Done" },
];
const filterIds = filters.map((item) => item.id);

function statusLabel(status: string): string {
  return status.replace(/_/g, " ");
}

function ActivityRow({ item }: { item: AdaptiveActivityItem }) {
  return (
    <article className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3" aria-label={item.title}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-zinc-100">{item.title}</h3>
            <ToneBadge tone={toneForRisk(item.status)}>{statusLabel(item.status)}</ToneBadge>
            {item.requiresReview ? <ToneBadge tone="warning">Review</ToneBadge> : null}
          </div>
          <p className="mt-1 text-xs leading-5 text-zinc-400">{item.summary}</p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 text-[11px] text-zinc-500">
          <span>{item.startedAt}</span>
          <span>{item.evidenceCount} evidence</span>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
        <span className="rounded-md border border-zinc-800 bg-black/20 px-2 py-1">{item.actor}</span>
        <span className="rounded-md border border-zinc-800 bg-black/20 px-2 py-1">{readableCapability(item.toolLabel, "Runtime action")}</span>
      </div>
    </article>
  );
}

export function ActivityCenter({ initialState }: { initialState?: AdaptiveActivityState }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoActivityState,
    initialData: initialState,
    load: fetchAdaptiveActivity,
  });
  const [filter, setFilter] = useState<ActivityFilter>("all");
  const selectFilter = useCallback((nextFilter: ActivityFilter) => setFilter(nextFilter), []);
  const filterTabs = useAdaptiveTabs({
    ids: filterIds,
    selectedId: filter,
    onSelect: selectFilter,
    idPrefix: "adaptive-activity-filter",
  });
  const filteredItems = useMemo(() => {
    const items = data?.items ?? [];
    if (filter === "all") return items;
    return items.filter((item) => item.status === filter);
  }, [data, filter]);

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive activity center">
      <SurfaceHeader
        eyebrow="Adaptive runtime"
        title="Activity Center"
        description="Track running work, review requests, blockers, evidence counts, and completed activity without exposing internal tool identifiers."
        action={
          <button
            type="button"
            className={adaptiveControlClass}
            onClick={refresh}
            aria-label="Refresh activity center"
            aria-busy={status === "loading"}
            disabled={status === "loading"}
          >
            <RotateCw size={14} aria-hidden="true" />
            Refresh
          </button>
        }
      />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {!data ? (
        <AdaptiveEmptyState>Adaptive activity is unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
        <>

      <div className={adaptiveSectionClass}>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <MetricTile label="Running" value={data.counters.running} tone="info" />
          <MetricTile label="Needs review" value={data.counters.needsReview} tone="warning" />
          <MetricTile label="Blocked" value={data.counters.blocked} tone="danger" />
          <MetricTile label="Completed today" value={data.counters.completedToday} tone="good" />
        </div>
      </div>

      <div className="grid gap-0 border-t border-zinc-800/70 xl:grid-cols-[1fr_330px]">
        <div className={adaptiveSectionClass}>
          <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <ListFilter size={15} className="text-cyan-200" aria-hidden="true" />
              <h2 className="text-sm font-semibold text-zinc-50">Runtime work</h2>
            </div>
            <div
              className="flex flex-wrap gap-1"
              role="tablist"
              aria-label="Activity filters"
              aria-orientation="horizontal"
            >
              {filters.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  {...filterTabs.tabProps(item.id)}
                  className={`${adaptiveControlClass} ${filter === item.id ? "border-cyan-400/40 bg-cyan-400/10 text-cyan-100" : ""}`}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
          <div
            id={filterTabs.panelId(filter)}
            role="tabpanel"
            aria-labelledby={filterTabs.tabId(filter)}
            tabIndex={0}
            className="space-y-2"
          >
            <AdaptiveStatusMessage className="sr-only">
              {filteredItems.length} activity {filteredItems.length === 1 ? "item" : "items"} shown for {filters.find((item) => item.id === filter)?.label ?? filter}.
            </AdaptiveStatusMessage>
            {filteredItems.map((item) => (
              <ActivityRow key={item.id} item={item} />
            ))}
            {filteredItems.length === 0 ? (
              <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-6 text-center text-sm text-zinc-500">No activity for this filter.</div>
            ) : null}
          </div>
        </div>

        <aside className={adaptiveSectionClass} aria-label="Review queue">
          <div className="mb-3 flex items-center gap-2">
            <ShieldAlert size={15} className="text-amber-200" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-zinc-50">Review queue</h2>
          </div>
          <AdaptiveStatusMessage className="sr-only">
            {data.reviewQueue.length} review {data.reviewQueue.length === 1 ? "request" : "requests"} available.
          </AdaptiveStatusMessage>
          <div className="space-y-2">
            {data.reviewQueue.map((item) => (
              <article key={item.id} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                <div className="flex items-start justify-between gap-2">
                  <h3 className="text-sm font-semibold text-zinc-100">{item.title}</h3>
                  <ToneBadge tone={toneForRisk(item.risk)}>{item.risk}</ToneBadge>
                </div>
                <p className="mt-2 text-xs leading-5 text-zinc-400">{item.reason}</p>
                <div className="mt-3 flex items-center justify-between gap-2 text-[11px] text-zinc-500">
                  <span className="flex items-center gap-1">
                    <AlertTriangle size={12} aria-hidden="true" />
                    {item.requestedBy}
                  </span>
                  <span className="flex items-center gap-1">
                    <Clock3 size={12} aria-hidden="true" />
                    {item.ageLabel}
                  </span>
                </div>
                <div className="mt-3 flex gap-2">
                  <button type="button" className={adaptiveControlClass} aria-label={`Open review for ${item.title}`}>Open</button>
                  <button type="button" className={adaptiveControlClass} aria-label={`Mark ${item.title} as reviewed`}>
                    <CheckCircle2 size={13} aria-hidden="true" />
                    Reviewed
                  </button>
                </div>
              </article>
            ))}
          </div>
        </aside>
      </div>
        </>
      )}
    </section>
  );
}
