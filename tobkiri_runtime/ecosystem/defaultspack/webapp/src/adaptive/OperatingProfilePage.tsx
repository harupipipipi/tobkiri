import { BrainCircuit, CheckCircle2, KeyRound, Save, ShieldCheck, UserRound } from "lucide-react";
import { useEffect, useState } from "react";

import type { AdaptiveOperatingProfile } from "../lib/adaptiveApi";
import { fetchAdaptiveOperatingProfile, saveAdaptiveOperatingProfile } from "../lib/adaptiveApi";
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
  toneForRisk,
} from "./AdaptivePrimitives";
import { demoOperatingProfile } from "./demoData";
import { useAdaptiveResource } from "./useAdaptiveResource";

const autonomyOptions = [
  { value: "draft", label: "Draft only" },
  { value: "confirm", label: "Ask before acting" },
  { value: "supervised", label: "Supervised run" },
  { value: "autonomous", label: "Autonomous inside policy" },
] as const;

const finalizationActions = ["merge", "commit", "push", "publish", "delivery"] as const;

const defaultReviewPolicy: NonNullable<AdaptiveOperatingProfile["reviewPolicy"]> = {
  mode: "off",
  reviewerProfile: "",
  requireSeparateRun: true,
  appliesTo: [],
  storeFindings: true,
};

