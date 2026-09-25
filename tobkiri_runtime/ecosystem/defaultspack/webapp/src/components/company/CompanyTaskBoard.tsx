import { ClipboardList, Plus, Search, Send, Trash2, X } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import {
  createCompanyOperationId,
  pendingCompanyAction,
  rejectedCompanyAction,
  type CompanyActionState,
  type CompanyMutationReceipt,
} from "../../features/company/companyWorkspaceState";
import type { CompanyAgent, CompanyRunLink, CompanyTask } from "../../lib/api";
import { CompanyRunConversation } from "./CompanyRunConversation";

const STATUSES = ["queued", "running", "waiting_approval", "blocked", "completed", "done"] as const;

export function CompanyTaskBoard({
  tasks,
  agents,
  runs = [],
  expectedTaskCount,
  busy = false,
  onCreateTask,
  onCreateResearchTask,
  onUpdateTask,
  onDeleteTask,
  onDispatchTask,
}: {
  tasks: CompanyTask[];
  agents: CompanyAgent[];
  runs?: CompanyRunLink[];
  expectedTaskCount?: number;
  busy?: boolean;
  onCreateTask?: (title: string, targetAgentIds: string[], operationId: string) => Promise<CompanyMutationReceipt<CompanyTask>>;
  onCreateResearchTask?: (query: string, targetAgentIds: string[], operationId: string) => Promise<CompanyMutationReceipt<CompanyTask>>;
  onUpdateTask?: (taskId: string, updates: Partial<CompanyTask>, operationId: string) => Promise<CompanyMutationReceipt<CompanyTask>>;
  onDeleteTask?: (taskId: string, operationId: string) => Promise<CompanyMutationReceipt<{ deleted: boolean; task_id: string }>>;
  onDispatchTask?: (taskId: string, operationId: string) => Promise<CompanyMutationReceipt<Record<string, unknown>>>;
}) {
  const [title, setTitle] = useState("");
  const [targetAgentId, setTargetAgentId] = useState("");
  const [deleteCandidateId, setDeleteCandidateId] = useState<string | null>(null);
  const [actionStates, setActionStates] = useState<Record<string, CompanyActionState>>({});
  const retries = useRef(new Map<string, {
    operationId: string;
    work: (operationId: string) => Promise<CompanyMutationReceipt<unknown>>;
    onCommitted?: () => void;
  }>());
  const grouped = useMemo(() => {
    const map = new Map<string, CompanyTask[]>();
    for (const task of tasks) {
      const status = task.status || "queued";
      map.set(status, [...(map.get(status) ?? []), task]);
    }
    return map;
  }, [tasks]);
  const latestRunByTaskId = useMemo(() => {
    const map = new Map<string, CompanyRunLink>();
    for (const run of runs) {
      const taskId = String(run.task_id || "");
      if (taskId && !map.has(taskId)) map.set(taskId, run);
    }
    return map;
  }, [runs]);
  const visibleStatuses = useMemo(
    () => [...STATUSES, ...[...grouped.keys()].filter((status) => !(STATUSES as readonly string[]).includes(status))],
    [grouped],
  );
  const displayedTaskCount = Math.max(tasks.length, expectedTaskCount ?? 0);
  const actionPending = (key: string) => actionStates[key]?.phase === "pending";

  const runTaskAction = async <T,>(
    key: string,
    work: (operationId: string) => Promise<CompanyMutationReceipt<T>>,
    operationId = createCompanyOperationId(key),
    onCommitted?: () => void,
  ): Promise<CompanyMutationReceipt<T>> => {
    if (actionPending(key)) {
      return { operationId, phase: "rejected", error: "This action is already pending.", retryable: false };
    }
    setActionStates((current) => ({ ...current, [key]: pendingCompanyAction(operationId) }));
    retries.current.set(key, {
      operationId,
      work: work as (value: string) => Promise<CompanyMutationReceipt<unknown>>,
      onCommitted,
    });
    try {
      const receipt = await work(operationId);
      if (receipt.phase === "committed") {
        setActionStates((current) => ({
          ...current,
          [key]: { phase: "committed", operationId, message: "Saved", updatedAt: Date.now() },
        }));
        onCommitted?.();
        retries.current.delete(key);
      } else {
        setActionStates((current) => ({
          ...current,
          [key]: {
            phase: "rejected",
            operationId,
            message: receipt.error ?? "The action was not saved.",
            retryable: receipt.retryable ?? true,
            ambiguous: receipt.ambiguous,
          },
        }));
      }
      return receipt;
    } catch (error) {
      const rejected = rejectedCompanyAction(operationId, error);
      setActionStates((current) => ({ ...current, [key]: rejected }));
      return {
        operationId,
        phase: "rejected",
        error: rejected.message,
        retryable: rejected.retryable,
        ambiguous: rejected.ambiguous,
      };
    }
  };

  const renderActionState = (key: string) => {
    const state = actionStates[key];
    if (!state || state.phase === "idle") return null;
    const retry = retries.current.get(key);
    return (
      <div
        role={state.phase === "rejected" ? "alert" : "status"}
        aria-live="polite"
        className={`mt-1 flex items-center justify-between gap-2 text-[10px] ${state.phase === "rejected" ? "text-amber-200" : "text-emerald-300"}`}
      >
        <span>{state.message}</span>
        {state.phase === "rejected" && state.retryable && retry && (
          <button type="button" className="underline" onClick={() => void runTaskAction(key, retry.work, retry.operationId, retry.onCommitted)}>Retry</button>
        )}
      </div>
    );
  };

  return (
    <section className="space-y-2 p-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Delegated Tasks</h4>
        <span className="text-[10px] text-zinc-600">{displayedTaskCount}</span>
      </div>

      {onCreateTask && (
        <form
          className={onCreateResearchTask ? "grid grid-cols-[minmax(0,1fr)_92px_28px_28px] gap-1.5" : "grid grid-cols-[minmax(0,1fr)_92px_28px] gap-1.5"}
          onSubmit={(event) => {
            event.preventDefault();
            const cleanTitle = title.trim();
            if (!cleanTitle) return;
            const targets = targetAgentId ? [targetAgentId] : [];
            void runTaskAction(
              "task:create",
              (operationId) => onCreateTask(cleanTitle, targets, operationId),
              undefined,
              () => setTitle((current) => current.trim() === cleanTitle ? "" : current),
            );
          }}
        >
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            disabled={busy || actionPending("task:create")}
            placeholder="Ask a Subagent"
            className="h-8 min-w-0 rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
          />
          <select
            value={targetAgentId}
            onChange={(event) => setTargetAgentId(event.target.value)}
            disabled={busy || actionPending("task:create")}
            className="h-8 rounded-md border border-zinc-800 bg-zinc-950 px-1.5 text-[11px] text-zinc-300 outline-none"
          >
            <option value="">Subagent</option>
            {agents.map((agent) => (
              <option key={agent.agent_id} value={agent.agent_id}>
                {agent.role_key || agent.agent_id}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={busy || actionPending("task:create") || !title.trim()}
            aria-busy={actionPending("task:create")}
            className="flex h-8 w-8 items-center justify-center rounded-md bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-30"
            title="Create task"
          >
            <Plus size={13} />
          </button>
          {onCreateResearchTask && (
            <button
              type="button"
              disabled={busy || actionPending("task:create") || !title.trim()}
              onClick={() => {
                const query = title.trim();
                if (!query) return;
                const targets = targetAgentId ? [targetAgentId] : [];
                void runTaskAction(
                  "task:create",
                  (operationId) => onCreateResearchTask(query, targets, operationId),
                  undefined,
                  () => setTitle((current) => current.trim() === query ? "" : current),
                );
              }}
              className="flex h-8 w-8 items-center justify-center rounded-md border border-sky-500/30 text-sky-300 hover:bg-sky-500/10 disabled:opacity-30"
              title="Deep research with DuckDuckGo"
            >
              <Search size={13} />
            </button>
          )}
        </form>
      )}
      {renderActionState("task:create")}

      <div className="space-y-2">
        {visibleStatuses.map((status) => {
          const items = grouped.get(status) ?? [];
          if (items.length === 0) return null;
          return (
            <div key={status} className="space-y-1">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-zinc-600">
                <ClipboardList size={10} />
                <span>{status}</span>
                <span>{items.length}</span>
              </div>
              {items.map((task) => {
                const latestRun = latestRunByTaskId.get(task.id);
                const latestRunMessage = latestRun?.agent_run?.result_preview || latestRun?.agent_run?.error;
                return (
                  <div key={task.id} className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-1.5">
                    <div className="flex items-start justify-between gap-2">
                      <p className="min-w-0 flex-1 text-[12px] font-medium leading-snug text-zinc-200">{task.title}</p>
                      <div className="flex flex-shrink-0 items-center gap-1">
                        {onDispatchTask && status === "queued" && (
                          <button
                            type="button"
                            onClick={() => void runTaskAction(
                              `task:${task.id}:dispatch`,
                              (operationId) => onDispatchTask(task.id, operationId),
                            )}
                            disabled={busy || actionPending(`task:${task.id}:dispatch`)}
                            aria-busy={actionPending(`task:${task.id}:dispatch`)}
                            className="flex h-6 w-6 items-center justify-center rounded border border-sky-500/30 text-sky-300 hover:bg-sky-500/10 disabled:opacity-40"
                            title="Dispatch task to agent"
                          >
                            <Send size={11} />
                          </button>
                        )}
                        {onUpdateTask && (
                          <select
                            value={status}
                            onChange={(event) => void runTaskAction(
                              `task:${task.id}:status`,
                              (operationId) => onUpdateTask(task.id, { status: event.target.value }, operationId),
                            )}
                            disabled={busy || actionPending(`task:${task.id}:status`)}
                            aria-label={`Move ${task.title} to status`}
                            className="h-6 max-w-28 rounded border border-zinc-800 bg-zinc-950 px-1 text-[10px] text-zinc-400 disabled:opacity-40"
                          >
                            {visibleStatuses.map((option) => (
                              <option key={option} value={option}>{option}</option>
                            ))}
                          </select>
                        )}
                        {onDeleteTask && deleteCandidateId !== task.id && (
                          <button
                            type="button"
                            onClick={() => setDeleteCandidateId(task.id)}
                            disabled={busy}
                            className="flex h-6 w-6 items-center justify-center rounded border border-red-500/20 text-red-300/70 hover:bg-red-500/10 hover:text-red-200 disabled:opacity-40"
                            title={`Delete ${task.title}`}
                            aria-label={`Delete ${task.title}`}
                          >
                            <Trash2 size={11} />
                          </button>
                        )}
                        {onDeleteTask && deleteCandidateId === task.id && (
                          <div className="flex items-center gap-1" role="group" aria-label={`Confirm deletion of ${task.title}`}>
                            <button
                              type="button"
                              onClick={() => {
                                void runTaskAction(
                                  `task:${task.id}:delete`,
                                  (operationId) => onDeleteTask(task.id, operationId),
                                  undefined,
                                  () => setDeleteCandidateId(null),
                                );
                              }}
                              disabled={busy || actionPending(`task:${task.id}:delete`)}
                              aria-busy={actionPending(`task:${task.id}:delete`)}
                              className="h-6 rounded border border-red-500/30 px-1.5 text-[10px] text-red-200 hover:bg-red-500/10 disabled:opacity-40"
                              title={`Confirm delete ${task.title}`}
                            >
                              Delete
                            </button>
                            <button
                              type="button"
                              onClick={() => setDeleteCandidateId(null)}
                              disabled={busy}
                              className="flex h-6 w-6 items-center justify-center rounded border border-zinc-800 text-zinc-500 hover:bg-zinc-800 disabled:opacity-40"
                              title="Cancel delete"
                              aria-label="Cancel delete"
                            >
                              <X size={11} />
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                    {task.target_agent_ids && task.target_agent_ids.length > 0 && (
                      <p className="mt-1 truncate text-[10px] text-zinc-500">{task.target_agent_ids.join(", ")}</p>
                    )}
                    {latestRun && (
                      <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-zinc-500">
                        <span className="truncate">{latestRun.agent_id}</span>
                        <span className="flex-shrink-0 rounded border border-zinc-800 px-1 py-0.5">{latestRun.agent_run?.status ?? latestRun.status}</span>
                      </div>
                    )}
                    {latestRun?.agent_run?.model && (
                      <p className="mt-1 truncate font-mono text-[10px] text-zinc-600">{latestRun.agent_run.model}</p>
                    )}
                    <CompanyRunConversation
                      messages={latestRun?.agent_run?.conversation}
                      fallback={latestRunMessage}
                      fallbackError={Boolean(latestRun?.agent_run?.error && !latestRun?.agent_run?.result_preview)}
                    />
                    {renderActionState(`task:${task.id}:dispatch`)}
                    {renderActionState(`task:${task.id}:status`)}
                    {renderActionState(`task:${task.id}:delete`)}
                  </div>
                );
              })}
            </div>
          );
        })}
        {tasks.length === 0 && (
          <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            {displayedTaskCount > 0
              ? `${displayedTaskCount} tasks recorded. Refreshing task details...`
              : "No delegated tasks."}
          </div>
        )}
      </div>
    </section>
  );
}
