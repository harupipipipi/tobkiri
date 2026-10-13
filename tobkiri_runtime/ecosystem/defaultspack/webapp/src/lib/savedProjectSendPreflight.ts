const standaloneWorkspaceGuidance = "新しい会話でProjectを「No project」にし、実行モードを「Coding」にして作業フォルダを1つ選んでください。モード選択が見えない場合は、設定の「詳細設定」で「実行モードを手動選択できるようにする」を有効にしてください。";

/** Explain unsupported Project context without changing membership or authority. */
export function assertSavedProjectSendSupported(input: {
  projectId?: unknown;
  pendingWorkspaceBindings?: unknown;
  conversationWorkspaceBindings?: unknown;
}): void {
  if (input.projectId !== undefined && input.projectId !== null && input.projectId !== "") {
    throw new Error(`Projectやグループに属する会話の保存付き送信は、作業フォルダが1つでも未対応です。${standaloneWorkspaceGuidance}`);
  }
  const bindings = [input.pendingWorkspaceBindings, input.conversationWorkspaceBindings]
    .filter((value) => value !== undefined && value !== null);
  if (!bindings.length) return;
  const multipleRoots = bindings.some((value) => Array.isArray(value) && value.length > 1);
  const reason = multipleRoots
    ? "複数の作業フォルダを持つ会話の保存付き送信にはまだ対応していません。"
    : "Project形式の作業フォルダ設定を持つ会話の保存付き送信にはまだ対応していません。";
  throw new Error(`${reason}${standaloneWorkspaceGuidance}`);
}
