import { Database, FileSearch, Gauge, GitBranch, ShieldCheck } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import type { AdaptiveContextBudget, AdaptiveEvidenceBundle, AdaptiveRepositoryMap } from "../lib/adaptiveApi";
import { fetchAdaptiveContextBudget, fetchAdaptiveEvidence, fetchAdaptiveRepositoryMap } from "../lib/adaptiveApi";
import {
  AdaptiveEmptyState,
  ProgressBar,
  ResourceBanner,
  SurfaceHeader,
  ToneBadge,
  adaptiveControlClass,
  adaptivePageClass,
  adaptivePanelClass,
  adaptiveSectionClass,
  toneForRisk,
} from "./AdaptivePrimitives";
import { useAdaptiveTabs } from "./AdaptiveTabs";
import { demoContextBudget, demoEvidenceBundle, demoRepositoryMap } from "./demoData";
import { useAdaptiveResource } from "./useAdaptiveResource";

export function EvidenceViewer({ initialBundle }: { initialBundle?: AdaptiveEvidenceBundle }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoEvidenceBundle,
    initialData: initialBundle,
    load: fetchAdaptiveEvidence,
  });
  const [selectedId, setSelectedId] = useState(initialBundle?.selectedId ?? initialBundle?.items[0]?.id ?? demoEvidenceBundle.selectedId ?? demoEvidenceBundle.items[0]?.id ?? null);
  const selected = useMemo(
    () => data?.items.find((item) => item.id === selectedId) ?? data?.items[0] ?? null,
    [data, selectedId],
  );
  const evidenceIds = useMemo(() => data?.items.map((item) => item.id) ?? [], [data]);
  const selectEvidence = useCallback((id: string) => setSelectedId(id), []);
  const evidenceTabs = useAdaptiveTabs({
    ids: evidenceIds,
    selectedId,
    onSelect: selectEvidence,
    idPrefix: "adaptive-evidence",
    orientation: "vertical",
  });

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive evidence viewer">
      <SurfaceHeader eyebrow="Adaptive runtime" title="Evidence Viewer" description="Inspect source summaries, confidence, redactions, and linked proof for reviewable decisions." />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {!data ? (
        <AdaptiveEmptyState>Adaptive evidence is unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
      <div className="grid border-t border-zinc-800/70 lg:grid-cols-[290px_1fr]">
        <div className={adaptiveSectionClass} role="tablist" aria-label="Evidence items" aria-orientation="vertical">
          <div className="space-y-2">
            {data.items.map((item) => (
              <button
                key={item.id}
                type="button"
                {...evidenceTabs.tabProps(item.id)}
                className={`w-full rounded-md border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 ${
                  selected?.id === item.id ? "border-cyan-400/40 bg-cyan-400/10" : "border-zinc-800 bg-zinc-950/45 hover:bg-zinc-900"
                }`}
              >
                <span className="block text-sm font-semibold text-zinc-100">{item.title}</span>
                <span className="mt-1 block text-[11px] text-zinc-500">{item.sourceLabel}</span>
              </button>
            ))}
          </div>
        </div>
        <div
          id={selected ? evidenceTabs.panelId(selected.id) : undefined}
          role="tabpanel"
          aria-labelledby={selected ? evidenceTabs.tabId(selected.id) : undefined}
          tabIndex={0}
          className={adaptiveSectionClass}
        >
          {selected ? (
            <article>
              <div className="flex flex-wrap items-center gap-2">
                <FileSearch size={15} className="text-cyan-200" aria-hidden="true" />
                <h2 className="text-sm font-semibold text-zinc-50">{selected.title}</h2>
                <ToneBadge tone="info">{selected.kind}</ToneBadge>
              </div>
              <p className="mt-3 text-sm leading-6 text-zinc-300">{selected.summary}</p>
              <dl className="mt-4 grid gap-2 sm:grid-cols-3">
                <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                  <dt className="text-[10px] uppercase tracking-wide text-zinc-500">Captured</dt>
                  <dd className="mt-1 text-xs text-zinc-100">{selected.capturedAt}</dd>
                </div>
                <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                  <dt className="text-[10px] uppercase tracking-wide text-zinc-500">Confidence</dt>
                  <dd className="mt-1 text-xs text-zinc-100">{Math.round(selected.confidence * 100)}%</dd>
                </div>
                <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                  <dt className="text-[10px] uppercase tracking-wide text-zinc-500">Source</dt>
                  <dd className="mt-1 text-xs text-zinc-100">{selected.sourceLabel}</dd>
                </div>
              </dl>
              <div className="mt-4 flex flex-wrap gap-2">
                {selected.redactions.map((redaction) => (
                  <ToneBadge key={redaction} tone="warning">{redaction}</ToneBadge>
                ))}
              </div>
            </article>
          ) : (
            <p className="text-sm text-zinc-500">No evidence is available.</p>
          )}
        </div>
      </div>
      )}
    </section>
  );
}

