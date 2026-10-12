import { FolderGit2, FolderPlus, RefreshCw } from "lucide-react";
import { useState } from "react";

import type { CodingWorkspaceRecord } from "../../lib/api";
import { ErrorNotice } from "../ErrorNotice";

/** Preserve mount failures for the visible Coding surface without replaying them. */
export async function addCodingWorkspace(
  create: () => Promise<CodingWorkspaceRecord | null>,
): Promise<{ workspace: CodingWorkspaceRecord | null; error: string | null }> {
  try {
    return { workspace: await create(), error: null };
  } catch (error) {
    return {
      workspace: null,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

/** Show the finite workspace operations offered by the captured v4 surface. */
export function CodingWorkspaceCockpit({
  workspaces,
  selectedWorkspaceId,
  onWorkspaceSelect,
  onWorkspaceCreate,
  onWorkspacesRefresh,
}: {
  workspaces: CodingWorkspaceRecord[];
  selectedWorkspaceId?: string | null;
  onWorkspaceSelect?: (workspaceId: string) => void;
  onWorkspaceCreate?: () => Promise<CodingWorkspaceRecord | null>;
  onWorkspacesRefresh?: () => void;
}) {
  const selectedWorkspace = workspaces.find(
    (workspace) => workspace.workspace_id === selectedWorkspaceId,
  ) ?? null;
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const handleAdd = async () => {
    if (!onWorkspaceCreate || adding) return;
    setAdding(true);
    setError(null);
    setNotice("Choose a folder, then review any approval in Tobkiri Launcher.");
    const result = await addCodingWorkspace(onWorkspaceCreate);
    setAdding(false);
    setError(result.error);
    setNotice(result.error ? null : result.workspace
      ? `Added ${result.workspace.label || result.workspace.workspace_id}.`
      : "Folder selection cancelled.");
  };

  return (
    <section
      className="flex min-h-0 w-full flex-1 flex-col overflow-y-auto rounded-lg border border-zinc-800 bg-[#0b0b0f] p-4"
      aria-label="Coding Cockpit"
      aria-busy={adding}
    >
      <div className="mb-4 flex items-center justify-between gap-2">
        <h1 className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
          <FolderGit2 size={16} />Coding Cockpit
        </h1>
        <button
          type="button"
          disabled={adding || !onWorkspacesRefresh}
          onClick={onWorkspacesRefresh}
          aria-label="Refresh workspaces"
          className="rounded-md p-2 text-zinc-400 hover:bg-zinc-800 disabled:opacity-40"
        >
          <RefreshCw size={14} />
        </button>
      </div>
      <label className="mb-2 text-xs text-zinc-400" htmlFor="coding-cockpit-workspace">
        Workspace
      </label>
      <select
        id="coding-cockpit-workspace"
        value={selectedWorkspace?.workspace_id ?? ""}
        disabled={adding || !onWorkspaceSelect || workspaces.length === 0}
        onChange={(event) => event.target.value && onWorkspaceSelect?.(event.target.value)}
        className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-300"
      >
        {!selectedWorkspace && <option value="">Select a workspace</option>}
        {workspaces.map((workspace) => (
          <option key={workspace.workspace_id} value={workspace.workspace_id}>
            {workspace.label || workspace.workspace_id}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={adding || !onWorkspaceCreate}
        onClick={() => void handleAdd()}
        className="mt-2 flex h-9 items-center justify-center gap-2 rounded-md border border-zinc-800 text-xs text-zinc-300 hover:bg-zinc-900 disabled:opacity-40"
        aria-label="Add workspace"
      >
        <FolderPlus size={14} />{adding ? "Adding workspace…" : "Add workspace"}
      </button>
      {error && (
        <ErrorNotice className="mt-3 px-2 py-2 text-xs" message={error} copyLabel="Copy workspace error" />
      )}
      {notice && (
        <p role="status" className="mt-3 text-xs text-zinc-400">{notice}</p>
      )}
      {selectedWorkspace ? (
        <div className="mt-4 rounded-md border border-zinc-800 p-3 text-xs text-zinc-400">
          <p className="font-medium text-zinc-200">{selectedWorkspace.label || selectedWorkspace.workspace_id}</p>
          <p className="mt-2 break-all font-mono">{selectedWorkspace.root_path}</p>
          <p className="mt-2">Workspace identity: {selectedWorkspace.workspace_id}</p>
          <p className="mt-2">Selecting this workspace does not grant trust or approve actions.</p>
        </div>
      ) : (
        <p role="status" className="mt-4 text-xs text-zinc-400">
          {selectedWorkspaceId
            ? "The selected workspace is unavailable. Refresh or select a workspace."
            : "Select or add a workspace to use for coding."}
        </p>
      )}
      <p className="mt-4 rounded-md border border-zinc-800 p-3 text-xs leading-5 text-zinc-500">
        This Profile provides workspace selection and folder mounting.
        Git status, file observations, reviews, branch changes, checkpoints,
        terminal, browser, MCP, and agent sessions are unavailable in this Coding view.
        No checks have been run here.
      </p>
    </section>
  );
}
