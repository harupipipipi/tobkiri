import type { Conversation } from "../../lib/api";

export type ConversationActivity = "idle" | "running" | "waiting" | "failed" | "done";

export type ConversationPresentation = {
  conversationId: string;
  title: string;
  iconId: string | null;
  activity: ConversationActivity;
  unread: boolean;
  accessibleStatusLabel: string | null;
};

type PendingConversationRequest = {
  status?: string | null;
  startedAt?: number | null;
  updatedAt?: number | null;
};

export type ConversationPresentationOptions = {
  activeConversationId?: string | null;
  runningConversationId?: string | null;
  pendingRequests?: Readonly<Record<string, PendingConversationRequest | undefined>>;
  readAtByConversation?: Readonly<Record<string, number | undefined>>;
};

function metadataRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function normalizedStatus(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function activityFromStatus(value: unknown): ConversationActivity | null {
  const status = normalizedStatus(value);
  if (!status) return null;
  if (/failed|failure|error|blocked|cancelled|canceled|interrupted|失敗|中断/.test(status)) {
    return "failed";
  }
  if (/waiting|approval|input|required|confirm|queued|保留|承認|待/.test(status)) {
    return "waiting";
  }
  if (/running|streaming|working|thinking|executing|progress|思考|実行中/.test(status)) {
    return "running";
  }
  if (/completed|complete|done|succeeded|success|finished|完了|成功/.test(status)) {
    return "done";
  }
  if (status === "idle") return "idle";
  return null;
}

function metadataActivity(metadata: Record<string, unknown>): ConversationActivity | null {
  for (const key of ["activity", "activity_status", "execution_status", "run_status", "status"]) {
    const activity = activityFromStatus(metadata[key]);
    if (activity) return activity;
  }
  return null;
}

function statusLabel(activity: ConversationActivity, unread: boolean): string | null {
  const activityLabel = activity === "running"
    ? "Running"
    : activity === "waiting"
      ? "Waiting for approval or input"
      : activity === "failed"
        ? "Failed"
        : activity === "done"
          ? "Completed"
          : null;
  if (activityLabel && unread) return `${activityLabel}, unread`;
  if (activityLabel) return activityLabel;
  return unread ? "Unread update" : null;
}

/** Convert seconds or millisecond timestamps into comparable epoch milliseconds. */
export function conversationTimestamp(value: unknown): number {
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return 0;
  return numeric < 1_000_000_000_000 ? numeric * 1_000 : numeric;
}

/** Build the canonical display projection consumed by history and workspace tabs. */
export function conversationPresentation(
  conversation: Pick<Conversation, "id" | "title" | "updated_at" | "metadata">,
  options: ConversationPresentationOptions = {},
): ConversationPresentation {
  const metadata = metadataRecord(conversation.metadata);
  const pendingRequest = options.pendingRequests?.[conversation.id];
  const pendingStatus = pendingRequest?.status;
  const isActive = options.activeConversationId === conversation.id;
  const activity = options.runningConversationId === conversation.id
    ? "running"
    : activityFromStatus(pendingStatus)
      ?? metadataActivity(metadata)
      ?? "idle";
  const updatedAt = conversationTimestamp(conversation.updated_at);
  const activityUpdatedAt = conversationTimestamp(
    pendingRequest?.updatedAt ?? pendingRequest?.startedAt,
  );
  const readAt = conversationTimestamp(options.readAtByConversation?.[conversation.id]);
  const explicitUnread = metadata.unread === true || metadata.is_unread === true;
  const unread = !isActive && (
    (explicitUnread && readAt === 0)
    || (readAt > 0 && Math.max(updatedAt, activityUpdatedAt) > readAt)
  );
  const projectedActivity = activity === "idle" && unread ? "done" : activity;
  const iconId = typeof metadata.icon_id === "string" && metadata.icon_id.trim()
    ? metadata.icon_id.trim()
    : null;
  return {
    conversationId: conversation.id,
    title: conversation.title.trim() || "New Conversation",
    iconId,
    activity: projectedActivity,
    unread,
    accessibleStatusLabel: statusLabel(projectedActivity, unread),
  };
}

/** Build one ID-indexed projection map for every conversation surface. */
export function buildConversationPresentations(
  conversations: Array<Pick<Conversation, "id" | "title" | "updated_at" | "metadata">>,
  options: ConversationPresentationOptions = {},
): Record<string, ConversationPresentation> {
  return Object.fromEntries(conversations.map((conversation) => {
    const presentation = conversationPresentation(conversation, options);
    return [presentation.conversationId, presentation];
  }));
}

/** Safe fallback for a history item when its list projection is temporarily absent. */
export function fallbackConversationPresentation(input: {
  conversationId: string;
  title: string;
  metadata?: Record<string, unknown> | null;
}): ConversationPresentation {
  return conversationPresentation({
    id: input.conversationId,
    title: input.title,
    updated_at: 0,
    metadata: input.metadata,
  });
}
