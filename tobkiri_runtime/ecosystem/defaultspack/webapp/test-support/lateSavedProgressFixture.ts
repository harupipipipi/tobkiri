import type { Conversation, SavedTurn, SavedTurnEventSnapshot } from "../src/lib/api";
import type { ThreadProgressPage } from "../src/host/threadProgressContract";

export const lateSavedProgressText = {
  draft: "Keep the saved answer when tool progress arrives late.",
  provisional: "Provisional response before the canonical save.",
  answer: "Structured response accepted.",
};

export const lateSavedProgressConversation: Conversation = {
  id: "c-smoke", title: "Late saved progress", conversation_revision: 1,
  created_at: 1_780_000_000_000, updated_at: 1_780_000_000_000, model: "stub/default",
  tags: [], metadata: {}, is_starred: false, is_archived: false, messages: [],
};

/** One owner, two contiguous provisional pages, and a separate canonical receipt. */
export function lateSavedProgressFixture(turnId: string, now: number) {
  const base = lateSavedProgressConversation;
  const userId = `message:${"a".repeat(64)}`;
  const assistantId = `message:${"b".repeat(64)}`;
  const requestId = "saved-turn.late-progress-fixture";
  const stageId = "c".repeat(64);
  const runningTurn: SavedTurn = {
    id: turnId, conversation_id: base.id, request_id: requestId,
    input_digest: `sha256:${"d".repeat(64)}`, conversation_revision: 1, status: "running", revision: 2,
  };
  const completedTurn: SavedTurn = { ...runningTurn, status: "completed", revision: 3,
    result_reference: { conversation_id: base.id, conversation_revision: 3,
      user_message_id: userId, assistant_message_id: assistantId, outcome_digest: `sha256:${"e".repeat(64)}` } };
  const runningConversation: Conversation = { ...base, conversation_revision: 2, current_node_id: userId,
    messages: [{ id: userId, conversation_id: base.id, role: "user", created_at: now,
      content: [{ type: "text", text: lateSavedProgressText.draft }], metadata: { turn_id: turnId } }] };
  const completedConversation: Conversation = { ...runningConversation, conversation_revision: 3,
    current_node_id: assistantId, messages: [...runningConversation.messages,
      { id: assistantId, conversation_id: base.id, role: "assistant", created_at: now + 1,
        parent_id: userId, finish_reason: "stop", content: [{ type: "text", text: lateSavedProgressText.answer }],
        metadata: { turn_id: turnId }, events: [], tool_logs: [] }] };
  const snapshot = (turn: SavedTurn): SavedTurnEventSnapshot => {
    const identity = { turn_id: turnId, operation_id: turnId, conversation_id: base.id,
      request_id: requestId, turn_revision: turn.revision, status: turn.status };
    return { ...identity, turn, events: [], terminal: turn.status === "completed"
      ? { ...identity, result_reference: turn.result_reference } : null };
  };
  const initialPage: ThreadProgressPage = {
    version: "tobkiri.turn-progress.v1", progress_id: stageId, provisional: true,
    binding: { turn_id: turnId, conversation_id: base.id, parent_id: userId, request_id: requestId,
      conversation_revision: 2, input_digest: runningTurn.input_digest!, ai_input_digest: `sha256:${stageId}` },
    events: [{ cursor: 1, event: { type: "text_delta", delta: lateSavedProgressText.provisional } }],
    cursor: 1, provider_complete: false, expires_at_ms: now + 110_000, canonical_turn_status: "running",
  };
  // This is valid active-owner progress captured before completion, not a
  // malformed page or a terminal status that would independently discard it.
  const latePage: ThreadProgressPage = { ...initialPage, cursor: 2,
    events: [{ cursor: 2, event: { type: "tool_started", tool_id: "web_search",
      tool_call_id: "call-late", arguments: { query: "late saved progress" } } }] };
  return { runningTurn, completedTurn, runningConversation, completedConversation,
    runningSnapshot: snapshot(runningTurn), completedSnapshot: snapshot(completedTurn), initialPage, latePage };
}
