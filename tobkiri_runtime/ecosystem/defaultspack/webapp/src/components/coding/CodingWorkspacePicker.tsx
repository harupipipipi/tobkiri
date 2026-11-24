import { ChevronDown, FolderPlus, RefreshCw, ShieldCheck, ShieldQuestion } from "lucide-react";

import type { CodingWorkspaceRecord } from "../../lib/api";

export function CodingWorkspacePicker({
  workspaces,
  selectedWorkspaceId,
  disabled = false,
  busy = false,
  onSelect,
  onTrust,
  onCreate,
  onRefresh,
}: {
  workspaces: CodingWorkspaceRecord[];
  selectedWorkspaceId?: string | null;
  disabled?: boolean;
  busy?: boolean;
  onSelect?: (workspaceId: string) => void;
  onTrust?: (workspaceId: string) => void;
  onCreate?: () => void;
  onRefresh?: () => void;
}) {
  const selected = selectedWorkspaceId
    ? workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId) ?? null
    : workspaces[0] ?? null;
  const interactionDisabled = disabled || busy;
  const TrustIcon = selected?.trusted ? ShieldCheck : ShieldQuestion;

  return (
    <div className="rumi-workspace-picker flex min-w-0 items-center gap-1.5" data-workspace-count={workspaces.length}>
      <div className="rumi-workspace-picker-main relative min-w-0 flex-1">
        {selected ? (
          <TrustIcon
            size={13}
            aria-hidden="true"
            className={`pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 ${selected.trusted ? "text-emerald-300" : "text-amber-300"}`}
          />
        ) : (
          <ShieldQuestion size={13} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
        )}
        <select
          value={selected?.workspace_id ?? ""}
          disabled={interactionDisabled || workspaces.length === 0}
          onChange={(event) => event.target.value && onSelect?.(event.target.value)}
          className="rumi-workspace-picker-select h-8 w-full appearance-none rounded-lg border border-zinc-700/70 bg-zinc-900/55 py-0 pl-8 pr-7 text-[12px] font-medium text-zinc-200 outline-none transition-colors hover:border-zinc-600 focus:border-zinc-500 disabled:cursor-not-allowed disabled:opacity-60"
          title={selected?.root_path ?? "Coding workspace"}
          aria-label="Coding workspace"
        >
          {workspaces.map((workspace) => (
            <option key={workspace.workspace_id} value={workspace.workspace_id} className="bg-zinc-900 text-zinc-100">
              {workspace.label || workspace.workspace_id}
            </option>
          ))}
          {!selected && <option value="">workspace を選択</option>}
        </select>
        <ChevronDown size={12} className="rumi-workspace-picker-chevron pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-500" aria-hidden="true" />
      </div>

      <div className="rumi-workspace-picker-actions flex shrink-0 items-center gap-1">
        {selected && !selected.trusted && onTrust && (
          <button
            type="button"
            disabled={interactionDisabled}
            onClick={() => onTrust(selected.workspace_id)}
            className="rumi-workspace-picker-action is-trust inline-flex h-8 w-8 items-center justify-center rounded-lg text-amber-300 transition-colors hover:bg-zinc-800 hover:text-amber-200 disabled:cursor-not-allowed disabled:opacity-50"
            title="workspace を信頼"
            aria-label={`${selected.label || selected.workspace_id} を信頼`}
          >
            <ShieldCheck size={13} />
          </button>
        )}
        {onRefresh && (
          <button
            type="button"
            disabled={interactionDisabled}
            onClick={onRefresh}
            className="rumi-workspace-picker-action inline-flex h-8 w-8 items-center justify-center rounded-lg text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-50"
            title="workspace 一覧を更新"
            aria-label="workspace 一覧を更新"
          >
            <RefreshCw size={13} className={busy ? "animate-spin" : ""} />
          </button>
        )}
        {onCreate && (
          <button
            type="button"
            disabled={interactionDisabled}
            onClick={onCreate}
            className="rumi-workspace-picker-action inline-flex h-8 w-8 items-center justify-center rounded-lg text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-50"
            title="現在のフォルダを workspace に追加"
            aria-label="workspace を追加"
          >
            <FolderPlus size={13} />
          </button>
        )}
      </div>
    </div>
  );
}
