/** Keep the existing scalar primary; all Project folder hints remain inert. */
export function savedCodingContext(input: {
  coding: boolean;
  workspaceId: string | null;
  pending: Record<string, unknown> | null;
  conversation: {
    conversation_kind?: string | null;
    group_id?: string | null;
    metadata?: Record<string, unknown> | null;
  } | null;
}): { mode: "coding"; workspace_id: string } | null {
  if (!input.coding) return null;
  const id = input.workspaceId;
  if (!id || !/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,255}$/.test(id)) {
    throw new Error("コード作業を送信するには、作業フォルダを1つ選んでください。");
  }
  const contexts = [input.pending, input.conversation?.metadata];
  const memberships = [input.conversation?.group_id, ...contexts.flatMap((context) => [context?.group_id, context?.groupId])]
    .filter((value) => value != null && value !== "");
  if (memberships.some((value) => typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value))
    || new Set(memberships).size > 1) throw new Error("Projectの所属情報が一致しません。会話を開き直してください。");
  const project = memberships.length > 0;
  const forbidden = ["group_id", "groupId", "workspace_root", "workspaceRoot",
    "rootPath", "rumi_data_path", "rumiDataPath", "rumi_dp_path",
    "workspace_bindings", "workspaceBindings", "workspaceId", "shared_read_only"];
  // The normal coding-workspace picker can carry its label/root for display.
  // Those hints are safe only when tied to the exact selected owner ID, and
  // are intentionally omitted from the returned Host-bound payload.
  if (input.pending && (input.pending.workspaceRoot != null
    || input.pending.workspaceLabel != null)
    && input.pending.workspaceId !== id) {
    throw new Error("選択した作業フォルダの情報が一致しません。選び直してください。");
  }
  for (const context of [input.pending, input.conversation?.metadata]) {
    if (context && forbidden.some((key) => context[key] != null
      && !(context === input.conversation?.metadata && context.workspace_id === id
        && ["workspace_root", "workspaceRoot", "rootPath", "rumi_data_path", "rumiDataPath",
          "rumi_dp_path", "workspace_bindings", "workspaceBindings"].includes(key))
      && !(project && ["group_id", "groupId", "workspace_root", "workspaceRoot", "rootPath",
        "rumi_data_path", "rumiDataPath", "rumi_dp_path", "workspace_bindings", "workspaceBindings"].includes(key))
      && !(project && key === "workspaceId" && context[key] === id)
      && !(context === input.pending && (
        (key === "workspaceId" && context[key] === id)
        || ((key === "workspaceRoot" || key === "workspaceLabel")
          && context.workspaceId === id)
      )))) {
      throw new Error("この会話の作業フォルダ設定には対応していません。作業フォルダを選び直してください。");
    }
  }
  const conversation = input.conversation;
  if (conversation && (![null, undefined, "", "chat", "coding"].includes(conversation.conversation_kind)
    || (project ? conversation.metadata?.workspace_id ?? conversation.metadata?.workspaceId
      : conversation.metadata?.workspace_id) !== id
    || (conversation.conversation_kind === "coding" && conversation.metadata?.mode !== "coding")
    || (conversation.metadata?.mode === "coding" && conversation.conversation_kind !== "coding")
    || (conversation.metadata?.mode != null && conversation.metadata.mode !== "coding"))) {
    throw new Error("会話の作業フォルダが変わりました。選び直してから送信してください。");
  }
  return { mode: "coding", workspace_id: id };
}

type SavedCodingConversation = {
  conversation_kind?: string | null;
  group_id?: string | null;
  metadata?: Record<string, unknown> | null;
};

/** Recheck coding ownership after pending conversation writes settle. */
export async function savedCodingContextAfterMutation<T extends SavedCodingConversation>(input: {
  coding: boolean;
  workspaceId: string | null;
  pending: Record<string, unknown> | null;
  waitForMutation: () => Promise<void>;
  refetchConversation: () => Promise<T>;
}): Promise<{ conversation: T; metadata: { mode: "coding"; workspace_id: string } | null }> {
  await input.waitForMutation();
  const conversation = await input.refetchConversation();
  const metadata = savedCodingContext({
    coding: input.coding,
    workspaceId: input.workspaceId,
    pending: input.pending,
    conversation,
  });
  return { conversation, metadata };
}
