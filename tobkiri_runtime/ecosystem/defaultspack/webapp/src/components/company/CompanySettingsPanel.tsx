import { Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

function asBool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function CompanySettingsPanel({
  settings,
  busy = false,
  onSave,
}: {
  settings: Record<string, unknown>;
  busy?: boolean;
  onSave?: (settings: Record<string, unknown>) => void | Promise<void>;
}) {
  const [draft, setDraft] = useState(settings);
  const [savedSettings, setSavedSettings] = useState(settings);
  const [saveStatus, setSaveStatus] = useState("");
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    setDraft(settings);
    setSavedSettings(settings);
    setSaveStatus("");
    setSaveError("");
  }, [settings]);

  const update = (key: string, value: unknown) => {
    setSaveStatus("");
    setSaveError("");
    setDraft((current) => ({ ...current, [key]: value }));
  };
  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(savedSettings),
    [draft, savedSettings],
  );

  return (
    <section aria-labelledby="company-settings-title" aria-describedby="company-settings-help" aria-busy={busy} className="space-y-2 p-2">
      <form
        className="space-y-2"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!onSave || !dirty || busy) return;
          setSaveStatus("Saving Company settings");
          setSaveError("");
          try {
            await onSave(draft);
            setSavedSettings(draft);
            setSaveStatus("Company settings saved");
          } catch (error) {
            setSaveStatus("");
            setSaveError(error instanceof Error ? error.message : "Company settings failed to save");
          }
        }}
      >
        <div className="flex items-center justify-between gap-2">
          <h4 id="company-settings-title" className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Settings</h4>
          {onSave && (
            <button
              type="submit"
              disabled={busy || !dirty}
              aria-label={dirty ? "Save Company settings changes" : "Company settings have no unsaved changes"}
              className="flex min-h-11 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-900 px-3 text-[11px] text-zinc-300 hover:bg-zinc-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-300 disabled:opacity-40"
            >
              <Save size={12} />
              Save
            </button>
          )}
        </div>
        <p id="company-settings-help" className="sr-only">Settings apply to the active Company. Save becomes available when a setting changes.</p>

        <div className="space-y-2">
          <div className="flex min-h-11 items-center justify-between gap-3 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
            <label htmlFor="company-task-policy" className="text-[11px] text-zinc-400">Queue tasks</label>
            <select
              id="company-task-policy"
              value={String(draft.task_policy ?? "queued")}
              onChange={(event) => update("task_policy", event.target.value)}
              disabled={busy}
              aria-describedby="company-task-policy-help"
              className="max-w-[120px] bg-transparent text-[11px] text-zinc-200 outline-none"
            >
              <option className="bg-zinc-900" value="queued">queued</option>
              <option className="bg-zinc-900" value="manual">manual</option>
            </select>
            <span id="company-task-policy-help" className="sr-only">Choose whether new Company tasks are queued automatically or require manual handling.</span>
          </div>

          <div className="flex min-h-11 items-center justify-between gap-3 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
            <span id="company-dispatch-policy-label" className="text-[11px] text-zinc-400">Dispatch policy</span>
            <span role="status" aria-labelledby="company-dispatch-policy-label" className="max-w-[130px] truncate font-mono text-[10px] text-zinc-500">
              {String(draft.dispatch_policy ?? "local_queue_only")}
            </span>
          </div>

          {[
            ["normal_status_silent", "Quiet normal status"],
            ["mentions_create_tasks", "Mentions create tasks"],
            ["direct_tool_execution", "Direct tool execution"],
          ].map(([key, label]) => (
            <div key={key} className="flex min-h-11 items-center justify-between gap-3 rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
              <label htmlFor={`company-setting-${key}`} className="text-[11px] text-zinc-400">{label}</label>
              <input
                id={`company-setting-${key}`}
                type="checkbox"
                checked={asBool(draft[key], key !== "direct_tool_execution")}
                onChange={(event) => update(key, event.target.checked)}
                disabled={busy}
                className="h-4 w-4 accent-emerald-500"
              />
            </div>
          ))}
        </div>
        <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
          {busy ? "Company settings are busy" : saveStatus || (dirty ? "Company settings have unsaved changes" : "Company settings are saved")}
        </div>
        {saveError ? <p role="alert" className="text-[11px] text-red-300">{saveError}</p> : null}
      </form>
    </section>
  );
}
