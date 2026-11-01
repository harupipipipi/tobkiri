/** Check inert Project context shape; the Host resolves actual owner facts. */
export function assertSavedProjectSendSupported(input: {
  projectId?: unknown;
  pendingWorkspaceBindings?: unknown;
  conversationWorkspaceBindings?: unknown;
}): void {
  const project = input.projectId;
  if (project !== undefined && project !== null && project !== ""
    && (typeof project !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(project))) {
    throw new Error("Projectの所属情報が無効です。会話を開き直してください。");
  }
  const bindings = [input.pendingWorkspaceBindings, input.conversationWorkspaceBindings]
    .filter((value) => value !== undefined && value !== null);
  if (!bindings.length) return;
  for (const value of bindings) {
    if (!Array.isArray(value) || !value.length || value.length > 32) {
      throw new Error("Projectの作業フォルダ設定が無効です。");
    }
    const ids = new Set<string>();
    for (const item of value) {
      if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Projectの作業フォルダ設定が無効です。");
      const record = item as Record<string, unknown>;
      const fields = "workspace_id" in record
        ? ["workspace_id", "workspace_label", "workspace_root"]
        : ["workspaceId", "workspaceLabel", "workspaceRoot"];
      const [id, label, root] = fields.map((key) => record[key]);
      if (Object.keys(record).length !== 3 || typeof id !== "string"
        || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(id) || ids.has(id)
        || typeof label !== "string" || !label.trim()
        || typeof root !== "string" || !root.trim()) throw new Error("Projectの作業フォルダ設定が無効です。");
      ids.add(id);
    }
  }
}
