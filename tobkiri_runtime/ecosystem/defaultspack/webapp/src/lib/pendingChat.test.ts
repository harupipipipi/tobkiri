import test from "node:test";
import assert from "node:assert/strict";

import type { ChatMessage, Conversation, SavedTurnResult } from "./api";
import {
  PENDING_USER_ONLY_GRACE_MS,
  canClearSavedTurnStopFailureError,
  chatContinuationPacketMatchesTurn,
  isGenerationActiveForView,
  markSavedTurnOwnerObserved,
  resetPersistedSavedTurnOwnerObservations,
  savedTurnControlActionsReady,
  savedTurnSnapshotState,
  savedTurnSnapshotNotice,
  savedTurnProgressNotice,
  savedTurnProgressState,
  savedTurnCanRestoreUnwrittenDraft,
  savedTurnTerminalNotice,
  shouldReconcileSavedTurnAfterAcknowledgedStop,
  updateSavedTurnNotice,
  type SavedTurnStopAcknowledgement,
  type SavedTurnSubmissionAttempt,
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

test("late saved stop notices stay with their original pending turn", () => {
  const first = { ...pending(1000), savedTurn: true, operationId: "turn-1" };
  const second = { ...first, conversationId: "c2", operationId: "turn-2" };
  const current = { c1: first, c2: second };
  const updated = updateSavedTurnNotice(current, "c1", "turn-1", "stop requested");
  assert.equal(updated.c1.status, "stop requested");
  assert.equal(updated.c2, second);
  assert.equal(updated.c1.operationId, "turn-1");
  assert.equal(first.status, "Processing...");
  const stale: Record<string, PendingChatRequest>[] = [
    { c2: second },
    { c1: { ...first, operationId: "new-turn" } },
    { c1: { ...first, savedTurn: false } },
    { c1: { ...first, conversationId: "c2" } },
  ];
  for (const records of stale) {
    assert.equal(updateSavedTurnNotice(records, "c1", "turn-1", "late reply"), records);
  }
});

test("generation state belongs to the selected pending root or matching view ticket", () => {
  const activeViewTicket = { workspaceTabId: "workspace-b", conversationId: "c2", epoch: 8 };
  const input = {
    activeConversationId: "c2",
    activeViewTicket,
    globalIsGenerating: true,
    isConversationPending: false,
    streamingConversationId: null,
    submissionViewTicket: null,
  };

  // An unresolved Stop in tab A cannot put a completed tab B into steer mode.
  assert.equal(isGenerationActiveForView(input), false);
  assert.equal(isGenerationActiveForView({ ...input, isConversationPending: true }), true);
  assert.equal(isGenerationActiveForView({ ...input, streamingConversationId: "c2" }), true);
  assert.equal(isGenerationActiveForView({
    ...input,
    submissionViewTicket: { ...activeViewTicket },
  }), true);
});

test("saved-turn controls wait for a readable exact active root", () => {
  const registrationTicket = {
    workspaceTabId: "workspace-a",
    conversationId: "c1",
    epoch: 4,
  };
  const request: PendingChatRequest = {
    ...pending(1000),
    operationId: "turn-1",
    ownerTurnObserved: false,
    requestFingerprint: "sha256:request-1",
    savedTurn: true,
  };
  const current = { c1: request };
  const runningRoot = { id: "turn-1", conversation_id: "c1", status: "running" };

  assert.equal(savedTurnControlActionsReady(true, request), false);
  const observed = markSavedTurnOwnerObserved(
    current,
    request,
    runningRoot,
    registrationTicket,
    registrationTicket,
  );
  assert.notEqual(observed, current);
  assert.equal(observed.c1.ownerTurnObserved, true);
  assert.equal(savedTurnControlActionsReady(true, observed.c1), true);
  assert.equal(savedTurnControlActionsReady(true, { ...request, savedTurn: false }), true);
});

test("saved-turn registration rejects foreign, retried, stale-view, and terminal observations", () => {
  const registrationTicket = {
    workspaceTabId: "workspace-a",
    conversationId: "c1",
    epoch: 4,
  };
  const request: PendingChatRequest = {
    ...pending(1000),
    operationId: "turn-1",
    ownerTurnObserved: false,
    requestFingerprint: "sha256:request-1",
    savedTurn: true,
  };
  const current = { c1: request };
  const cases: Array<{
    label: string;
    expected?: PendingChatRequest;
    turn: { id: string; conversation_id: string; status: string };
    ticket?: typeof registrationTicket;
    currentTicket?: typeof registrationTicket;
  }> = [
    {
      label: "wrong root",
      turn: { id: "turn-2", conversation_id: "c1", status: "running" },
    },
    {
      label: "wrong conversation",
      turn: { id: "turn-1", conversation_id: "c2", status: "running" },
    },
    {
      label: "retried root",
      expected: { ...request, operationId: "turn-2" },
      turn: { id: "turn-2", conversation_id: "c1", status: "running" },
    },
    {
      label: "old view",
      turn: { id: "turn-1", conversation_id: "c1", status: "running" },
      ticket: registrationTicket,
      currentTicket: { ...registrationTicket, epoch: registrationTicket.epoch + 1 },
    },
    {
      label: "terminal root",
      turn: { id: "turn-1", conversation_id: "c1", status: "completed" },
    },
  ];
  for (const testCase of cases) {
    assert.equal(
      markSavedTurnOwnerObserved(
        current,
        testCase.expected ?? request,
        testCase.turn,
        testCase.ticket ?? registrationTicket,
        testCase.currentTicket ?? testCase.ticket ?? registrationTicket,
      ),
      current,
      testCase.label,
    );
  }
});

test("a fresh current-view root read restores readiness after a tab ABA", () => {
  const originalTicket = {
    workspaceTabId: "workspace-a",
    conversationId: "c1",
    epoch: 4,
  };
  const returnedTicket = { ...originalTicket, epoch: 6 };
  const request: PendingChatRequest = {
    ...pending(1000),
    operationId: "turn-1",
    ownerTurnObserved: false,
    requestFingerprint: "sha256:request-1",
    savedTurn: true,
  };
  const current = { c1: request };
  const root = { id: "turn-1", conversation_id: "c1", status: "waiting" };

  assert.equal(
    markSavedTurnOwnerObserved(current, request, root, originalTicket, returnedTicket),
    current,
    "an old pending read cannot mark the returned view",
  );
  const observed = markSavedTurnOwnerObserved(
    current,
    request,
    root,
    returnedTicket,
    returnedTicket,
  );
  assert.equal(observed.c1.ownerTurnObserved, true);
  assert.equal(savedTurnControlActionsReady(true, observed.c1), true);
});

test("a restored saved request requires a fresh current-view root read", () => {
  const ticket = { workspaceTabId: "workspace-a", conversationId: "c1", epoch: 4 };
  const persisted: Record<string, PendingChatRequest> = {
    c1: {
      ...pending(1000),
      operationId: "turn-1",
      ownerTurnObserved: true,
      requestFingerprint: "sha256:request-1",
      savedTurn: true,
    },
  };
  const restored = resetPersistedSavedTurnOwnerObservations(persisted);
  assert.notEqual(restored, persisted);
  assert.equal(restored.c1.ownerTurnObserved, false);
  assert.equal(savedTurnControlActionsReady(true, restored.c1), false);

  const observed = markSavedTurnOwnerObserved(
    restored,
    restored.c1,
    { id: "turn-1", conversation_id: "c1", status: "queued" },
    ticket,
    ticket,
  );
  assert.equal(observed.c1.ownerTurnObserved, true);
});

test("a completed root becomes control-ready only through its exact active guidance child", () => {
  const ticket = { workspaceTabId: "workspace-a", conversationId: "c1", epoch: 4 };
  const request: PendingChatRequest = {
    ...pending(1000),
    operationId: "turn-root",
    ownerTurnObserved: false,
    requestFingerprint: "sha256:request-1",
    savedTurn: true,
  };
  const current = { c1: request };
  const completedRoot = { id: "turn-root", conversation_id: "c1", status: "completed" };
  const activeChild = { id: "turn-child", conversation_id: "c1", status: "running" };

  assert.equal(
    markSavedTurnOwnerObserved(current, request, completedRoot, ticket, ticket),
    current,
    "a terminal root without a verified active child stays inert",
  );
  assert.equal(
    markSavedTurnOwnerObserved(
      current,
      request,
      completedRoot,
      ticket,
      ticket,
      { ...activeChild, conversation_id: "c2" },
    ),
    current,
    "a foreign child cannot authorize controls",
  );
  const observed = markSavedTurnOwnerObserved(
    current,
    request,
    completedRoot,
    ticket,
    ticket,
    activeChild,
  );
  assert.equal(observed.c1.ownerTurnObserved, true);
});

test("pre-create generation requires an exact owned ticket and rejects ABA or foreign views", () => {
  const preCreateTicket = { workspaceTabId: "workspace-new", conversationId: null, epoch: 3 };
  const input = {
    activeConversationId: null,
    activeViewTicket: preCreateTicket,
    globalIsGenerating: true,
    isConversationPending: false,
    streamingConversationId: null,
    submissionViewTicket: { ...preCreateTicket },
  };

  assert.equal(isGenerationActiveForView(input), true);
  assert.equal(isGenerationActiveForView({
    ...input,
    submissionViewTicket: { ...preCreateTicket, workspaceTabId: "workspace-other" },
  }), false);
  assert.equal(isGenerationActiveForView({
    ...input,
    submissionViewTicket: { ...preCreateTicket, epoch: 4 },
  }), false);
  assert.equal(isGenerationActiveForView({
    ...input,
    submissionViewTicket: null,
  }), false);
});

test("a delayed stop receipt cannot clear a newer same-view error generation", () => {
  assert.equal(canClearSavedTurnStopFailureError(12, 12), true);
  assert.equal(canClearSavedTurnStopFailureError(12, 13), false);
  assert.equal(canClearSavedTurnStopFailureError(12, -1), false);
});

test("acknowledged same-root stop reclassifies only its cancellation 503 for reconciliation", () => {
  const attempt: SavedTurnSubmissionAttempt = {
    attemptId: "attempt-1",
    conversationId: "c1",
    requestFingerprint: "sha256:request-1",
    turnId: "turn-1",
    viewTicket: { workspaceTabId: "workspace-a", conversationId: "c1", epoch: 4 },
  };
  const acknowledgement: SavedTurnStopAcknowledgement = {
    ...attempt,
    status: "cancellation_requested",
  };
  const cancellation503 = new Error([
    "HTTP 503 Service Unavailable",
    "backend または provider 側の障害です。少し待って再試行してください。",
    "詳細: The runtime operation is unavailable",
  ].join("\n"));

  assert.equal(
    shouldReconcileSavedTurnAfterAcknowledgedStop(
      acknowledgement,
      attempt,
      cancellation503,
    ),
    true,
  );
  assert.equal(
    shouldReconcileSavedTurnAfterAcknowledgedStop(
      { ...acknowledgement, status: "stopped_confirmed" },
      attempt,
      cancellation503,
    ),
    true,
  );
});

test("unacknowledged or failed stops leave the original saved 503 as an error", () => {
  const attempt: SavedTurnSubmissionAttempt = {
    attemptId: "attempt-1",
    conversationId: "c1",
    requestFingerprint: "sha256:request-1",
    turnId: "turn-1",
    viewTicket: { workspaceTabId: "workspace-a", conversationId: "c1", epoch: 4 },
  };
  const cancellation503 = new Error([
    "HTTP 503 Service Unavailable",
    "backend または provider 側の障害です。少し待って再試行してください。",
    "詳細: The runtime operation is unavailable",
  ].join("\n"));

  assert.equal(
    shouldReconcileSavedTurnAfterAcknowledgedStop(null, attempt, cancellation503),
    false,
  );
  assert.equal(
    shouldReconcileSavedTurnAfterAcknowledgedStop(
      { ...attempt, status: "rejected" as "cancellation_requested" },
      attempt,
      cancellation503,
    ),
    false,
  );
  assert.equal(
    shouldReconcileSavedTurnAfterAcknowledgedStop(
      { ...attempt, status: "cancellation_requested" },
      attempt,
      new Error("HTTP 500 Internal Server Error"),
    ),
    false,
  );
});

test("foreign roots, attempts, and view-ticket ABA stops cannot mask a provider 503", () => {
  const attempt: SavedTurnSubmissionAttempt = {
    attemptId: "attempt-1",
    conversationId: "c1",
    requestFingerprint: "sha256:request-1",
    turnId: "turn-1",
    viewTicket: { workspaceTabId: "workspace-a", conversationId: "c1", epoch: 4 },
  };
  const cancellation503 = new Error([
    "HTTP 503 Service Unavailable",
    "backend または provider 側の障害です。少し待って再試行してください。",
    "詳細: The runtime operation is unavailable",
  ].join("\n"));
  for (const acknowledgement of [
    { ...attempt, turnId: "turn-2", status: "cancellation_requested" as const },
    { ...attempt, conversationId: "c2", viewTicket: { ...attempt.viewTicket, conversationId: "c2" }, status: "cancellation_requested" as const },
    { ...attempt, requestFingerprint: "sha256:request-2", status: "cancellation_requested" as const },
    { ...attempt, attemptId: "attempt-2", status: "cancellation_requested" as const },
    { ...attempt, viewTicket: { ...attempt.viewTicket, epoch: 6 }, status: "cancellation_requested" as const },
  ]) {
    assert.equal(
      shouldReconcileSavedTurnAfterAcknowledgedStop(acknowledgement, attempt, cancellation503),
      false,
      JSON.stringify(acknowledgement),
    );
  }
});

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
  assert.deepEqual(savedTurnSnapshotNotice("changed"), {
    tone: "success",
    title: "送信を確認しました",
    message: "送信の保存完了を確認しました。会話はその後更新されているため、現在の内容を表示しています。",
  });
  assert.deepEqual(savedTurnSnapshotNotice("unavailable"), {
    tone: "warning",
    title: "現在の会話を確認できません",
    message: "送信の保存完了を確認しましたが、現在の会話は取得できません。自動再送はしません。",
  });
  assert.equal(savedTurnSnapshotNotice("current"), null);
});

