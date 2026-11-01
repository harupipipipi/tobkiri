import test from "node:test";
import assert from "node:assert/strict";
import { archiveScopedPendingChat, readScopedPendingChat, savedTurnStoreIdentity, writeScopedPendingChat } from "./scopedPendingChat";
import type { PendingChatRequest } from "./pendingChat";

const a = `sha256:${"a".repeat(64)}`;
const b = `sha256:${"b".repeat(64)}`;
const request: PendingChatRequest = {
  conversationId: "chat-1", operationId: "turn-1", savedTurn: true,
  hostRequestId: "11111111-1111-4111-8111-111111111111", submittedText: "使えるtool教えて",
  ownerTurnObserved: false, startedAt: 100, status: "照合中", toolNames: [],
};
function storage() {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
}

test("store A to B to A retains exact owner requests without adopting foreign pending", () => {
  const local = storage();
  writeScopedPendingChat(local, "default", a, { "chat-1": request });
  assert.deepEqual(readScopedPendingChat(local, "default", null).pending, {});
  const foreign = readScopedPendingChat(local, "default", b);
  assert.deepEqual(foreign.pending, {});
  assert.equal(foreign.recovery[0].request.operationId, "turn-1");
  writeScopedPendingChat(local, "default", b, {});
  assert.deepEqual(readScopedPendingChat(local, "default", a).pending, { "chat-1": request });
  assert.deepEqual(readScopedPendingChat(local, "other-profile", a).pending, {});
});

test("legacy IDs stay visible and unknown, never copied to verified store", () => {
  const local = storage();
  local.setItem("rumi-pending-chat-requests:default", JSON.stringify({ "chat-1": request }));
  const restored = readScopedPendingChat(local, "default", a);
  assert.deepEqual(restored.pending, {});
  assert.equal(restored.recovery[0].storeId, null);
  archiveScopedPendingChat(local, "default", restored.recovery[0]);
  const retained = readScopedPendingChat(local, "default", a);
  assert.equal(retained.recovery.length, 1);
  assert.equal(retained.recovery[0].archived, true);
  assert.equal(retained.recovery[0].request.submittedText, request.submittedText);
  assert.equal(local.getItem("rumi-pending-chat-requests:default"), JSON.stringify({ "chat-1": request }));
});

test("explicit local archive retains request identity/text without remote cancellation", () => {
  const local = storage();
  writeScopedPendingChat(local, "default", a, { "chat-1": request });
  archiveScopedPendingChat(local, "default", { id: `${a}:chat-1:turn-1`, storeId: a, request, archived: false });
  const retained = readScopedPendingChat(local, "default", a);
  assert.deepEqual(retained.pending, {});
  assert.deepEqual(retained.recovery[0].request, request);
  assert.equal(retained.recovery[0].archived, true);
});

test("only opaque canonical store identity is accepted", () => {
  assert.equal(savedTurnStoreIdentity(a), a);
  for (const value of [null, undefined, "/private/store.db", "activation-id", "a".repeat(64)]) {
    assert.equal(savedTurnStoreIdentity(value), null);
  }
});
