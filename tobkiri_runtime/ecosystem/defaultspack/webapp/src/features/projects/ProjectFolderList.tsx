import { FolderOpen, X } from "lucide-react";
import { projectFolderLabels, type ProjectFolderDraft } from "../../lib/projectFolderDraft";

type ProjectFolderListProps = {
  draft: ProjectFolderDraft;
  disabled?: boolean;
  onRemove: (selectionId: string) => void;
  onPrimaryChange: (selectionId: string) => void;
};

/** Review draft folders and their primary binding before Host approval. */
export function ProjectFolderList({ draft, disabled = false, onRemove, onPrimaryChange }: ProjectFolderListProps) {
  const labels = projectFolderLabels(draft);
  if (!draft.selections.length) return null;
  return (
    <fieldset className="min-w-0 space-y-1 rounded-xl border border-zinc-800 p-2" disabled={disabled}>
      <legend className="px-1 text-[10px] text-zinc-400">Linked folders ({draft.selections.length})</legend>
      <p className="text-[10px] text-zinc-500">The primary folder is used by tools that accept one workspace.</p>
      <ul className="max-h-48 space-y-1 overflow-y-auto" aria-label="Selected project folders">
        {draft.selections.map((selection, index) => {
          const label = labels[index];
          const primary = selection.selection_id === draft.primary_selection_id;
          const consumed = draft.consumedIds.includes(selection.selection_id);
          return (
            <li key={selection.selection_id} className="flex min-h-11 min-w-0 items-center gap-1 rounded-lg bg-black/20 px-1">
              <FolderOpen size={13} className="shrink-0 text-zinc-400" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs text-zinc-200" title={label}>{label}</span>
                {consumed && <span className="block text-[10px] text-zinc-500">Submitted for approval</span>}
              </span>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onPrimaryChange(selection.selection_id)}
                aria-pressed={primary}
                aria-label={`Use ${label} as primary folder`}
                className="min-h-11 shrink-0 rounded-lg px-1 text-[10px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
              >{primary ? "Primary" : "Set primary"}</button>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onRemove(selection.selection_id)}
                aria-label={`Remove ${label}`}
                className="flex h-11 w-8 shrink-0 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50"
              ><X size={13} aria-hidden="true" /></button>
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}