test("pending saved turn distinguishes owner message persistence without authorizing replay", () => {
  const turn: SavedTurnResult["turn"] = {
    id: "turn-1", conversation_id: "c1", status: "waiting", revision: 3,
    events: [{
      name: "turn.running",
      details: {
        phase: "saved_execution_claimed",
        user_message_id: "message:" + "a".repeat(64),
        assistant_message_id: "message:" + "b".repeat(64),
      },
    }],
  };
  const snapshot = {
    id: "c1", conversation_revision: 2, messages: [
      message({
        id: "message:" + "a".repeat(64),
        role: "user",
        metadata: { turn_id: "turn-1" },
      }),
    ],
  };
  const classify = (
    value: typeof snapshot | null,
    result = turn,
  ) => savedTurnProgressState(result, value, "c1", "turn-1");
  assert.equal(classify({ ...snapshot, messages: [] }), "ledger_only");
  assert.equal(classify(snapshot), "user_saved");
  assert.equal(classify({
    ...snapshot,
    conversation_revision: 3,
    messages: [
      ...snapshot.messages,
      message({
        id: "message:" + "b".repeat(64),
        metadata: { turn_id: "turn-1" },
        finish_reason: "stop",
      }),
    ],
  }), "all_messages_saved_unconfirmed");
  assert.equal(classify({
    ...snapshot,
    messages: [
      ...snapshot.messages,
      message({
        id: "message:" + "b".repeat(64),
        metadata: { turn_id: "turn-1", thinking: { state: "streaming" } },
        finish_reason: "streaming",
      }),
    ],
  }), "user_saved");
  assert.equal(classify(null), "conversation_unavailable");
  assert.equal(classify(snapshot, { ...turn, id: "other" }), "ledger_only");
  assert.equal(classify(snapshot, { ...turn, events: [] }), "ledger_only");
  assert.equal(classify({
    ...snapshot,
    messages: [message({
      id: "forged",
      role: "user",
      metadata: { turn_id: "turn-1" },
    })],
  }), "ledger_only");
  assert.match(savedTurnProgressNotice("user_saved"), /assistant の保存状態/);
  assert.match(savedTurnProgressNotice("all_messages_saved_unconfirmed"), /完了状態/);
  assert.match(savedTurnProgressNotice("conversation_unavailable"), /取得できません/);
  assert.match(savedTurnProgressNotice("ledger_only"), /保存状態/);
});

