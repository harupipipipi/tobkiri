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

export type SavedTurnProgressState =
  | "ledger_only"
  | "user_saved"
  | "all_messages_saved_unconfirmed"
  | "conversation_unavailable";

export function savedTurnProgressState(
  turn: SavedTurnResult["turn"],
  conversation: Pick<Conversation, "id" | "conversation_revision" | "messages"> | null,
  conversationId: string,
  turnId: string,
): SavedTurnProgressState {
  if (conversation === null) return "conversation_unavailable";
  if (turn.id !== turnId || turn.conversation_id !== conversationId
    || turn.status === "completed" || conversation.id !== conversationId
    || !Number.isSafeInteger(conversation.conversation_revision)
    || (conversation.conversation_revision ?? 0) < 1) return "ledger_only";
  const claim = [...(turn.events ?? [])].reverse().find(
    (event) => event.name === "turn.running"
      && event.details?.phase === "saved_execution_claimed",
  );
  const userId = claim?.details?.user_message_id;
  const assistantId = claim?.details?.assistant_message_id;
  if (typeof userId !== "string" || typeof assistantId !== "string"
    || !/^message:[a-f0-9]{64}$/.test(userId)
    || !/^message:[a-f0-9]{64}$/.test(assistantId)) return "ledger_only";
  const messages = conversation.messages.filter(
    (message) => message.metadata?.turn_id === turnId,
  );
  const userSaved = messages.some(
    (message) => message.id === userId && message.role === "user",
  );
  const assistantSaved = messages.some(
    (message) => message.id === assistantId && message.role === "assistant"
      && !isAssistantMessageStillRunning(message),
  );
  if (userSaved && assistantSaved) return "all_messages_saved_unconfirmed";
  return userSaved ? "user_saved" : "ledger_only";
}

export function savedTurnProgressNotice(state: SavedTurnProgressState): string {
  if (state === "user_saved") {
    return "ユーザーメッセージは保存済みです。assistant の保存状態を照合中です。自動再送はしません。";
  }
  if (state === "all_messages_saved_unconfirmed") {
    return "user／assistant メッセージは保存済みです。turn の完了状態を照合中です。自動再送はしません。";
  }
  if (state === "conversation_unavailable") {
    return "turn 台帳は未完了で、現在の会話を取得できません。自動再送せず照合を待ちます。";
  }
  return "turn 台帳は未完了です。保存状態を照合中のため自動再送はしません。";
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

export function updateSavedTurnNotice(
  current: Record<string, PendingChatRequest>, conversationId: string,
  turnId: string, status: string,
): Record<string, PendingChatRequest> {
  const entry = current[conversationId];
  // A delayed stop receipt belongs to the original turn, not the currently
  // visible conversation or a newer request. Never restore a completed entry.
  if (!entry?.savedTurn || entry.conversationId !== conversationId
    || entry.operationId !== turnId || entry.status === status) return current;
  return { ...current, [conversationId]: { ...entry, status } };
}

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
