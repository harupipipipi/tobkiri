import { Bot, RefreshCw, Square } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { CodexAppServerRuntimeStatus } from "../../lib/api";
import { codingResources } from "../../features/coding/resources/codingResources";
import { ErrorNotice } from "../ErrorNotice";

function effortsFor(status: CodexAppServerRuntimeStatus | null, modelId: string): string[] {
  const model = status?.models.find((item) => item.id === modelId);
  return (model?.supported_reasoning_efforts ?? []).map((item) => (
    typeof item === "string" ? item : String(item.reasoningEffort ?? item.effort ?? item.value ?? "")
  )).filter(Boolean);
}

function eventText(event: CodexAppServerRuntimeStatus["events"][number]): string {
  const params = event.params ?? {};
  const message = params.message ?? params.text ?? params.delta;
  if (typeof message === "string") return message;
  return JSON.stringify(params);
}

export function CodexAppServerPanel({
  workspaceId,
  trusted,
}: {
  workspaceId: string | null;
  trusted: boolean;
}) {
  const [runtime, setRuntime] = useState<CodexAppServerRuntimeStatus | null>(null);
  const [message, setMessage] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!workspaceId || !trusted) {
      setRuntime(null);
      return;
    }
    try {
      const next = await codingResources.getCodexAppServerRuntimeStatus(workspaceId);
      setRuntime(next);
      setError("");
      setModel((current) => current || next.models.find((item) => item.is_default)?.id || next.models[0]?.id || "");
    } catch (errorValue) {
      setRuntime(null);
      setError(errorValue instanceof Error ? errorValue.message : String(errorValue));
    }
  }, [trusted, workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!runtime?.active_turn_id) return;
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, [refresh, runtime?.active_turn_id]);

  const efforts = useMemo(() => effortsFor(runtime, model), [model, runtime]);
  useEffect(() => {
    const selected = runtime?.models.find((item) => item.id === model);
    setEffort((current) => efforts.includes(current) ? current : selected?.default_reasoning_effort ?? efforts[0] ?? "");
  }, [efforts, model, runtime?.models]);

  const start = async () => {
    if (!workspaceId || !message.trim()) return;
    setBusy(true);
    setError("");
    try {
      const next = await codingResources.startCodexAppServerTurn({
        workspace_id: workspaceId,
        message: message.trim(),
        model: model || undefined,
        effort: effort || undefined,
      });
      setRuntime(next);
      setMessage("");
    } catch (errorValue) {
      setError(errorValue instanceof Error ? errorValue.message : String(errorValue));
    } finally {
      setBusy(false);
    }
  };

  const interrupt = async () => {
    if (!workspaceId) return;
    setBusy(true);
    try {
      setRuntime(await codingResources.interruptCodexAppServerTurn(workspaceId));
      setError("");
    } catch (errorValue) {
      setError(errorValue instanceof Error ? errorValue.message : String(errorValue));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="border-b border-zinc-800/60 p-3" aria-label="Codex App Server">
      <div className="mb-2 flex items-center gap-2">
        <Bot size={14} className="text-cyan-300" />
        <h2 className="truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">Codex App Server</h2>
        <button type="button" onClick={() => void refresh()} disabled={!workspaceId || !trusted || busy} className="ml-auto text-zinc-500 hover:text-zinc-100 disabled:opacity-40" title="Refresh Codex App Server">
          <RefreshCw size={13} />
        </button>
      </div>
      {!workspaceId ? <p className="text-[11px] text-zinc-600">Select a workspace to start.</p> : null}
      {workspaceId && !trusted ? <p className="text-[11px] text-amber-300">Trust this workspace before starting Codex.</p> : null}
      {error ? <ErrorNotice className="mb-2 px-2 py-1 text-[11px]" copyLabel="Copy Codex App Server error" message={error} /> : null}
      {runtime ? (
        <>
          <div className="mb-2 grid grid-cols-2 gap-1.5">
            <select aria-label="Codex model" value={model} onChange={(event) => setModel(event.target.value)} disabled={busy || Boolean(runtime.active_turn_id)} className="h-8 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[11px] text-zinc-300">
              {runtime.models.map((item) => <option key={item.id} value={item.id}>{item.display_name || item.id}</option>)}
            </select>
            <select aria-label="Reasoning effort" value={effort} onChange={(event) => setEffort(event.target.value)} disabled={busy || Boolean(runtime.active_turn_id) || efforts.length === 0} className="h-8 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[11px] text-zinc-300">
              {efforts.length === 0 ? <option value="">Default effort</option> : efforts.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <textarea aria-label="Codex task" value={message} onChange={(event) => setMessage(event.target.value)} disabled={busy || Boolean(runtime.active_turn_id)} placeholder="Ask Codex to work in this workspace" className="min-h-20 w-full resize-y rounded-md border border-zinc-800 bg-zinc-950/40 px-2 py-1.5 text-[11px] text-zinc-300 outline-none" />
          <div className="mt-1.5 flex gap-1.5">
            <button type="button" onClick={() => void start()} disabled={busy || !message.trim() || Boolean(runtime.active_turn_id)} className="h-7 flex-1 rounded-md bg-zinc-100 text-[11px] font-semibold text-zinc-950 disabled:bg-zinc-800 disabled:text-zinc-600">Start turn</button>
            <button type="button" onClick={() => void interrupt()} disabled={busy || !runtime.active_turn_id} className="flex h-7 items-center gap-1 rounded-md border border-zinc-700 px-2 text-[11px] text-zinc-300 disabled:opacity-40"><Square size={10} />Stop</button>
          </div>
          <p className="mt-1.5 truncate font-mono text-[10px] text-zinc-600">thread {runtime.thread_id || "starting"}</p>
          <div aria-live="polite" className="mt-2 max-h-48 space-y-1 overflow-y-auto rounded-md border border-zinc-800 bg-black/30 p-2">
            {runtime.events.slice(-30).map((event, index) => (
              <div key={`${event.sequence ?? index}-${event.method ?? "event"}`} className="text-[10px] leading-4 text-zinc-400">
                <span className="mr-1 font-mono text-zinc-600">{event.method || "event"}</span>{eventText(event)}
              </div>
            ))}
            {runtime.events.length === 0 ? <p className="text-[10px] text-zinc-600">No turn events yet.</p> : null}
          </div>
        </>
      ) : null}
    </section>
  );
}
