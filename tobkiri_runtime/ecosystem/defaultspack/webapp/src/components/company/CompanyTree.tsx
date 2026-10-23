import { Bot, Plus, RefreshCw } from "lucide-react";

import type { CompanyRecord } from "../../lib/api";

export function CompanyTree({
  companies,
  activeCompanyId,
  activeTaskCount,
  busy = false,
  emptyMessage = "No Subagent Team loaded.",
  onSelect,
  onBootstrap,
  onRefresh,
}: {
  companies: CompanyRecord[];
  activeCompanyId?: string | null;
  activeTaskCount?: number;
  busy?: boolean;
  emptyMessage?: string;
  onSelect?: (companyId: string) => void;
  onBootstrap?: () => void;
  onRefresh?: () => void;
}) {
  return (
    <section className="border-b border-zinc-800/60 p-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Bot size={14} className="text-zinc-500" />
          <h3 className="truncate text-[12px] font-semibold text-zinc-200">Subagent Team</h3>
        </div>
        <div className="flex items-center gap-1">
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              disabled={busy}
              className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
              title="Refresh Subagents"
            >
              <RefreshCw size={12} />
            </button>
          )}
          {onBootstrap && (
            <button
              type="button"
              onClick={onBootstrap}
              disabled={busy}
              className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
              title="Create Subagent Team"
            >
              <Plus size={13} />
            </button>
          )}
        </div>
      </div>

      <div className="space-y-1">
        {companies.map((company) => {
          const active = activeCompanyId === company.id;
          const taskCount = active && typeof activeTaskCount === "number"
            ? activeTaskCount
            : company.task_count ?? Object.keys(company.tasks ?? {}).length;
          return (
            <button
              key={company.id}
              type="button"
              onClick={() => onSelect?.(company.id)}
              className={`w-full rounded-md border px-2 py-1.5 text-left transition-colors ${
                active
                  ? "border-emerald-500/30 bg-emerald-500/10"
                  : "border-zinc-800/70 bg-zinc-950/40 hover:border-zinc-700"
              }`}
            >
              <span className="block truncate text-[12px] font-medium text-zinc-200">{company.name || company.id}</span>
              <span
                className="mt-0.5 block truncate font-mono text-[9px] text-zinc-600"
                title={company.id}
              >
                ID: {company.id}
              </span>
              <span className="mt-0.5 flex items-center gap-2 text-[10px] text-zinc-500">
                <span>{company.agent_count ?? Object.keys(company.agents ?? {}).length} Agents</span>
                <span>{taskCount} tasks</span>
              </span>
            </button>
          );
        })}
        {companies.length === 0 && (
          <div className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            {busy ? "Loading Subagent Team..." : emptyMessage}
          </div>
        )}
      </div>
    </section>
  );
}