export function RepositoryMapPanel({ initialMap }: { initialMap?: AdaptiveRepositoryMap }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoRepositoryMap,
    initialData: initialMap,
    load: fetchAdaptiveRepositoryMap,
  });

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive repository map">
      <SurfaceHeader
        eyebrow="Adaptive runtime"
        title="Repository Map"
        description="Show owned adaptive files, read-only integration context, and repository risks before a task starts."
        action={data?.branch ? <ToneBadge tone="info">{data.branch}</ToneBadge> : null}
      />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {!data ? (
        <AdaptiveEmptyState>Adaptive repository mapping is unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
      <div className={adaptiveSectionClass}>
        <div className="mb-3 flex items-center gap-2">
          <GitBranch size={15} className="text-cyan-200" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-zinc-50">{data.rootLabel}</h2>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {data.sections.map((section) => (
            <section key={section.id} aria-label={section.label}>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{section.label}</h3>
              <p className="mt-1 text-xs leading-5 text-zinc-500">{section.description}</p>
              <div className="mt-2 space-y-2">
                {section.paths.map((path) => (
                  <div key={path.path} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <p className="break-all font-mono text-xs text-zinc-100">{path.path}</p>
                      <ToneBadge tone={toneForRisk(path.status)}>{path.status.replace("_", " ")}</ToneBadge>
                    </div>
                    <p className="mt-1 text-xs text-zinc-500">{path.role}</p>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
        <div className="mt-4 rounded-md border border-amber-500/20 bg-amber-500/5 p-3">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-amber-200">
            <ShieldCheck size={14} aria-hidden="true" />
            Guardrails
          </div>
          <ul className="space-y-1 text-xs leading-5 text-zinc-300">
            {data.risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
        </div>
      </div>
      )}
    </section>
  );
}

export function ContextBudgetPanel({ initialBudget }: { initialBudget?: AdaptiveContextBudget }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoContextBudget,
    initialData: initialBudget,
    load: fetchAdaptiveContextBudget,
  });
  const available = data ? Math.max(0, data.limit - data.used - data.reserved) : 0;

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive context budget">
      <SurfaceHeader
        eyebrow="Adaptive runtime"
        title="Context Budget"
        description="Track used, reserved, and available context before the runtime expands repository or evidence detail."
        action={data ? <ToneBadge tone={toneForRisk(data.riskLevel)}>{data.riskLevel}</ToneBadge> : null}
      />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {!data ? (
        <AdaptiveEmptyState>Adaptive context budget is unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
      <div className={adaptiveSectionClass}>
        <div className="mb-4 flex items-center gap-2">
          <Gauge size={15} className="text-cyan-200" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-zinc-50">Budget allocation</h2>
        </div>
        <ProgressBar value={data.used + data.reserved} max={data.limit} label="Allocated context" tone={toneForRisk(data.riskLevel)} />
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-[10px] uppercase tracking-wide text-zinc-500">Used</p>
            <p className="mt-1 font-mono text-sm text-zinc-100">{data.used}</p>
          </div>
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-[10px] uppercase tracking-wide text-zinc-500">Reserved</p>
            <p className="mt-1 font-mono text-sm text-amber-200">{data.reserved}</p>
          </div>
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-[10px] uppercase tracking-wide text-zinc-500">Available</p>
            <p className="mt-1 font-mono text-sm text-emerald-200">{available}</p>
          </div>
        </div>
        <div className="mt-4 grid gap-2 md:grid-cols-2">
          {data.segments.map((segment) => (
            <div key={segment.id} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-semibold text-zinc-100">{segment.label}</p>
                <ToneBadge tone={segment.tone ?? "neutral"}>{segment.tokens}</ToneBadge>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            <Database size={14} aria-hidden="true" />
            Compression plan
          </div>
          <ul className="space-y-1 text-xs leading-5 text-zinc-300">
            {data.compressionPlan.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </div>
      )}
    </section>
  );
}
