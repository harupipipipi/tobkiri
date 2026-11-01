import type { ProjectDirectorySelection, ProjectDirectorySelectionSet } from "./projectWorkspaceMount";

export const MAX_PROJECT_FOLDERS = 32;

/** Private Host tickets and their local draft freshness, never filesystem paths. */
export type ProjectFolderDraft = ProjectDirectorySelectionSet & {
  acquiredAtById: Record<string, number>;
  consumedIds: string[];
};

/** Start a standalone project draft without any directory authority. */
export function emptyProjectFolderDraft(): ProjectFolderDraft {
  return { selections: [], primary_selection_id: "", acquiredAtById: {}, consumedIds: [] };
}

/** Normalize the legacy single choice and native multi-choice response. */
export function normalizeProjectFolderSelections(
  choice: ProjectDirectorySelection | ProjectDirectorySelectionSet,
): ProjectDirectorySelectionSet {
  const set = "selections" in choice
    ? choice
    : { selections: [choice], primary_selection_id: choice.selection_id };
  if (!Array.isArray(set.selections) || set.selections.length < 1 || set.selections.length > MAX_PROJECT_FOLDERS) {
    throw new Error(`Choose between 1 and ${MAX_PROJECT_FOLDERS} folders.`);
  }
  const seen = new Set<string>();
  for (const item of set.selections) {
    if (!item || typeof item.selection_id !== "string" || !item.selection_id.trim()
      || typeof item.display_name !== "string" || !item.display_name.trim()
      || !Number.isFinite(item.expires_in_ms) || item.expires_in_ms <= 0
      || seen.has(item.selection_id)) {
      throw new Error("Folder selection returned invalid or duplicate tickets. Choose folders again.");
    }
    seen.add(item.selection_id);
  }
  if (!seen.has(set.primary_selection_id)) throw new Error("The primary folder must be one of the selected folders.");
  return {
    selections: set.selections.map((selection) => ({ ...selection })),
    primary_selection_id: set.primary_selection_id,
  };
}

/** Append choices by opaque ticket; equal names do not imply equal folders. */
export function appendProjectFolderSelections(
  draft: ProjectFolderDraft,
  choice: ProjectDirectorySelection | ProjectDirectorySelectionSet,
  now = Date.now(),
): ProjectFolderDraft {
  const incoming = normalizeProjectFolderSelections(choice);
  const selections = [...draft.selections];
  const acquiredAtById = { ...draft.acquiredAtById };
  const seen = new Set(selections.map((selection) => selection.selection_id));
  for (const selection of incoming.selections) {
    if (draft.consumedIds.includes(selection.selection_id) && !seen.has(selection.selection_id)) {
      throw new Error("Folder selection was already submitted. Choose folders again to obtain fresh selections.");
    }
    if (seen.has(selection.selection_id)) continue;
    seen.add(selection.selection_id);
    selections.push(selection);
    acquiredAtById[selection.selection_id] = now;
  }
  if (selections.length > MAX_PROJECT_FOLDERS) throw new Error(`A project can link at most ${MAX_PROJECT_FOLDERS} folders.`);
  return {
    selections,
    primary_selection_id: draft.primary_selection_id || incoming.primary_selection_id,
    acquiredAtById,
    consumedIds: [...draft.consumedIds],
  };
}

/** Removing the primary chooses the first remaining folder deterministically. */
export function removeProjectFolderSelection(draft: ProjectFolderDraft, id: string): ProjectFolderDraft {
  const selections = draft.selections.filter((selection) => selection.selection_id !== id);
  const acquiredAtById = { ...draft.acquiredAtById };
  delete acquiredAtById[id];
  return {
    selections,
    primary_selection_id: draft.primary_selection_id === id ? selections[0]?.selection_id ?? "" : draft.primary_selection_id,
    acquiredAtById,
    // Keep consumed ticket tombstones even when a folder is removed.
    consumedIds: [...draft.consumedIds],
  };
}

/** Select the scalar compatibility binding from the existing folder list. */
export function setPrimaryProjectFolderSelection(draft: ProjectFolderDraft, id: string): ProjectFolderDraft {
  if (!draft.selections.some((selection) => selection.selection_id === id)) {
    throw new Error("The primary folder must be one of the selected folders.");
  }
  return { ...draft, primary_selection_id: id };
}

/** Produce one fresh selection set before preparing one immutable Host effect. */
export function projectFolderSelectionSet(draft: ProjectFolderDraft, now = Date.now()): ProjectDirectorySelectionSet {
  const set = normalizeProjectFolderSelections(draft);
  if (set.selections.some((selection) => draft.consumedIds.includes(selection.selection_id))) {
    throw new Error("Folder selection was already submitted. Remove those folders and choose them again; your project name is preserved.");
  }
  if (set.selections.some((selection) => !Number.isFinite(draft.acquiredAtById[selection.selection_id])
    || now - draft.acquiredAtById[selection.selection_id] >= selection.expires_in_ms)) {
    throw new Error("Folder selection expired. Remove those folders and choose them again; your project name is preserved.");
  }
  return set;
}

/** A retry must recover its existing mount result, never prepare these tickets again. */
export function consumeProjectFolderSelections(draft: ProjectFolderDraft): ProjectFolderDraft {
  return { ...draft, consumedIds: [...new Set([...draft.consumedIds, ...draft.selections.map((selection) => selection.selection_id)])] };
}

/** Distinguish same-name folders without exposing canonical paths or tickets. */
export function projectFolderLabels(draft: ProjectFolderDraft): string[] {
  const counts = new Map<string, number>();
  for (const selection of draft.selections) counts.set(selection.display_name, (counts.get(selection.display_name) ?? 0) + 1);
  const occurrences = new Map<string, number>();
  return draft.selections.map((selection) => {
    const name = selection.display_name;
    const index = (occurrences.get(name) ?? 0) + 1;
    occurrences.set(name, index);
    return (counts.get(name) ?? 0) > 1 ? `${name} (${index})` : name;
  });
}
