import { ConversationMutationBarrier } from "./conversationMutationBarrier";

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

type MembershipOperation = {
  key: string;
  target: string | null;
  state: "pending" | "acknowledged" | "failed";
  error?: unknown;
};

/** The existing barrier plus exact operation identity, scoped to membership.
 * A rejected intent is cleared only by a fresh canonical read; draining its
 * Promise alone cannot silently authorize a send with the old membership.
 */
export class HistoryMembershipMutations {
  private barrier = new ConversationMutationBarrier();
  private latest = new Map<string, MembershipOperation>();

  record(storeId: string, conversationId: string, target: string | null, write: Promise<unknown>): MembershipOperation {
    const key = `${storeId}\u0000${conversationId}`;
    const operation: MembershipOperation = { key, target, state: "pending" };
    this.latest.set(key, operation);
    const settled = write.then(() => {
      operation.state = "acknowledged";
    }, (error: unknown) => {
      operation.state = "failed";
      operation.error = error;
      throw error;
    });
    this.barrier.record(storeId, conversationId, settled);
    return operation;
  }

  isCurrent(operation: MembershipOperation): boolean {
    return this.latest.get(operation.key) === operation;
  }

  async settleAndRead<T extends HistoryMembershipConversation>(
    storeId: string, conversationId: string, read: () => Promise<T>,
  ): Promise<{ conversation: T; failure?: unknown }> {
    const key = `${storeId}\u0000${conversationId}`;
    for (;;) {
      try { await this.barrier.wait(storeId, conversationId); } catch { /* Reconcile the latest exact operation below. */ }
      const operation = this.latest.get(key);
      const conversation = await read();
      if (conversation.id !== conversationId) throw new Error("Conversation owner returned another identity.");
      if (this.latest.get(key) !== operation || operation?.state === "pending") continue;
      if (operation?.state === "failed") {
        this.latest.delete(key);
        return { conversation, failure: operation.error ?? new Error("Project update failed.") };
      }
      if (operation && conversation.group_id !== operation.target) {
        operation.state = "failed";
        operation.error = new Error("Project membership changed before sending.");
        // This attempt stops. The verified canonical record reconciles the UI.
        this.latest.delete(key);
        return { conversation, failure: operation.error };
      }
      if (operation) this.latest.delete(key);
      return { conversation };
    }
  }
}

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
