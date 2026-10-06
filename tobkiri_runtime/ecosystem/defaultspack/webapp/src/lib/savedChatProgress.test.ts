import test from "node:test";
import assert from "node:assert/strict";
import type { Conversation, SavedTurnEventSnapshot } from "./api";
import { mergeSavedChatProgress, savedChatProgressMessage, savedChatProgressWitness } from "./savedChatProgress";
import type { PendingChatRequest } from "./pendingChat";
import { parseThreadProgressPage, type ThreadProgressPage } from "../host/threadProgressContract";

const now = 5_000;
const userId = `message:${"a".repeat(64)}`;
const a = "a".repeat(64);
const b = "b".repeat(64);
const requestId = "saved-turn.durable";
const turn = { id: "turn-1", conversation_id: "chat-1", request_id: requestId, input_digest: `sha256:${a}`,
  conversation_revision: 3, status: "running", revision: 2 };
const snapshot = { turn, turn_id: turn.id, conversation_id: turn.conversation_id, request_id: requestId } as SavedTurnEventSnapshot;
const pending: PendingChatRequest = { conversationId: "chat-1", operationId: "turn-1", ownerTurnObserved: true,
  savedTurn: true, startedAt: 1, status: "pending", toolNames: [] };
const conversation: Conversation = { id: "chat-1", title: "Test", created_at: 1, updated_at: 1, model: "stub/default",
  tags: [], is_starred: false, is_archived: false, conversation_revision: 4, current_node_id: userId, messages: [
  { id: userId, conversation_id: "chat-1", role: "user", created_at: 1, content: "1+2", metadata: { turn_id: "turn-1" } },
] };
const witness = savedChatProgressWitness(snapshot, conversation, pending)!;
const page = (events: ThreadProgressPage["events"], cursor: number, complete = false): ThreadProgressPage => ({
  version: "tobkiri.turn-progress.v1", progress_id: a, provisional: true,
  binding: { turn_id: "turn-1", conversation_id: "chat-1", request_id: requestId, parent_id: userId,
    conversation_revision: 4, input_digest: `sha256:${a}`, ai_input_digest: `sha256:${a}` },
  events, cursor, provider_complete: complete, expires_at_ms: now + 120_000, canonical_turn_status: "running",
});
const start = { cursor: 1, event: { type: "tool_started" as const, tool_id: "calculator", tool_call_id: "call-1", arguments: { expression: "1+2" } } };
const end = { cursor: 2, event: { type: "tool_completed" as const, tool_id: "calculator", tool_call_id: "call-1",
  status: "success" as const, content: JSON.stringify({ status: "success", result: 3, error: null }) } };

test("live display witness requires exact owner read and canonical current user branch", () => {
  assert.equal(witness.requestId, requestId);
  for (const invalid of [null, { ...snapshot, turn: { ...turn, input_digest: "invalid" } },
    { ...snapshot, request_id: "different" }, { ...snapshot, turn: { ...turn, status: "completed" } }]) {
    assert.equal(savedChatProgressWitness(invalid, conversation, pending), null);
  }
  assert.equal(savedChatProgressWitness(snapshot, { ...conversation, current_node_id: "old-node" }, pending), null);
  assert.equal(savedChatProgressWitness(snapshot, conversation, { ...pending, ownerTurnObserved: false }), null);
});

test("authenticated tool start and result produce visible activity without canonical completion", () => {
  const first = mergeSavedChatProgress(null, page([start], 1), witness, now)!;
  assert.equal(first.activity[0].type, "tool_call_started");
  const completed = mergeSavedChatProgress(first, page([end], 2, true), witness, now)!;
  assert.equal(completed.activity[1].type, "tool_call_completed");
  assert.deepEqual(completed.activity[1].result, { status: "success", result: 3, error: null });
  assert.equal((completed as unknown as Record<string, unknown>).result_reference, undefined);
  assert.equal(savedChatProgressMessage(completed, witness, conversation)?.finish_reason, "streaming");
  const canonical = { ...conversation, messages: [...conversation.messages,
    { id: "assistant", conversation_id: "chat-1", created_at: 1, role: "assistant" as const, content: "3", metadata: { turn_id: "turn-1" } }] };
  assert.equal(savedChatProgressMessage(completed, witness, canonical), null);
});

test("new authenticated stages reset text/cursor, retain logs, and reject old stage ABA", () => {
  const tool = mergeSavedChatProgress(null, page([start, end], 2, true), witness, now)!;
  const next: ThreadProgressPage = { ...page([{ cursor: 1, event: { type: "text_delta", delta: "Result is 3" } }], 1),
    progress_id: b, binding: { ...page([], 0).binding, ai_input_digest: `sha256:${b}` } };
  const ai = mergeSavedChatProgress(tool, next, witness, now)!;
  assert.equal(ai.stage.cursor, 1);
  assert.equal(ai.stage.text, "Result is 3");
  assert.equal(ai.activity.length, 2);
  assert.equal(mergeSavedChatProgress(ai, page([], 0), witness, now), null);
});

test("forged owners, replay/skips, unmatched result and status disagreement fail closed", () => {
  const first = mergeSavedChatProgress(null, page([start], 1), witness, now)!;
  assert.equal(mergeSavedChatProgress(first, page([start], 1), witness, now), null);
  assert.equal(mergeSavedChatProgress(null, page([{ ...end, cursor: 1 }], 1, true), witness, now), null);
  assert.equal(mergeSavedChatProgress(first, { ...page([end], 2, true), binding: { ...page([], 0).binding, request_id: "foreign" } }, witness, now), null);
  assert.equal(parseThreadProgressPage(page([{ ...end, event: { ...end.event, status: "error" } }], 2, true), now), null);
  assert.equal(mergeSavedChatProgress(first, page([{ ...end, cursor: 3 }], 3, true), witness, now), null);
});
