import { Save } from "lucide-react";
import { useEffect, useState } from "react";

import {
  createCompanyOperationId,
  discardSettingsDraft,
  editSettingsDraft,
  pendingCompanyAction,
  rejectedCompanyAction,
  updateSettingsDraft,
  type CompanyActionState,
  type CompanyMutationReceipt,
  type SettingsDraftState,
} from "../../features/company/companyWorkspaceState";

function asBool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function CompanySettingsPanel({
  settings,
  busy = false,
  onSave,
  onDirtyChange,
  navigationRequest = null,
  onResolveNavigation,
}: {
  settings: Record<string, unknown>;
  busy?: boolean;
  onSave?: (
    settings: Record<string, unknown>,
    operationId: string,
  ) => Promise<CompanyMutationReceipt<{ settings: Record<string, unknown> }>>;
  onDirtyChange?: (dirty: boolean) => void;
  navigationRequest?: { label: string } | null;
  onResolveNavigation?: (decision: "saved" | "discarded" | "cancelled") => void;
}) {
  const [latestSettings, setLatestSettings] = useState(settings);
  const [draftState, setDraftState] = useState<SettingsDraftState>({
    baseline: settings,
    draft: settings,
    dirty: false,
    conflict: false,
  });
  const [saveState, setSaveState] = useState<CompanyActionState>({ phase: "idle" });

  useEffect(() => {
    setLatestSettings(settings);
    setDraftState((current) => updateSettingsDraft(current, settings));
  }, [settings]);

  useEffect(() => {
    onDirtyChange?.(draftState.dirty);
  }, [draftState.dirty, onDirtyChange]);

  const update = (key: string, value: unknown) => {
    setDraftState((current) => editSettingsDraft(current, key, value));
    setSaveState({ phase: "idle" });
  };
  const savePending = saveState.phase === "pending";

  const saveDraft = async (operationId = createCompanyOperationId("company-settings")): Promise<boolean> => {
    if (!onSave || savePending) return false;
    setSaveState(pendingCompanyAction(operationId));
    try {
      const receipt = await onSave(draftState.draft, operationId);
      if (receipt.phase !== "committed") {
        setSaveState({
          phase: "rejected",
          operationId,
          message: receipt.error ?? "Settings were not saved. Your draft was kept.",
          retryable: receipt.retryable ?? true,
          ambiguous: receipt.ambiguous,
        });
        return false;
      }
      const savedSettings = receipt.value?.settings ?? draftState.draft;
      setLatestSettings(savedSettings);
      setDraftState({ baseline: savedSettings, draft: savedSettings, dirty: false, conflict: false });
      setSaveState({ phase: "committed", operationId, message: "Settings saved", updatedAt: Date.now() });
      return true;
    } catch (error) {
      setSaveState(rejectedCompanyAction(operationId, error));
      return false;
    }
  };

  const discardDraft = () => {
    setDraftState((current) => discardSettingsDraft(current, latestSettings));
    setSaveState({ phase: "idle" });
  };

  return (
    <section className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Settings</h4>
        {onSave && (
          <button
            type="button"
            onClick={() => void saveDraft()}
            disabled={busy || savePending || !draftState.dirty}
            aria-busy={savePending}
            className="flex h-7 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-900 px-2 text-[11px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
          >
            <Save size={12} />
            {savePending ? "Saving…" : "Save"}
          </button>
        )}
      </div>

      <div className="space-y-2">
        <label className="flex items-center justify-between gap-3 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
          <span className="text-[11px] text-zinc-400">Queue tasks</span>
          <select
            value={String(draftState.draft.task_policy ?? "queued")}
            onChange={(event) => update("task_policy", event.target.value)}
            className="max-w-[120px] bg-transparent text-[11px] text-zinc-200 outline-none"
          >
            <option className="bg-zinc-900" value="queued">queued</option>
            <option className="bg-zinc-900" value="manual">manual</option>
          </select>
        </label>

        <label className="flex items-center justify-between gap-3 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
          <span className="text-[11px] text-zinc-400">Dispatch policy</span>
          <span className="max-w-[130px] truncate font-mono text-[10px] text-zinc-500">
            {String(draftState.draft.dispatch_policy ?? "local_queue_only")}
          </span>
        </label>

        {[
          ["normal_status_silent", "Quiet normal status"],
          ["mentions_create_tasks", "Mentions create tasks"],
          ["direct_tool_execution", "Direct tool execution"],
        ].map(([key, label]) => (
          <label key={key} className="flex items-center justify-between gap-3 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
            <span className="text-[11px] text-zinc-400">{label}</span>
            <input
              type="checkbox"
              checked={asBool(draftState.draft[key], key !== "direct_tool_execution")}
              onChange={(event) => update(key, event.target.checked)}
              className="h-4 w-4 accent-emerald-500"
            />
          </label>
        ))}
      </div>

      {draftState.conflict && (
        <div role="alert" className="space-y-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-100">
          <p>Settings changed elsewhere. Your unsaved draft was preserved.</p>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" disabled={savePending} onClick={() => void saveDraft()} className="rounded border border-amber-300/30 px-2 py-1">Save draft</button>
            <button type="button" disabled={savePending} onClick={discardDraft} className="rounded border border-zinc-600 px-2 py-1">Discard draft</button>
            <button type="button" disabled={savePending} onClick={() => setDraftState((current) => ({ ...current, conflict: false }))} className="rounded border border-zinc-600 px-2 py-1">Keep editing</button>
          </div>
        </div>
      )}

      {navigationRequest && (
        <div role="alertdialog" aria-label="Unsaved Company settings" className="space-y-2 rounded-md border border-sky-500/30 bg-sky-500/10 p-2 text-[11px] text-sky-100">
          <p>Save or discard your settings before switching to {navigationRequest.label}.</p>
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              disabled={savePending}
              onClick={() => void saveDraft().then((saved) => saved && onResolveNavigation?.("saved"))}
              className="rounded border border-sky-300/30 px-2 py-1"
            >
              Save &amp; switch
            </button>
            <button
              type="button"
              disabled={savePending}
              onClick={() => {
                discardDraft();
                onResolveNavigation?.("discarded");
              }}
              className="rounded border border-zinc-600 px-2 py-1"
            >
              Discard &amp; switch
            </button>
            <button type="button" disabled={savePending} onClick={() => onResolveNavigation?.("cancelled")} className="rounded border border-zinc-600 px-2 py-1">Cancel</button>
          </div>
        </div>
      )}

      {saveState.phase !== "idle" && (
        <p role={saveState.phase === "rejected" ? "alert" : "status"} aria-live="polite" className={saveState.phase === "rejected" ? "text-[11px] text-amber-200" : "text-[11px] text-emerald-300"}>
          {saveState.message}
          {saveState.phase === "rejected" && saveState.retryable && (
            <button type="button" className="ml-2 underline" onClick={() => void saveDraft(saveState.operationId)}>Retry</button>
          )}
        </p>
      )}
    </section>
  );
}
