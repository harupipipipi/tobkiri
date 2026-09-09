import type { ChatMessage, Conversation, SavedTurnResult } from "./api";

export function savedTurnSnapshotState(
  turn: SavedTurnResult["turn"],
  conversation: Pick<Conversation, "id" | "conversation_revision" | "messages"> | null,
  conversationId: string,
  turnId: string,
): "pending" | "current" | "changed" | "unavailable" {
  const reference = turn.result_reference;
  if (turn.id !== turnId || turn.conversation_id !== conversationId
    || turn.status !== "completed" || !reference
    || reference.conversation_id !== conversationId
    || !Number.isSafeInteger(reference.conversation_revision) || reference.conversation_revision < 1
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(reference.user_message_id ?? "")
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(reference.assistant_message_id ?? "")
    || !/^sha256:[a-f0-9]{64}$/.test(reference.outcome_digest ?? "")) return "pending";
  // null means a completed turn followed by an explicit unavailable conversation response.
  // It is not evidence that the saved operation failed, nor permission to resend it.
  if (conversation === null) return "unavailable";
  if (conversation.id !== conversationId || !Number.isSafeInteger(conversation.conversation_revision)
    || conversation.conversation_revision! < reference.conversation_revision) return "pending";
  if (conversation.conversation_revision! > reference.conversation_revision) return "changed";
  const user = conversation.messages.find((message) => message.id === reference.user_message_id);
  const assistant = conversation.messages.find((message) => message.id === reference.assistant_message_id);
  return user?.role === "user" && user.metadata?.turn_id === turnId
    && assistant?.role === "assistant" && assistant.metadata?.turn_id === turnId
    && !isAssistantMessageStillRunning(assistant) ? "current" : "pending";
}

export function savedTurnSnapshotNotice(state: ReturnType<typeof savedTurnSnapshotState>): string | null {
  if (state === "changed") return "送信の保存完了を確認しました。会話はその後更新されているため、現在の内容を表示しています。";
  if (state === "unavailable") return "送信の保存完了を確認しましたが、現在の会話は取得できません。自動再送はしません。";
  return null;
}

export type PendingChatRequest = {
  conversationId: string;
  operationId?: string;
  savedTurn?: boolean;
  requestFingerprint?: string;
  startedAt: number;
  status: string;
  toolNames: string[];
  toolStartedAt?: Record<string, number>;
  recoveredFromLocation?: boolean;
};

export const PENDING_CHAT_REQUEST_TTL_MS = 6 * 60 * 60_000;
export const PENDING_USER_ONLY_GRACE_MS = 8_000;

export function shouldForgetPendingAfterPollError(errorValue: unknown): boolean {
  const message = errorValue instanceof Error ? errorValue.message : String(errorValue ?? "");
  return /(?:^|\n)HTTP (?:404|410)\b/i.test(message)
    || /\b(?:NOT_FOUND|EXPIRED)\b/i.test(message);
}

export function isAssistantMessageStillRunning(message: ChatMessage | undefined): boolean {
  if (!message || message.role === "user") return false;
  const metadata = message.metadata && typeof message.metadata === "object" ? message.metadata : {};
  const thinking = metadata.thinking && typeof metadata.thinking === "object"
    ? metadata.thinking as Record<string, unknown>
    : {};
  const state = String(thinking.state ?? "").toLowerCase();
  const finishReason = String(message.finish_reason ?? "").toLowerCase();
  return state === "streaming" || state === "running" || finishReason === "streaming";
}

export function shouldClearPendingAfterConversationRefresh(
  latest: ChatMessage | undefined,
  request: PendingChatRequest | null | undefined,
  now = Date.now(),
): boolean {
  if (!latest || !request) return false;
  // Saved turns require the durable completion reference, not editable message metadata.
  if (request.savedTurn) return false;
  if (latest.role !== "user") return !isAssistantMessageStillRunning(latest);
  return now - request.startedAt >= PENDING_USER_ONLY_GRACE_MS;
}
