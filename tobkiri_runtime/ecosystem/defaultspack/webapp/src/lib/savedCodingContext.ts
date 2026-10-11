/** Build only the canonical single-workspace saved context; Host owns authority. */
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
    throw new Error("保存付き coding には単一 workspace ID が必要です。");
  }
  const forbidden = ["group_id", "groupId", "workspace_root", "workspaceRoot",
    "rootPath", "rumi_data_path", "rumiDataPath", "rumi_dp_path",
    "workspace_bindings", "workspaceBindings", "workspaceId", "shared_read_only"];
  // The normal coding-workspace picker can carry its label/root for display.
  // Those hints are safe only when tied to the exact selected owner ID, and
  // are intentionally omitted from the returned Host-bound payload.
  if (input.pending && (input.pending.workspaceRoot != null
    || input.pending.workspaceLabel != null)
    && input.pending.workspaceId !== id) {
    throw new Error("表示用 workspace 情報と選択 workspace が一致しません。");
  }
  for (const context of [input.pending, input.conversation?.metadata]) {
    if (context && forbidden.some((key) => context[key] != null
      && !(context === input.pending && (
        (key === "workspaceId" && context[key] === id)
        || ((key === "workspaceRoot" || key === "workspaceLabel")
          && context.workspaceId === id)
      )))) {
      throw new Error("この保存付き coding の特殊 workspace context は未対応です。");
    }
  }
  const conversation = input.conversation;
  if (conversation && (conversation.group_id
    || ![null, undefined, "", "chat", "coding"].includes(conversation.conversation_kind)
    || conversation.metadata?.workspace_id !== id
    || (conversation.metadata?.mode != null && conversation.metadata.mode !== "coding"))) {
    throw new Error("保存済み会話の workspace context が一致しません。");
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
