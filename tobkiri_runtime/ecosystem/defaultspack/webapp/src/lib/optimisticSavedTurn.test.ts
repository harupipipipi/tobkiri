import test from "node:test";
import assert from "node:assert/strict";

import {
  bindOptimisticSavedTurnExpectedUserMessageId,
  bindOptimisticSavedTurnOverlay,
  createOptimisticSavedTurnOverlay,
  expectedSavedTurnUserMessageId,
  SavedTurnViewFence,
  shouldDisplayOptimisticSavedTurnOverlay,
} from "./optimisticSavedTurn";

const conversationId = "conversation-1";
const operationId = "turn-1";
const userMessageId = `message:${"a".repeat(64)}`;
const assistantMessageId = `message:${"b".repeat(64)}`;

function activeViewTicket() {
  return new SavedTurnViewFence("workspace-a", null).capture();
}

function overlay() {
  return bindOptimisticSavedTurnOverlay(
    createOptimisticSavedTurnOverlay({
      clientId: "local-1",
      content: "hello",
      createdAt: 1,
      viewTicket: activeViewTicket(),
    }),
    {
      conversationId,
      operationId,
      requestFingerprint: "sha256:request",
    },
  );
}

function runningTurn() {
  return {
    id: operationId,
    conversation_id: conversationId,
    status: "running",
    revision: 2,
    events: [{
      name: "turn.running",
      details: {
        phase: "saved_execution_claimed",
        user_message_id: userMessageId,
      },
    }],
  };
}

test("new-conversation draft is displayable before a durable conversation id exists", () => {
  const draft = createOptimisticSavedTurnOverlay({
      clientId: "new-draft",
      content: "hello",
      createdAt: 1,
      viewTicket: activeViewTicket(),
  });

  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(draft, {
    activeConversationId: null,
    activeOperationId: null,
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [],
  }), true);
  assert.equal(draft.message.raw_text, "hello");
  assert.equal(draft.message.conversation_id, "");
});

test("only the claimed exact root user id replaces the optimistic overlay", () => {
  const bound = bindOptimisticSavedTurnExpectedUserMessageId(overlay(), runningTurn());
  assert.equal(bound.expectedUserMessageId, userMessageId);

  const canonicalUser = {
    id: userMessageId,
    role: "user",
    content: "hello",
    created_at: 1,
    conversation_id: conversationId,
    metadata: { turn_id: operationId },
  };
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: conversationId,
    activeOperationId: operationId,
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [canonicalUser],
  }), false);
});

test("same text, timestamp, or root metadata never replaces without the claimed id", () => {
  const bound = bindOptimisticSavedTurnExpectedUserMessageId(overlay(), runningTurn());
  const foreignMessage = {
    id: `message:${"c".repeat(64)}`,
    role: "user",
    content: "hello",
    created_at: 1,
    conversation_id: conversationId,
    metadata: { turn_id: operationId },
  };

  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: conversationId,
    activeOperationId: operationId,
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [foreignMessage],
  }), true);
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: conversationId,
    activeOperationId: operationId,
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [{
      ...foreignMessage,
      id: userMessageId,
      metadata: { turn_id: "other-turn" },
    }],
  }), true);
});

test("overlay is isolated to its bound conversation and operation across ABA", () => {
  const bound = bindOptimisticSavedTurnExpectedUserMessageId(overlay(), runningTurn());
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: "conversation-2",
    activeOperationId: "turn-2",
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [],
  }), false);
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: conversationId,
    activeOperationId: "turn-2",
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [],
  }), false);
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: conversationId,
    activeOperationId: null,
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [],
  }), false);
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(bound, {
    activeConversationId: conversationId,
    activeOperationId: operationId,
    activeViewTicket: activeViewTicket(),
    canonicalMessages: [],
  }), true);
});

test("unbound new-conversation overlay does not revive after a null-to-null tab ABA", () => {
  const fence = new SavedTurnViewFence("workspace-a", null);
  const originalTicket = fence.capture();
  const draft = createOptimisticSavedTurnOverlay({
    clientId: "new-draft",
    content: "hello",
    createdAt: 1,
    viewTicket: originalTicket,
  });

  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(draft, {
    activeConversationId: null,
    activeOperationId: null,
    activeViewTicket: fence.capture(),
    canonicalMessages: [],
  }), true);
  fence.synchronize("workspace-b", null);
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(draft, {
    activeConversationId: null,
    activeOperationId: null,
    activeViewTicket: fence.capture(),
    canonicalMessages: [],
  }), false);
  fence.synchronize("workspace-a", null);
  assert.equal(fence.matches(originalTicket), false);
  assert.equal(shouldDisplayOptimisticSavedTurnOverlay(draft, {
    activeConversationId: null,
    activeOperationId: null,
    activeViewTicket: fence.capture(),
    canonicalMessages: [],
  }), false);
  assert.equal(fence.matches(fence.capture()), true);
});

test("terminal references provide an expected id only with an exact completed root", () => {
  const completed = {
    ...runningTurn(),
    status: "completed",
    result_reference: {
      conversation_id: conversationId,
      conversation_revision: 3,
      user_message_id: userMessageId,
      assistant_message_id: assistantMessageId,
      outcome_digest: `sha256:${"d".repeat(64)}`,
    },
  };

  assert.equal(expectedSavedTurnUserMessageId(completed, conversationId, operationId), userMessageId);
  assert.equal(expectedSavedTurnUserMessageId({ ...completed, id: "other" }, conversationId, operationId), null);
  assert.equal(expectedSavedTurnUserMessageId({ ...runningTurn(), status: "failed" }, conversationId, operationId), null);
  assert.equal(expectedSavedTurnUserMessageId({
    ...completed,
    result_reference: { ...completed.result_reference, outcome_digest: "invalid" },
  }, conversationId, operationId), null);
});