export function OperatingProfilePage({ initialProfile }: { initialProfile?: AdaptiveOperatingProfile }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoOperatingProfile,
    initialData: initialProfile,
    load: fetchAdaptiveOperatingProfile,
  });
  const initialDraft = initialProfile ?? demoOperatingProfile;
  const [summaryDraft, setSummaryDraft] = useState(initialDraft.summary);
  const [autonomyDraft, setAutonomyDraft] = useState(initialDraft.autonomy.level);
  const [reviewPolicyDraft, setReviewPolicyDraft] = useState(initialDraft.reviewPolicy ?? defaultReviewPolicy);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!data) return;
    setSummaryDraft(data.summary);
    setAutonomyDraft(data.autonomy.level);
    setReviewPolicyDraft(data.reviewPolicy ?? defaultReviewPolicy);
  }, [data]);

  const handleSave = async () => {
    if (!data) {
      setSaveError("Cannot save until the adaptive API returns a profile.");
      setSaveStatus(null);
      return;
    }
    setSaveError(null);
    setSaveStatus("Saving profile draft...");
    try {
      await saveAdaptiveOperatingProfile({
        ...data,
        summary: summaryDraft,
        autonomy: {
          ...data.autonomy,
          level: autonomyDraft,
          label: autonomyOptions.find((option) => option.value === autonomyDraft)?.label ?? data.autonomy.label,
        },
        reviewPolicy: reviewPolicyDraft,
      });
      setSaveStatus("Profile settings saved and activated.");
    } catch (err) {
      setSaveStatus(null);
      setSaveError(`Kept local draft. ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive operating profile">
      <SurfaceHeader
        eyebrow="Adaptive runtime"
        title="Operating Profile"
        description="Review the assistant role, autonomy, approval policy, privacy posture, and pack recommendations as one reusable profile."
        action={<ToneBadge tone={status === "live" ? "good" : status === "error" ? "danger" : "warning"}>{status === "live" ? "Live" : status === "error" ? "API error" : "Placeholder"}</ToneBadge>}
      />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {!data ? (
        <AdaptiveEmptyState>Adaptive operating profile is unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
        <>

      <div className="grid gap-0 border-t border-zinc-800/70 xl:grid-cols-[1.15fr_0.85fr]">
        <div className={adaptiveSectionClass}>
          <div className="mb-3 flex items-center gap-2">
            <UserRound size={15} className="text-cyan-200" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-zinc-50">{data.name}</h2>
          </div>
          <label className="block">
            <span className="text-xs font-medium text-zinc-400">Profile summary</span>
            <textarea
              value={summaryDraft}
              onChange={(event) => setSummaryDraft(event.target.value)}
              className="mt-2 min-h-24 w-full rounded-md border border-zinc-800 bg-zinc-950/60 p-3 text-sm leading-6 text-zinc-100 outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
              aria-label="Operating profile summary"
            />
          </label>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <label className="block">
              <span className="text-xs font-medium text-zinc-400">Autonomy mode</span>
              <select
                value={autonomyDraft}
                onChange={(event) => setAutonomyDraft(event.target.value as typeof autonomyDraft)}
                className="mt-2 h-9 w-full rounded-md border border-zinc-800 bg-zinc-950/60 px-2 text-sm text-zinc-100 outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
                aria-label="Autonomy mode"
              >
                {autonomyOptions.map((option) => (
                  <option key={option.value} value={option.value} className="bg-zinc-950">
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Review cadence</p>
              <p className="mt-2 text-sm text-zinc-100">{data.review.cadence}</p>
            </div>
          </div>
          <fieldset className="mt-4 rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">Agent review gate</legend>
            <div className="grid gap-3 md:grid-cols-2">
              <label className="block">
                <span className="text-xs font-medium text-zinc-400">Enforcement</span>
                <select
                  value={reviewPolicyDraft.mode}
                  onChange={(event) => setReviewPolicyDraft((current) => ({ ...current, mode: event.target.value as typeof current.mode }))}
                  className="mt-2 h-9 w-full rounded-md border border-zinc-800 bg-zinc-950/60 px-2 text-sm text-zinc-100 outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
                  aria-label="Agent review gate enforcement"
                >
                  <option value="off">Off</option>
                  <option value="warning">Warning</option>
                  <option value="blocking">Blocking</option>
                </select>
              </label>
              <label className="block">
                <span className="text-xs font-medium text-zinc-400">Reviewer profile</span>
                <input
                  value={reviewPolicyDraft.reviewerProfile}
                  onChange={(event) => setReviewPolicyDraft((current) => ({ ...current, reviewerProfile: event.target.value }))}
                  disabled={reviewPolicyDraft.mode === "off"}
                  className="mt-2 h-9 w-full rounded-md border border-zinc-800 bg-zinc-950/60 px-2 text-sm text-zinc-100 outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 disabled:opacity-50"
                  placeholder="reviewer_agent"
                  aria-label="Agent reviewer profile"
                />
              </label>
            </div>
            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-2" aria-label="Reviewed finalization actions">
              {finalizationActions.map((action) => (
                <label key={action} className="flex items-center gap-2 text-xs text-zinc-300">
                  <input
                    type="checkbox"
                    checked={reviewPolicyDraft.appliesTo.includes(action)}
                    disabled={reviewPolicyDraft.mode === "off"}
                    onChange={(event) => setReviewPolicyDraft((current) => ({
                      ...current,
                      appliesTo: event.target.checked
                        ? [...new Set([...current.appliesTo, action])]
                        : current.appliesTo.filter((item) => item !== action),
                    }))}
                  />
                  {action}
                </label>
              ))}
            </div>
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
              <label className="flex items-center gap-2 text-xs text-zinc-300">
                <input
                  type="checkbox"
                  checked={reviewPolicyDraft.requireSeparateRun}
                  disabled={reviewPolicyDraft.mode === "off"}
                  onChange={(event) => setReviewPolicyDraft((current) => ({ ...current, requireSeparateRun: event.target.checked }))}
                />
                Require a separate reviewer run
              </label>
              <label className="flex items-center gap-2 text-xs text-zinc-300">
                <input
                  type="checkbox"
                  checked={reviewPolicyDraft.storeFindings}
                  disabled={reviewPolicyDraft.mode === "off"}
                  onChange={(event) => setReviewPolicyDraft((current) => ({ ...current, storeFindings: event.target.checked }))}
                />
                Attach findings to the run
              </label>
            </div>
            <p className="mt-3 text-xs leading-5 text-zinc-500">
              Blocking gates pause the selected final step until the configured profile submits an Authority-bound review for the exact artifact.
            </p>
          </fieldset>
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" className={adaptivePrimaryControlClass} onClick={handleSave} aria-label="Save operating profile draft">
              <Save size={14} aria-hidden="true" />
              Save and activate
            </button>
            <button type="button" className={adaptiveControlClass} onClick={refresh} aria-label="Reload operating profile">
              Reload
            </button>
          </div>
          {saveError ? (
            <ErrorNotice
              className="mt-2 rounded-md p-3 text-xs"
              copyLabel="Copy operating profile save error"
              copyText={saveError}
              errorIcon="operating-profile-save"
              message={saveError}
            />
          ) : null}
          {saveStatus ? <p className="mt-2 rounded-md border border-zinc-800 bg-zinc-950/45 px-3 py-2 text-xs text-zinc-300">{saveStatus}</p> : null}
        </div>

        <aside className={adaptiveSectionClass} aria-label="Profile guardrails">
          <div className="grid gap-3">
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                <BrainCircuit size={14} aria-hidden="true" />
                Focus areas
              </div>
              <div className="flex flex-wrap gap-2">
                {data.focusAreas.map((area) => (
                  <ToneBadge key={area} tone="info">{area}</ToneBadge>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                <ShieldCheck size={14} aria-hidden="true" />
                Boundaries
              </div>
              <ul className="space-y-2">
                {data.boundaries.map((boundary) => (
                  <li key={boundary} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs leading-5 text-zinc-300">{boundary}</li>
                ))}
              </ul>
            </div>
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                <KeyRound size={14} aria-hidden="true" />
                Approval policy
              </div>
              <div className="space-y-2">
                {data.approvalPolicy.map((permission) => (
                  <div key={permission.id} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-xs font-semibold text-zinc-100">{permission.label}</p>
                      <ToneBadge tone={toneForRisk(permission.risk)}>{permission.risk}</ToneBadge>
                    </div>
                    <p className="mt-1 text-xs text-zinc-500">{permission.mode}</p>
                  </div>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                <CheckCircle2 size={14} aria-hidden="true" />
                Pack recommendations
              </div>
              <div className="flex flex-wrap gap-2">
                {data.packRecommendations.map((pack) => (
                  <ToneBadge key={pack.id} tone={toneForRisk(pack.status)}>{pack.label}</ToneBadge>
                ))}
              </div>
            </div>
          </div>
        </aside>
      </div>
        </>
      )}
    </section>
  );
}
