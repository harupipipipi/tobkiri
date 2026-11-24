import test from "node:test";
import assert from "node:assert/strict";
import { pendingRecoveryReadTargets } from "./usePendingRecoveryNotices";

test("recovery reads require the current validated store to own the retained entry", () => {
  const currentStoreId = `sha256:${"a".repeat(64)}`;
  const foreignStoreId = `sha256:${"b".repeat(64)}`;
  const target = (storeId: string | null, conversationId: string, operationId: string) => ({
    id: `${storeId ?? "legacy"}:${conversationId}:${operationId}`,
    storeId,
    conversationId,
    operationId,
    savedTurn: true,
  });
  const targets = [
    target(currentStoreId, "conversation-1", "turn-1"),
    target(foreignStoreId, "conversation-2", "turn-2"),
    target(null, "conversation-3", "turn-3"),
  ];

  assert.deepEqual(pendingRecoveryReadTargets({
    profileId: "defaults",
    currentStoreId,
    targets,
  }).map(({ id }) => id), [
    `${currentStoreId}:conversation-1:turn-1`,
  ]);
  assert.deepEqual(pendingRecoveryReadTargets({
    profileId: "defaults",
    currentStoreId: null,
    targets,
  }), []);
  assert.deepEqual(pendingRecoveryReadTargets({
    profileId: " ",
    currentStoreId,
    targets,
  }), []);
});
