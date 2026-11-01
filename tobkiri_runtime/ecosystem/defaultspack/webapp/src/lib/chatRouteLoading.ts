export async function loadConversationForRefresh({
  preferredId,
  activeConversationId,
  locationChatId,
  listedConversations,
  loadConversation,
  isCurrentView = () => true,
}: {
  preferredId?: string | null;
  activeConversationId?: string | null;
  locationChatId?: string | null;
  listedConversations: Array<{ id: string }>;
  loadConversation: (conversationId: string | null) => Promise<void>;
  isCurrentView?: () => boolean;
}): Promise<void> {
  if (!isCurrentView()) return;
  const targetId = preferredId ?? locationChatId ?? activeConversationId ?? listedConversations[0]?.id ?? null;
  if (!targetId) {
    await loadConversation(null);
    return;
  }

  if (listedConversations.some((conversation) => conversation.id === targetId)) {
    await loadConversation(targetId);
    return;
  }

  try {
    await loadConversation(targetId);
  } catch {
    if (!isCurrentView()) return;
    await loadConversation(listedConversations[0]?.id ?? null);
  }
}

export function resolveSupersededConversationRedirect(
  conversation: { id?: string | null; metadata?: unknown } | null | undefined,
  requestedId: string | null | undefined,
): string | null {
  const metadata = conversation?.metadata;
  if (!metadata || typeof metadata !== "object") return null;
  const record = metadata as Record<string, unknown>;
  if (record.superseded !== true) return null;
  const activeId = String(record.active_conversation_id ?? record.replacement_conversation_id ?? "").trim();
  if (!activeId || activeId === requestedId || activeId === conversation?.id) return null;
  return activeId;
}
