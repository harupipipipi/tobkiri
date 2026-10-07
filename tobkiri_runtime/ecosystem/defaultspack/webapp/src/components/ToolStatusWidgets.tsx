import { AlertTriangle, CheckCircle2, EyeOff, ShieldAlert, Wrench } from "lucide-react";

import type { SidebarItem } from "../lib/api";
import {
  summarizeToolManager,
  toolFilterReasonDetail,
  toolFilterReasonLabel,
  toolFilterStatusLabel,
  type ToolFilterEntry,
} from "../lib/toolStatus";

function countCard(label: string, value: number, tone: string) {
  return (
    <div style={{ gridTemplateColumns: "minmax(0,1fr) auto" }} className="grid min-w-0 items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/45 px-2.5 py-2">
      <p className="min-w-0 text-xs leading-4 text-zinc-500">{label}</p>
      <p className={`shrink-0 whitespace-nowrap text-right text-lg font-semibold leading-6 tabular-nums ${tone}`}>{value}</p>
    </div>
  );
}

export function ToolManagerWidget({
  tools,
  disabledToolIds,
  hiddenToolIds,
  filterEntries,
}: {
  tools: SidebarItem[];
  disabledToolIds: string[];
  hiddenToolIds: string[];
  filterEntries: ToolFilterEntry[];
}) {
  const summary = summarizeToolManager(tools, { disabledToolIds, hiddenToolIds, filterEntries });
  const blockedEntries = filterEntries.filter((entry) => entry.status === "blocked" || entry.status === "rejected");
  return (
    <div className="space-y-3">
      <div data-testid="tool-manager-status-counts" className="grid grid-cols-2 gap-2 rumi-stagger-tight">
        {countCard("許可中", summary.onCount, "text-emerald-300")}
        {countCard("権限で無効", summary.offByUserCount, "text-zinc-300")}
        {countCard("実行不可", summary.blockedCount, "text-amber-300")}
        {countCard("承認が必要", summary.needsApprovalCount, "text-sky-300")}
        {countCard("設定が必要", summary.missingSetupCount, "text-rose-300")}
      </div>
      {summary.hiddenCount > 0 && (
        <div className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2 text-xs text-zinc-400">
          <EyeOff size={13} />
          <span>一覧から隠す {summary.hiddenCount}</span>
        </div>
      )}
      {blockedEntries.length > 0 && (
        <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
          <div className="mb-2 flex items-center gap-2 text-xs font-medium text-amber-200">
            <AlertTriangle size={13} />
            <span>このターンで利用不可</span>
          </div>
          <div className="space-y-2">
            {blockedEntries.slice(0, 4).map((entry) => (
              <div key={`${entry.tool_name}:${entry.reason_code ?? entry.status}`} className="rounded-lg border border-zinc-800 bg-zinc-950/55 px-3 py-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-zinc-100">{entry.tool_name}</p>
                    <p className="mt-0.5 text-[11px] text-amber-200">{toolFilterReasonLabel(entry.reason_code)}</p>
                  </div>
                  <Wrench size={13} className="mt-0.5 flex-shrink-0 text-zinc-500" />
                </div>
                <p className="mt-1 text-[11px] leading-5 text-zinc-400">{toolFilterReasonDetail(entry)}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function ToolFilterLogWidget({ entries }: { entries: ToolFilterEntry[] }) {
  if (entries.length === 0) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-3 text-xs text-zinc-500">
        このターンの機能選定ログはまだありません。
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {entries.map((entry) => {
        const blocked = entry.status === "blocked" || entry.status === "rejected";
        const approval = entry.status === "approval_required";
        const hidden = entry.status === "hidden";
        return (
          <div key={`${entry.tool_name}:${entry.reason_code ?? entry.status}`} className="rounded-lg border border-zinc-800 bg-zinc-950/45 px-3 py-2">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm text-zinc-100">{entry.tool_name}</p>
                <p className="mt-0.5 text-[11px] text-zinc-500">
                  {toolFilterStatusLabel(entry)}
                </p>
              </div>
              {blocked ? (
                <AlertTriangle size={13} className="mt-0.5 flex-shrink-0 text-amber-300" />
              ) : approval ? (
                <ShieldAlert size={13} className="mt-0.5 flex-shrink-0 text-sky-300" />
              ) : hidden ? (
                <EyeOff size={13} className="mt-0.5 flex-shrink-0 text-zinc-400" />
              ) : (
                <CheckCircle2 size={13} className="mt-0.5 flex-shrink-0 text-emerald-300" />
              )}
            </div>
            {(blocked || approval || hidden) && (
              <p className="mt-1 text-[11px] leading-5 text-zinc-400">{toolFilterReasonDetail(entry)}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