test("only matching terminal saved turns stop reconciliation", () => {
  const turn: SavedTurnResult["turn"] = {
    id: "turn-1", conversation_id: "c1", status: "failed", revision: 4,
  };
  assert.match(savedTurnTerminalNotice(turn, "c1", "turn-1")!, /失敗で終了/);
  assert.match(savedTurnTerminalNotice({ ...turn, status: "cancelled" }, "c1", "turn-1")!, /停止を確認/);
  for (const candidate of [
    { ...turn, status: "running" },
    { ...turn, status: "waiting" },
    { ...turn, status: "queued" },
    { ...turn, status: "unknown" },
    { ...turn, id: "other" },
    { ...turn, conversation_id: "other" },
  ]) {
    assert.equal(savedTurnTerminalNotice(candidate, "c1", "turn-1"), null);
  }
});

test("only an exact root receipt proving no persistence permits draft restoration", () => {
  const userMessageId = `message:${"a".repeat(64)}`;
  const assistantMessageId = `message:${"b".repeat(64)}`;
  const noMessages: Pick<Conversation, "id" | "messages"> = { id: "c1", messages: [] };
  const failed: SavedTurnResult["turn"] = {
    id: "turn-1", conversation_id: "c1", status: "failed", revision: 3,
    events: [
      {
        name: "turn.running",
        details: {
          phase: "saved_execution_claimed",
          user_message_id: userMessageId,
          assistant_message_id: assistantMessageId,
        },
      },
      {
        name: "turn.failed",
        details: {
          phase: "saved_execution_failed",
          user_persistence: "not_written",
          assistant_persistence: "not_written",
        },
      },
    ],
  };
  const canRestore = (
    turn: SavedTurnResult["turn"],
    conversation: typeof noMessages | null = noMessages,
  ) => savedTurnCanRestoreUnwrittenDraft(turn, conversation, "c1", "turn-1");

  assert.equal(canRestore(failed), true);
  assert.equal(canRestore({
    ...failed,
    status: "cancelled",
    events: [failed.events![0], {
      name: "turn.cancelled",
      details: {
        phase: "saved_execution_cancelled",
        user_persistence: "not_written",
        assistant_persistence: "not_written",
      },
    }],
  }), true);
  assert.equal(canRestore({ ...failed, id: "other" }), false);
  assert.equal(canRestore({ ...failed, result_reference: {
    conversation_id: "c1", conversation_revision: 2, user_message_id: userMessageId,
    assistant_message_id: assistantMessageId, outcome_digest: `sha256:${"c".repeat(64)}`,
  } }), false);
  assert.equal(canRestore({ ...failed, guidance_parent_turn_id: "parent" }), false);
  assert.equal(canRestore({ ...failed, guidance: [{ id: "guidance-1", status: "queued", value: {} }] }), false);
  assert.equal(canRestore({ ...failed, events: [failed.events![0]] }), false);
  assert.equal(canRestore({
    ...failed,
    events: [...failed.events!, failed.events![1]],
  }), false);
  assert.equal(canRestore(failed, {
    id: "c1",
    messages: [message({
      id: userMessageId,
      role: "user",
      metadata: { turn_id: "turn-1" },
    })],
  }), false);
  assert.equal(canRestore(failed, {
    id: "c1",
    messages: [message({ id: assistantMessageId, role: "assistant", metadata: null })],
  }), false);
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

test("continuation packets bind to the exact pending saved turn", () => {
  const packet = {
    turn_id: "turn-1",
    conversation_id: "c1",
    operation_id: "turn-1",
    request_id: "apr-1",
    terminal: null,
  };
  assert.equal(
    chatContinuationPacketMatchesTurn(packet, "turn-1", "c1", "apr-1"),
    true,
  );
  const completed = {
    ...packet,
    terminal: {
      turn_id: "turn-1",
      conversation_id: "c1",
      operation_id: "turn-1",
      request_id: "apr-1",
      status: "completed" as const,
    },
  };
  assert.equal(
    chatContinuationPacketMatchesTurn(completed, "turn-1", "c1", "apr-1"),
    true,
  );
});

test("continuation packets from foreign identities are rejected", () => {
  const base = {
    turn_id: "turn-1",
    conversation_id: "c1",
    operation_id: "turn-1",
    request_id: "apr-1",
    terminal: null,
  };
  for (const packet of [
    { ...base, turn_id: "foreign-turn" },
    { ...base, turn_id: undefined },
    { ...base, conversation_id: "c2" },
    { ...base, operation_id: "turn-2" },
    { ...base, request_id: "apr-2" },
    { ...base, terminal: undefined },
    {
      ...base,
      terminal: { ...base, status: "running" },
    } as unknown as Parameters<typeof chatContinuationPacketMatchesTurn>[0],
    {
      ...base,
      terminal: {
        turn_id: "turn-2",
        conversation_id: "c1",
        operation_id: "turn-2",
        request_id: "apr-1",
        status: "completed" as const,
      },
    },
  ]) {
    assert.equal(
      chatContinuationPacketMatchesTurn(packet, "turn-1", "c1", "apr-1"),
      false,
      JSON.stringify(packet),
    );
  }
  // A packet bound to one turn can never be projected onto another pending turn.
  assert.equal(
    chatContinuationPacketMatchesTurn(base, "turn-2", "c1", "apr-1"),
    false,
  );
});

test("unbound continuation packets require an explicit empty turn", () => {
  const packet = {
    turn_id: "",
    conversation_id: "c1",
    operation_id: "",
    request_id: "apr-1",
    terminal: null,
  };
  assert.equal(chatContinuationPacketMatchesTurn(packet, "", "c1", "apr-1"), true);
  assert.equal(chatContinuationPacketMatchesTurn(packet, "turn-1", "c1", "apr-1"), false);
  assert.equal(
    chatContinuationPacketMatchesTurn({ ...packet, turn_id: "turn-1" }, "", "c1", "apr-1"),
    false,
  );
});
