/** Display facts read from the mounted workspace owner, never execution authority. */
export type SavedCodingWorkspace = {
  workspace_id: string;
  label: string;
  root_path: string;
  trusted: boolean;
  metadata: Record<string, unknown>;
  trust_granted_at?: string | number | null;
  last_used_at?: string | number | null;
};

const workspaceIdPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const absoluteRootPattern = /^(?:\/|[A-Za-z]:[\\/]|\\\\)/;
const invalidWorkspace = () => new Error("作業フォルダの読み込み結果が一致しません。選び直してください。");
const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
const validTimestamp = (value: unknown): value is string | number | null =>
  value === null || (typeof value === "string" && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value))
    || (typeof value === "number" && Number.isSafeInteger(value) && value >= 0);

/** Verify the exact requested mount and project only owner-provided display facts. */
export function savedCodingWorkspace(workspaceId: string, response: unknown): SavedCodingWorkspace {
  if (typeof workspaceId !== "string" || !workspaceIdPattern.test(workspaceId)) throw invalidWorkspace();
  const workspace = record(record(response)?.workspace);
  const metadata = record(workspace?.metadata);
  if (!workspace || workspace.workspace_id !== workspaceId
    || typeof workspace.label !== "string" || !workspace.label.trim()
    || typeof workspace.root_path !== "string" || !absoluteRootPattern.test(workspace.root_path)
    || /[\u0000-\u001f\u007f]/.test(workspace.root_path)
    || typeof workspace.trusted !== "boolean" || !metadata) throw invalidWorkspace();
  const result: SavedCodingWorkspace = {
    workspace_id: workspaceId,
    label: workspace.label,
    root_path: workspace.root_path,
    trusted: workspace.trusted,
    metadata: { ...metadata },
  };
  for (const key of ["trust_granted_at", "last_used_at"] as const) {
    if (!(key in workspace)) continue;
    const value = workspace[key];
    if (!validTimestamp(value)) throw invalidWorkspace();
    result[key] = value;
  }
  return result;
}

/** Read one explicit mount without a legacy context, git, trust or selection call. */
export async function loadSavedCodingWorkspace(
  workspaceId: string,
  readWorkspace: (workspaceId: string) => Promise<unknown>,
): Promise<SavedCodingWorkspace> {
  if (typeof workspaceId !== "string" || !workspaceIdPattern.test(workspaceId)) throw invalidWorkspace();
  return savedCodingWorkspace(workspaceId, await readWorkspace(workspaceId));
}
