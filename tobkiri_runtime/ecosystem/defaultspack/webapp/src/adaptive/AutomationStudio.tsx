import { FlaskConical, Play, Plus, Power, RotateCw, Workflow } from "lucide-react";
import { useMemo, useState } from "react";

import type { AdaptiveAutomation, AdaptiveAutomationState } from "../lib/adaptiveApi";
import { fetchAdaptiveAutomations, updateAdaptiveAutomation } from "../lib/adaptiveApi";
import { ErrorNotice } from "../components/ErrorNotice";
import {
  AdaptiveEmptyState,
  ResourceBanner,
  SurfaceHeader,
  ToneBadge,
  adaptiveControlClass,
  adaptivePageClass,
  adaptivePanelClass,
  adaptivePrimaryControlClass,
  adaptiveSectionClass,
  readableCapability,
  toneForRisk,
} from "./AdaptivePrimitives";
import { demoAutomationState } from "./demoData";
import { useAdaptiveResource } from "./useAdaptiveResource";

function AutomationItem({
  automation,
  enabled,
  onToggle,
}: {
  automation: AdaptiveAutomation;
  enabled: boolean;
  onToggle: () => void;
}) {
  return (
    <article className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3" aria-label={automation.name}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-zinc-100">{automation.name}</h3>
            <ToneBadge tone={enabled ? "good" : "neutral"}>{enabled ? "Enabled" : "Paused"}</ToneBadge>
            <ToneBadge tone={toneForRisk(automation.risk)}>{automation.risk}</ToneBadge>
          </div>
          <p className="mt-1 text-xs leading-5 text-zinc-400">{automation.description}</p>
          <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-zinc-500">
            <span className="rounded-md border border-zinc-800 bg-black/20 px-2 py-1">{automation.trigger}</span>
            <span className="rounded-md border border-zinc-800 bg-black/20 px-2 py-1">{automation.schedule}</span>
            <span className="rounded-md border border-zinc-800 bg-black/20 px-2 py-1">{automation.lastRun ?? "Not run yet"}</span>
          </div>
        </div>
        <button
          type="button"
          className={adaptiveControlClass}
          onClick={onToggle}
          aria-pressed={enabled}
          aria-label={`${enabled ? "Pause" : "Enable"} ${automation.name}`}
        >
          <Power size={14} aria-hidden="true" />
          {enabled ? "Pause" : "Enable"}
        </button>
      </div>
      <ol className="mt-3 grid gap-2 md:grid-cols-2">
        {automation.steps.map((step, index) => (
          <li key={step.id} className="rounded-md border border-zinc-800 bg-black/20 p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-xs font-semibold text-zinc-100">{index + 1}. {step.label}</p>
                <p className="mt-1 text-[11px] text-zinc-500">{readableCapability(step.capabilityLabel)}</p>
              </div>
              {step.requiresApproval ? <ToneBadge tone="warning">Approval</ToneBadge> : null}
            </div>
          </li>
        ))}
      </ol>
    </article>
  );
}

export function AutomationStudio({ initialState }: { initialState?: AdaptiveAutomationState }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoAutomationState,
    initialData: initialState,
    load: fetchAdaptiveAutomations,
  });
  const [enabledOverrides, setEnabledOverrides] = useState<Record<string, boolean>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const automations = useMemo(
    () => (data?.automations ?? []).map((automation) => ({
      ...automation,
      enabled: enabledOverrides[automation.id] ?? automation.enabled,
    })),
    [data, enabledOverrides],
  );

  const handleToggle = async (automation: AdaptiveAutomation) => {
    const nextEnabled = !(enabledOverrides[automation.id] ?? automation.enabled);
    setEnabledOverrides((current) => ({ ...current, [automation.id]: nextEnabled }));
    setOperationError(null);
    setMessage(nextEnabled ? "Automation enabled locally." : "Automation paused locally.");
    try {
      await updateAdaptiveAutomation(automation.id, { enabled: nextEnabled });
      setMessage(nextEnabled ? "Automation enabled." : "Automation paused.");
    } catch (err) {
      setMessage(null);
      setOperationError(`Kept local automation state. ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive automation studio">
      <SurfaceHeader
        eyebrow="Adaptive runtime"
        title="Automation Studio"
        description="Draft, simulate, and enable recurring workflows while keeping risky steps behind local review gates."
        action={
          <div className="flex gap-2">
            <button type="button" className={adaptiveControlClass} onClick={refresh} aria-label="Refresh automations">
              <RotateCw size={14} aria-hidden="true" />
              Refresh
            </button>
            <button type="button" className={adaptivePrimaryControlClass} aria-label="Create automation draft">
              <Plus size={14} aria-hidden="true" />
              New draft
            </button>
          </div>
        }
      />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {operationError ? (
        <ErrorNotice
          className="rounded-none border-x-0 border-b-0 px-3 py-2 text-xs"
          copyLabel="Copy automation update error"
          copyText={operationError}
          errorIcon="automation-update"
          message={operationError}
        />
      ) : null}
      {message ? <div className="border-t border-zinc-800/70 bg-zinc-950/60 px-3 py-2 text-xs text-zinc-300">{message}</div> : null}
      {!data ? (
        <AdaptiveEmptyState>Adaptive automations are unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
        <>

      <div className="grid gap-0 border-t border-zinc-800/70 xl:grid-cols-[1fr_330px]">
        <div className={adaptiveSectionClass}>
          <div className="mb-3 flex items-center gap-2">
            <Workflow size={15} className="text-cyan-200" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-zinc-50">Automations</h2>
          </div>
          <div className="space-y-2">
            {automations.map((automation) => (
              <AutomationItem
                key={automation.id}
                automation={automation}
                enabled={automation.enabled}
                onToggle={() => void handleToggle(automation)}
              />
            ))}
          </div>
        </div>

        <aside className={adaptiveSectionClass} aria-label="Automation templates and simulation">
          <div className="mb-3 flex items-center gap-2">
            <FlaskConical size={15} className="text-amber-200" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-zinc-50">Simulation</h2>
          </div>
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-xs font-semibold text-zinc-100">{data.simulation.scenario}</p>
            <p className="mt-2 text-xs leading-5 text-zinc-400">{data.simulation.result}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {data.simulation.approvals.map((approval) => (
                <ToneBadge key={approval} tone="warning">{approval}</ToneBadge>
              ))}
            </div>
            <button type="button" className={`${adaptiveControlClass} mt-3`} aria-label="Run automation simulation">
              <Play size={14} aria-hidden="true" />
              Run simulation
            </button>
          </div>

          <div className="mt-4">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Templates</h3>
            <div className="space-y-2">
              {data.templates.map((template) => (
                <article key={template.id} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                  <p className="text-xs font-semibold text-zinc-100">{template.name}</p>
                  <p className="mt-1 text-xs leading-5 text-zinc-500">{template.description}</p>
                  <button type="button" className={`${adaptiveControlClass} mt-3`} aria-label={`Use ${template.name} template`}>Use template</button>
                </article>
              ))}
            </div>
          </div>
        </aside>
      </div>
        </>
      )}
    </section>
  );
}
