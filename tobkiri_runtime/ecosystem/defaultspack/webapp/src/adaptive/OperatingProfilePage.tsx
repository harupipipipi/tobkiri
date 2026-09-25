import { BrainCircuit, CheckCircle2, KeyRound, Save, ShieldCheck, UserRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { AdaptiveOperatingProfile } from "../lib/adaptiveApi";
import {
  AdaptiveApiError,
  createAdaptiveRequestId,
  fetchAdaptiveOperatingProfile,
  saveAdaptiveOperatingProfile,
} from "../lib/adaptiveApi";
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
import {
  adaptiveDraftKey,
  clearAdaptiveDraft,
  loadAdaptiveDraft,
  saveAdaptiveDraft,
} from "./adaptiveDraftStore";
import { useAdaptiveResource } from "./useAdaptiveResource";

const autonomyOptions = [
  { value: "draft", label: "Draft only" },
  { value: "confirm", label: "Ask before acting" },
  { value: "supervised", label: "Supervised run" },
  { value: "autonomous", label: "Autonomous inside policy" },
] as const;

export function OperatingProfilePage({ initialProfile }: { initialProfile?: AdaptiveOperatingProfile }) {
  const { data, status, error, refresh, updateData } = useAdaptiveResource({
    demoData: demoOperatingProfile,
    initialData: initialProfile,
    load: fetchAdaptiveOperatingProfile,
  });
  const initialDraft = initialProfile ?? demoOperatingProfile;
  const [summaryDraft, setSummaryDraft] = useState(initialDraft.summary);
  const [autonomyDraft, setAutonomyDraft] = useState(initialDraft.autonomy.level);
  const [baseRevision, setBaseRevision] = useState(initialDraft.revision);
  const [draftDirty, setDraftDirty] = useState(false);
  const [draftState, setDraftState] = useState<"confirmed" | "unsaved" | "saving" | "failed" | "offline" | "conflict">("confirmed");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [conflictRevision, setConflictRevision] = useState<number | null>(null);
  const [reloadPrompt, setReloadPrompt] = useState(false);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const restoredKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!data) return;
    const key = adaptiveDraftKey("operating-profile", data.id);
    if (restoredKeyRef.current !== key) {
      restoredKeyRef.current = key;
      const restored = loadAdaptiveDraft<{ summary: string; autonomyLevel: typeof autonomyDraft }>(key);
      if (restored && restored.resourceId === data.resourceId) {
        setSummaryDraft(restored.value.summary);
        setAutonomyDraft(restored.value.autonomyLevel);
        setBaseRevision(restored.baseRevision);
        setRequestId(restored.requestId ?? null);
        setDraftDirty(true);
        setDraftState(restored.baseRevision === data.revision ? "unsaved" : "conflict");
        setConflictRevision(restored.baseRevision === data.revision ? null : data.revision);
        setSaveStatus(restored.baseRevision === data.revision
          ? "Recovered an unsaved local draft."
          : "Recovered a draft based on an older backend revision. Choose how to resolve it.");
        return;
      }
    }
    if (draftDirty) {
      if (data.revision !== baseRevision) {
        setDraftState("conflict");
        setConflictRevision(data.revision);
        setSaveStatus("The backend profile changed while this local draft was unsaved.");
      }
      return;
    }
    setSummaryDraft(data.summary);
    setAutonomyDraft(data.autonomy.level);
    setBaseRevision(data.revision);
    setDraftState("confirmed");
    setConflictRevision(null);
  }, [baseRevision, data, draftDirty]);

  useEffect(() => {
    if (!draftDirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [draftDirty]);

  const persistDraft = (
    summary: string,
    autonomyLevel: typeof autonomyDraft,
    options: {
      nextBaseRevision?: number;
      nextRequestId?: string | null;
      nextState?: typeof draftState;
      nextStatus?: string;
    } = {},
  ) => {
    if (!data) return;
    const stored = saveAdaptiveDraft(
      adaptiveDraftKey("operating-profile", data.id),
      {
        baseRevision: options.nextBaseRevision ?? baseRevision,
        requestId: options.nextRequestId === undefined ? requestId : options.nextRequestId,
        resourceId: data.resourceId,
        updatedAt: new Date().toISOString(),
        value: { summary, autonomyLevel },
      },
    );
    setDraftDirty(true);
    setDraftState(options.nextState ?? (stored ? "unsaved" : "offline"));
    setSaveStatus(options.nextStatus ?? (stored
      ? "Unsaved draft stored locally."
      : "Unsaved draft is only in this tab because local storage is unavailable."));
  };

  const handleSave = async () => {
    if (!data) {
      setSaveError("Cannot save until the adaptive API returns a profile.");
      setSaveStatus(null);
      return;
    }
    setSaveError(null);
    const nextRequestId = requestId ?? createAdaptiveRequestId(`operating-profile-${data.id}`);
    setRequestId(nextRequestId);
    persistDraft(summaryDraft, autonomyDraft, {
      nextRequestId,
      nextState: "saving",
      nextStatus: `Saving profile draft against revision ${baseRevision}...`,
    });
    setDraftState("saving");
    setSaveStatus(`Saving profile draft against revision ${baseRevision}...`);
    try {
      const saved = await saveAdaptiveOperatingProfile({
        ...data,
        summary: summaryDraft,
        autonomy: {
          ...data.autonomy,
          level: autonomyDraft,
          label: autonomyOptions.find((option) => option.value === autonomyDraft)?.label ?? data.autonomy.label,
        },
      }, { expectedRevision: baseRevision, requestId: nextRequestId });
      updateData(saved);
      setBaseRevision(saved.revision);
      setDraftDirty(false);
      setDraftState("confirmed");
      setRequestId(null);
      setConflictRevision(null);
      clearAdaptiveDraft(adaptiveDraftKey("operating-profile", data.id));
      setSaveStatus(`Profile draft confirmed at revision ${saved.revision}.`);
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      const nextState = err instanceof AdaptiveApiError && err.code === "REVISION_CONFLICT"
        ? "conflict"
        : err instanceof AdaptiveApiError && err.status > 0
          ? "failed"
          : "offline";
      if (err instanceof AdaptiveApiError && err.code === "REVISION_CONFLICT") {
        const latestRevision = Number(err.details.current_revision);
        setConflictRevision(Number.isSafeInteger(latestRevision) && latestRevision >= 0
          ? latestRevision
          : data.revision);
      }
      persistDraft(summaryDraft, autonomyDraft, {
        nextRequestId,
        nextState,
        nextStatus: `Draft remains unsaved. ${detail}`,
      });
      setSaveStatus(null);
      setSaveError(`Kept local draft. ${detail}`);
    }
  };

  const discardAndReload = () => {
    if (!data) return;
    clearAdaptiveDraft(adaptiveDraftKey("operating-profile", data.id));
    setSummaryDraft(data.summary);
    setAutonomyDraft(data.autonomy.level);
    setBaseRevision(data.revision);
    setDraftDirty(false);
    setDraftState("confirmed");
    setRequestId(null);
    setConflictRevision(null);
    setReloadPrompt(false);
    setSaveStatus("Local draft discarded. Reloading the backend profile.");
    refresh();
  };

  const keepDraftOnLatestRevision = () => {
    if (!data) return;
    const latestRevision = conflictRevision ?? data.revision;
    setBaseRevision(latestRevision);
    setRequestId(null);
    setConflictRevision(null);
    persistDraft(summaryDraft, autonomyDraft, {
      nextBaseRevision: latestRevision,
      nextRequestId: null,
      nextState: "unsaved",
      nextStatus: `Local draft rebased for explicit retry against revision ${latestRevision}.`,
    });
  };

  const requestReload = () => {
    if (draftDirty) setReloadPrompt(true);
    else refresh();
  };

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive operating profile">
      <SurfaceHeader
        eyebrow="Adaptive runtime"
        title="Operating Profile"
        description="Review the assistant role, autonomy, approval policy, privacy posture, and pack recommendations as one reusable profile."
        action={<ToneBadge tone={draftState === "confirmed" && status === "live" ? "good" : draftState === "conflict" || draftState === "failed" ? "danger" : "warning"}>{draftState === "confirmed" && status === "live" ? "Confirmed" : draftState === "saving" ? "Pending" : draftState === "conflict" ? "Conflict" : draftState === "offline" ? "Offline draft" : draftState === "failed" ? "Save failed" : "Unsaved"}</ToneBadge>}
      />
      <ResourceBanner status={status} error={error} onRefresh={requestReload} />
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
              onChange={(event) => {
                setSummaryDraft(event.target.value);
                persistDraft(event.target.value, autonomyDraft);
              }}
              className="mt-2 min-h-24 w-full rounded-md border border-zinc-800 bg-zinc-950/60 p-3 text-sm leading-6 text-zinc-100 outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
              aria-label="Operating profile summary"
            />
          </label>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <label className="block">
              <span className="text-xs font-medium text-zinc-400">Autonomy mode</span>
              <select
                value={autonomyDraft}
                onChange={(event) => {
                  const next = event.target.value as typeof autonomyDraft;
                  setAutonomyDraft(next);
                  persistDraft(summaryDraft, next);
                }}
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
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              className={adaptivePrimaryControlClass}
              onClick={() => void handleSave()}
              disabled={draftState === "saving"}
              aria-label="Save operating profile draft"
            >
              <Save size={14} aria-hidden="true" />
              Save draft
            </button>
            <button type="button" className={adaptiveControlClass} onClick={requestReload} aria-label="Reload operating profile">
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
          {reloadPrompt ? (
            <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 p-3" role="alert">
              <p className="text-xs text-amber-100">This profile has an unsaved local draft. Save, discard, or cancel before reloading.</p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button type="button" className={adaptivePrimaryControlClass} onClick={() => void handleSave()}>Save</button>
                <button type="button" className={adaptiveControlClass} onClick={discardAndReload}>Discard and reload</button>
                <button type="button" className={adaptiveControlClass} onClick={() => setReloadPrompt(false)}>Cancel</button>
              </div>
            </div>
          ) : null}
          {draftState === "conflict" ? (
            <div className="mt-3 rounded-md border border-rose-500/30 bg-rose-500/10 p-3" role="alert">
              <p className="text-xs text-rose-100">Backend revision {conflictRevision ?? data.revision} differs from this draft base revision {baseRevision}.</p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button type="button" className={adaptiveControlClass} onClick={keepDraftOnLatestRevision}>Keep draft for retry</button>
                <button type="button" className={adaptiveControlClass} onClick={discardAndReload}>Use backend version</button>
              </div>
            </div>
          ) : null}
          {saveStatus ? <p className="mt-2 rounded-md border border-zinc-800 bg-zinc-950/45 px-3 py-2 text-xs text-zinc-300" role="status" aria-live="polite">{saveStatus}</p> : null}
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
