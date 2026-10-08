/** Build inert canonical membership metadata; callers still require owner CAS. */
export function canonicalChatGroupUpdate(
  metadata: Record<string, unknown> | null | undefined,
  target: string | null,
): { group_id: string | null; metadata: Record<string, unknown> } {
  if (target !== null && (typeof target !== "string"
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(target))) {
    throw new Error("History group identity is invalid.");
  }
  if (metadata !== null && metadata !== undefined
    && (typeof metadata !== "object" || Array.isArray(metadata))) {
    throw new Error("History conversation metadata is invalid.");
  }
  const next = { ...(metadata ?? {}) };
  delete next.group_id;
  delete next.groupId;
  delete next.group_title;
  delete next.groupTitle;
  if (target !== null) next.group_id = target;
  return { group_id: target, metadata: next };
}

export type HistoryMembershipConversation = {
  id: string;
  conversation_revision?: number;
  group_id?: string | null;
  metadata?: Record<string, unknown> | null;
};

/** Commit an inert membership change under caller fences and conversation CAS.
 * Project lookup is not an atomic group-existence precondition. The backend must
 * revalidate references when using them; this helper grants no path authority.
 */
export async function commitHistoryMembershipMove<T extends HistoryMembershipConversation>(
  source: T,
  target: string | null,
  ports: {
    assertCurrent: () => void;
    projects: () => Promise<{ projects: { id: string; title: string }[] }>;
    update: (id: string, updates: ReturnType<typeof canonicalChatGroupUpdate>, revision: number) => Promise<T>;
  },
): Promise<T & { conversation_revision: number }> {
  if (!source || typeof source.id !== "string"
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(source.id)
    || typeof source.conversation_revision !== "number"
    || !Number.isSafeInteger(source.conversation_revision)
    || source.conversation_revision < 1
    || source.conversation_revision >= Number.MAX_SAFE_INTEGER) {
    throw new Error("Refresh the conversation before changing membership.");
  }
  const id = source.id;
  const revision = source.conversation_revision;
  const updates = canonicalChatGroupUpdate(source.metadata, target);
  ports.assertCurrent();
  if (target !== null) {
    const snapshot = await ports.projects();
    ports.assertCurrent();
    const project = snapshot.projects.find((item) => item.id === target);
    if (!project || typeof project.title !== "string" || !project.title.trim()) {
      throw new Error("Target Project is unavailable.");
    }
    updates.metadata.group_title = project.title;
  }
  ports.assertCurrent();
  const acknowledgement = await ports.update(id, updates, revision);
  ports.assertCurrent();
  const metadata = acknowledgement?.metadata;
  if (!acknowledgement || acknowledgement.id !== id
    || acknowledgement.group_id !== target
    || acknowledgement.conversation_revision !== revision + 1
    || !metadata || typeof metadata !== "object" || Array.isArray(metadata)
    || Object.prototype.hasOwnProperty.call(metadata, "groupId")
    || Object.prototype.hasOwnProperty.call(metadata, "groupTitle")
    || (target === null
      ? Object.prototype.hasOwnProperty.call(metadata, "group_id")
        || Object.prototype.hasOwnProperty.call(metadata, "group_title")
      : metadata.group_id !== target || metadata.group_title !== updates.metadata.group_title)) {
    throw new Error("Conversation membership acknowledgement is invalid.");
  }
  return acknowledgement as T & { conversation_revision: number };
}
