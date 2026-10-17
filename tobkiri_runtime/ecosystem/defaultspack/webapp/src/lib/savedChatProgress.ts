import type { ChatActivityEvent, ChatMessage, Conversation, SavedTurnEventSnapshot } from "./api";
import type { PendingChatRequest } from "./pendingChat";
import { mergeThreadProgress, type ThreadProgressState, type ThreadProgressWitness } from "../host/threadProgressState";
import { parseThreadProgressPage, progressDigest, progressId } from "../host/threadProgressContract";
import { parseFileEditReceiptFromToolResult } from "./fileEditTimeline";

export type SavedChatProgressWitness = ThreadProgressWitness & { requestId: string };
export type SavedChatProgressState = {
  stage: ThreadProgressState;
  stages: string[];
  activity: ChatActivityEvent[];
  started: Record<string, string>;
};

/** A current canonical user branch and exact owner event read establish display identity. */
export function savedChatProgressWitness(
  snapshot: SavedTurnEventSnapshot | null | undefined,
  conversation: Conversation | null,
  pending: PendingChatRequest | null | undefined,
): SavedChatProgressWitness | null {
  const turn = snapshot?.turn;
  if (!snapshot || !turn || !conversation || !pending?.savedTurn || pending.ownerTurnObserved !== true
    || pending.operationId !== turn.id || pending.conversationId !== turn.conversation_id
    || snapshot.turn_id !== turn.id || snapshot.conversation_id !== conversation.id
    || conversation.id !== turn.conversation_id || !["queued", "running", "waiting"].includes(turn.status)
    || turn.request_id !== snapshot.request_id || !progressId(turn.request_id)
    || !progressDigest(turn.input_digest) || !Number.isSafeInteger(turn.conversation_revision)
    || conversation.conversation_revision !== Number(turn.conversation_revision) + 1) return null;
  const user = conversation.messages.find((message) => message.id === conversation.current_node_id);
  if (!user || user.role !== "user" || user.conversation_id !== conversation.id
    || !/^message:[a-f0-9]{64}$/.test(user.id) || user.metadata?.turn_id !== turn.id) return null;
  return { turnId: turn.id, conversationId: conversation.id, parentId: user.id,
    conversationRevision: conversation.conversation_revision!, inputDigest: turn.input_digest,
    requestId: turn.request_id };
}

/** Provisional tool events never authorize control, prove completion, or write history. */
export function mergeSavedChatProgress(
  previous: SavedChatProgressState | null, value: unknown, witness: SavedChatProgressWitness,
  now = Date.now(),
): SavedChatProgressState | null {
  const page = parseThreadProgressPage(value, now);
  if (!page?.progress_id || page.binding.request_id !== witness.requestId) return null;
  const changedStage = previous && previous.stage.progressId !== page.progress_id;
  if (changedStage && previous.stages.includes(page.progress_id)) return null;
  const stage = mergeThreadProgress(previous?.stage ?? null, page, witness, now);
  if (!stage) return null;
  const activity = [...previous?.activity ?? []];
  const started = { ...previous?.started ?? {} };
  for (const { cursor, event } of page.events) {
    if (event.type !== "tool_started" && event.type !== "tool_completed") continue;
    const key = `${page.progress_id}:${event.tool_call_id}`;
    if (event.type === "tool_started") {
      if (started[key]) return null;
      started[key] = event.tool_id;
      activity.push({ type: "tool_call_started", phase: "tool_call_started", tool_name: event.tool_id,
        tool_call_id: event.tool_call_id, arguments: event.arguments, seq: cursor,
        provider_attempt_generation: page.progress_id });
    } else {
      if (started[key] !== event.tool_id) return null;
      delete started[key];
      const result: unknown = JSON.parse(event.content);
      const receipt = event.status === "success"
        ? parseFileEditReceiptFromToolResult(event.tool_id, result) : null;
      activity.push({ type: "tool_call_completed", phase: "tool_call_completed", tool_name: event.tool_id,
        tool_call_id: event.tool_call_id, result, is_error: event.status === "error",
        ...(receipt ? { file_edit_receipt: receipt } : {}),
        seq: cursor, provider_attempt_generation: page.progress_id });
    }
  }
  return { stage, activity, started,
    stages: changedStage || !previous ? [...previous?.stages ?? [], page.progress_id] : previous.stages };
}

/** Canonical assistant persistence replaces this transient display message. */
export function savedChatProgressMessage(
  progress: SavedChatProgressState | null, witness: SavedChatProgressWitness | null,
  conversation: Conversation | null,
): ChatMessage | null {
  if (!progress || !witness || !conversation
    || conversation.messages.some((message) => message.role === "assistant" && message.metadata?.turn_id === witness.turnId)
    || !progress.stage.text && !progress.activity.length) return null;
  return { id: `live-progress:${witness.turnId}`, conversation_id: witness.conversationId,
    role: "assistant", content: [{ type: "text", text: progress.stage.text }], events: progress.activity,
    created_at: Date.now(), finish_reason: "streaming",
    metadata: { thinking: { state: "streaming" }, provisional: true } };
}
