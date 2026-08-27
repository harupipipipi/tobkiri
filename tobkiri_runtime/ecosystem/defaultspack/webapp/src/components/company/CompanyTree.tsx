import { Bot, Plus, RefreshCw } from "lucide-react";
import { useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";

import type { CompanyRecord } from "../../lib/api";
import { nextCompositeIndex } from "./companyAccessibility";

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
  const companyRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const moveCompanyFocus = (event: ReactKeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    const nextIndex = nextCompositeIndex(currentIndex, companies.length, event.key);
    if (nextIndex === null) return;
    event.preventDefault();
    const company = companies[nextIndex];
    companyRefs.current[nextIndex]?.focus();
    onSelect?.(company.id);
  };

  return (
    <section aria-labelledby="company-tree-title" className="border-b border-zinc-800/60 p-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Bot size={14} className="text-zinc-500" />
          <h3 id="company-tree-title" className="truncate text-[12px] font-semibold text-zinc-200">Subagent Team</h3>
        </div>
        <div className="flex items-center gap-1">
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              disabled={busy}
              aria-label="Refresh Subagent Team companies"
              className="flex h-11 w-11 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-300 disabled:opacity-40"
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
              aria-label="Create Subagent Team for the active conversation"
              className="flex h-11 w-11 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-300 disabled:opacity-40"
              title="Create Subagent Team"
            >
              <Plus size={13} />
            </button>
          )}
        </div>
      </div>

      <div role="tree" aria-label="Companies" aria-busy={busy} className="space-y-1">
        {companies.map((company, index) => {
          const active = activeCompanyId === company.id;
          const taskCount = active && typeof activeTaskCount === "number"
            ? activeTaskCount
            : company.task_count ?? Object.keys(company.tasks ?? {}).length;
          return (
            <button
              key={company.id}
              ref={(element) => { companyRefs.current[index] = element; }}
              type="button"
              role="treeitem"
              aria-selected={active}
              aria-current={active ? "true" : undefined}
              tabIndex={active || (!activeCompanyId && index === 0) ? 0 : -1}
              onClick={() => onSelect?.(company.id)}
              onKeyDown={(event) => moveCompanyFocus(event, index)}
              className={`min-h-11 w-full rounded-md border px-2 py-1.5 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-emerald-300 ${
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
          <div role="status" className="rounded-md border border-zinc-800/70 bg-zinc-950/40 px-2 py-2 text-[11px] text-zinc-500">
            {busy ? "Loading Subagent Team..." : emptyMessage}
          </div>
        )}
      </div>
    </section>
  );
}
