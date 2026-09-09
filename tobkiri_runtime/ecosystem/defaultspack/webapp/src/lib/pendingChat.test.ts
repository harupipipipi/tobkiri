import test from "node:test";
import assert from "node:assert/strict";

import type { ChatMessage, SavedTurnResult } from "./api";
import {
  PENDING_USER_ONLY_GRACE_MS,
  savedTurnSnapshotState,
  savedTurnSnapshotNotice,
  isAssistantMessageStillRunning,
  shouldClearPendingAfterConversationRefresh,
  shouldForgetPendingAfterPollError,
  type PendingChatRequest,
} from "./pendingChat";

function message(patch: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: [],
    created_at: 1000,
    conversation_id: "c1",
    metadata: null,
    events: [],
    tool_logs: [],
    ...patch,
  };
}

function pending(startedAt: number): PendingChatRequest {
  return {
    conversationId: "c1",
    startedAt,
    status: "Processing...",
    toolNames: [],
  };
}

test("assistant streaming metadata keeps pending active", () => {
  assert.equal(isAssistantMessageStillRunning(message({
    finish_reason: "streaming",
    metadata: { thinking: { state: "running" } },
  })), true);
});

test("completed assistant clears pending", () => {
  const latest = message({
    finish_reason: "stop",
    metadata: { thinking: { state: "completed" } },
  });

  assert.equal(shouldClearPendingAfterConversationRefresh(latest, pending(1000), 2000), true);
});

test("saved turns require their own assistant acknowledgement, never elapsed grace", () => {
  const request = { ...pending(1000), savedTurn: true, operationId: "turn-1" };
  const late = 1000 + 24 * 60 * 60_000;
  for (const patch of [
    { role: "user", metadata: { turn_id: "turn-1" } },
    { metadata: { turn_id: "older-turn" } },
    { conversation_id: "other", metadata: { turn_id: "turn-1" } },
    { metadata: { turn_id: "turn-1" }, finish_reason: "streaming" },
    { metadata: null },
  ]) {
    assert.equal(shouldClearPendingAfterConversationRefresh(message(patch), request, late), false);
  }
  assert.equal(shouldClearPendingAfterConversationRefresh(message({
    metadata: { turn_id: "turn-1" }, finish_reason: "stop",
  }), request, late), false);
});

test("saved completion distinguishes current, changed, unavailable and unverified snapshots", () => {
  const turn: SavedTurnResult["turn"] = {
    id: "turn-1", conversation_id: "c1", status: "completed", revision: 3,
    result_reference: {
      conversation_id: "c1", conversation_revision: 3, user_message_id: "user-1",
      assistant_message_id: "assistant-1", outcome_digest: `sha256:${"a".repeat(64)}`,
    },
  };
  const snapshot = {
    id: "c1", conversation_revision: 3, messages: [
      message({ id: "user-1", role: "user", metadata: { turn_id: "turn-1" } }),
      message({ id: "assistant-1", metadata: { turn_id: "turn-1" } }),
    ],
  };
  const classify = (value: typeof snapshot | null, result = turn) => savedTurnSnapshotState(result, value, "c1", "turn-1");
  assert.equal(classify(snapshot), "current");
  assert.equal(classify({ ...snapshot, conversation_revision: 4, messages: [] }), "changed");
  assert.equal(classify(null), "unavailable");
  assert.equal(classify({ ...snapshot, conversation_revision: 2 }), "pending");
  assert.equal(classify({ ...snapshot, id: "other" }), "pending");
  assert.equal(classify({ ...snapshot, messages: [] }), "pending");
  assert.equal(classify(snapshot, { ...turn, status: "running" }), "pending");
  assert.equal(classify(null, { ...turn, result_reference: undefined }), "pending");
  assert.equal(classify(snapshot, { ...turn, id: "other" }), "pending");
  assert.equal(classify(snapshot, { ...turn, result_reference: { ...turn.result_reference!, outcome_digest: "forged" } }), "pending");
  assert.match(savedTurnSnapshotNotice("changed")!, /その後更新/);
  assert.match(savedTurnSnapshotNotice("unavailable")!, /自動再送はしません/);
  assert.equal(savedTurnSnapshotNotice("current"), null);
});

test("stale user-only pending is cleared after reload grace", () => {
  const latest = message({ role: "user" });

  assert.equal(shouldClearPendingAfterConversationRefresh(latest, pending(1000), 1000 + PENDING_USER_ONLY_GRACE_MS - 1), false);
  assert.equal(shouldClearPendingAfterConversationRefresh(latest, pending(1000), 1000 + PENDING_USER_ONLY_GRACE_MS), true);
});

test("poll transport failures preserve the operation id until an explicit terminal response", () => {
  for (const error of [
    new TypeError("Failed to fetch"),
    new Error("network connection interrupted"),
    new Error("HTTP 500 Internal Server Error"),
    new Error("timeout while checking conversation"),
  ]) {
    assert.equal(shouldForgetPendingAfterPollError(error), false, error.message);
  }
  assert.equal(shouldForgetPendingAfterPollError(new Error("HTTP 404 Not Found\nconversation missing")), true);
  assert.equal(shouldForgetPendingAfterPollError(new Error("HTTP 410 Gone (EXPIRED)")), true);
  assert.equal(shouldForgetPendingAfterPollError(new Error("NOT_FOUND")), true);
});
