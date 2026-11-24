import assert from "node:assert/strict";
import test from "node:test";
import type { SavedTurnEventSnapshot } from "./api";
import { savedTurnCanRestoreUnwrittenDraft, type PendingChatRequest } from "./pendingChat";
import { freshPendingTurnSnapshot, observePendingTerminal, publishPendingTerminal, removeObservedPendingTerminal, type PendingTerminalScope } from "./pendingTerminalObservation";

const expected: PendingChatRequest = {
  savedTurn: true, conversationId: "conversation-1", operationId: "turn-1",
  requestFingerprint: "sha256:fingerprint", startedAt: 1, status: "pending", toolNames: [],
};
const legacy: PendingChatRequest = { conversationId: "legacy-0049", startedAt: 1, status: "unknown", toolNames: [] };
const scope: PendingTerminalScope = {
  profileId: "defaults", storeId: "store-1", view: { conversationId: "conversation-1", workspaceTabId: "tab-1", epoch: 4 },
};
function receipt(status: "failed" | "cancelled" = "failed"): SavedTurnEventSnapshot {
  const identity = { turn_id: "turn-1", operation_id: "turn-1", conversation_id: "conversation-1", request_id: "owner-request-1", turn_revision: 3 };
  return {
    ...identity, status,
    turn: { id: "turn-1", conversation_id: "conversation-1", request_id: identity.request_id, status, revision: 3,
      result_reference: null as unknown as undefined },
    terminal: { ...identity, status, result_reference: null as unknown as undefined },
    events: [
      { ...identity, sequence: 0, name: "turn.queued", at: 1, details: {} },
      { ...identity, sequence: 1, name: "turn.running", at: 2, details: {
        phase: "saved_execution_claimed", user_message_id: `message:${"a".repeat(64)}`, assistant_message_id: `message:${"b".repeat(64)}`,
      } },
      { ...identity, sequence: 2, name: `turn.${status}`, at: 3, details: {
        phase: status === "failed" ? "saved_execution_failed" : "saved_execution_cancelled",
        error_code: "HOST_ACTION_FAILED", user_persistence: "saved", assistant_persistence: "not_written",
      } },
    ],
  };
}

test("a fresh failed receipt replaces the stale running snapshot after exactly the existing reconciliation", async () => {
  const initial = receipt();
  initial.status = initial.turn.status = "running";
  initial.terminal = null;
  const terminal = receipt();
  const order: string[] = [];
  const fresh = await freshPendingTurnSnapshot({
    initial, isCurrent: () => true,
    reconcile: async () => { order.push("existing reconcile"); return initial.turn; },
    readEvents: async () => { order.push("one event GET"); return terminal; },
  });
  assert.equal(fresh, terminal);
  assert.deepEqual(order, ["existing reconcile", "one event GET"]);
  assert.equal(initial.turn.status, "running");
  assert.ok(observePendingTerminal(fresh, expected, { [expected.conversationId]: expected }, scope, scope));
});

test("already terminal and completed receipts never request reconciliation or redundant event reads", async () => {
  for (const status of ["failed", "cancelled", "completed", "queued", "unknown"]) {
    const initial = receipt();
    initial.status = initial.turn.status = status;
    assert.equal(await freshPendingTurnSnapshot({
      initial, isCurrent: () => true,
      reconcile: () => assert.fail("extra POST"), readEvents: () => assert.fail("extra GET"),
    }), initial);
  }
});

test("late or failed active reads do not synthesize a terminal observation or retry", async () => {
  const initial = receipt();
  initial.status = initial.turn.status = "running";
  let current = false;
  assert.equal(await freshPendingTurnSnapshot({
    initial, isCurrent: () => current,
    reconcile: () => assert.fail("stale POST"), readEvents: () => assert.fail("stale GET"),
  }), null);
  current = true;
  assert.equal(await freshPendingTurnSnapshot({
    initial, isCurrent: () => current,
    reconcile: async () => { current = false; }, readEvents: () => assert.fail("stale GET"),
  }), null);
  current = true;
  assert.equal(await freshPendingTurnSnapshot({
    initial, isCurrent: () => current, reconcile: async () => undefined,
    readEvents: async () => { current = false; return receipt(); },
  }), null);
  let attempts = 0;
  await assert.rejects(freshPendingTurnSnapshot({
    initial, isCurrent: () => true, reconcile: async () => undefined,
    readEvents: async () => { attempts++; throw new Error("offline"); },
  }), /offline/);
  assert.equal(attempts, 1);
});

for (const status of ["failed", "cancelled"] as const) {
  test(`${status} with a null result is published before a hanging history read, without altering history or legacy`, async () => {
    const snapshot = receipt(status);
    let pending = { [expected.conversationId]: expected, [legacy.conversationId]: legacy };
    const messages = [{ role: "user", content: "saved user" }];
    const untouched = structuredClone(snapshot);
    const order: string[] = [];
    const published = publishPendingTerminal({
      snapshot, expected, current: pending, readScope: scope, currentScope: scope,
      publish: (observation) => {
        pending = removeObservedPendingTerminal(pending, expected, observation, scope, scope);
        order.push(observation.notice);
      },
      refreshHistory: () => { order.push("history"); return new Promise(() => undefined); },
    });
    assert.equal(published, true);
    assert.equal(pending[expected.conversationId], undefined);
    assert.equal(pending[legacy.conversationId], legacy);
    assert.equal(order.length, 1);
    assert.match(order[0], status === "failed" ? /HOST_ACTION_FAILED/ : /停止を確認/);
    await Promise.resolve();
    assert.equal(order[1], "history");
    assert.deepEqual(messages, [{ role: "user", content: "saved user" }]);
    assert.deepEqual(snapshot, untouched);
  });
}

test("a rejected history read cannot reintroduce pending or replace the terminal failure", async () => {
  const snapshot = receipt();
  let pending = { [expected.conversationId]: expected };
  let notice = "";
  publishPendingTerminal({
    snapshot, expected, current: pending, readScope: scope, currentScope: scope,
    publish: (observation) => {
      pending = removeObservedPendingTerminal(pending, expected, observation, scope, scope);
      notice = observation.notice;
    },
    refreshHistory: async () => { throw new Error("offline"); },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(pending, {});
  assert.match(notice, /HOST_ACTION_FAILED/);
});

test("foreign Profile, store, epoch, tab and conversation cannot publish termination", () => {
  for (const currentScope of [
    { ...scope, profileId: "foreign" }, { ...scope, storeId: "new-store" }, { ...scope, storeId: null },
    { ...scope, view: { ...scope.view, epoch: 6 } },
    { ...scope, view: { ...scope.view, workspaceTabId: "other-tab" } },
    { ...scope, view: { ...scope.view, conversationId: "other-conversation" } },
  ]) {
    assert.equal(publishPendingTerminal({
      snapshot: receipt(), expected, current: { [expected.conversationId]: expected }, readScope: scope, currentScope,
      publish: () => assert.fail("stale receipt published"), refreshHistory: () => assert.fail("stale read started"),
    }), false);
  }
});

test("malformed or foreign turn and terminal identities stay pending", () => {
  const snapshot = receipt();
  for (const candidate of [
    null, { ...snapshot, turn_id: "other" }, { ...snapshot, operation_id: "other" },
    { ...snapshot, conversation_id: "other" }, { ...snapshot, request_id: "bad/id" },
    { ...snapshot, turn_revision: 0 }, { ...snapshot, terminal: null },
    { ...snapshot, turn: { ...snapshot.turn, id: "other" } },
    { ...snapshot, turn: { ...snapshot.turn, revision: 2 } },
    { ...snapshot, turn: { ...snapshot.turn, request_id: "other" } },
    { ...snapshot, terminal: { ...snapshot.terminal!, request_id: "other" } },
    { ...snapshot, terminal: { ...snapshot.terminal!, turn_revision: 2 } },
    { ...snapshot, events: [{ ...snapshot.events[0], conversation_id: "other" }] },
  ]) assert.equal(observePendingTerminal(candidate, expected, { [expected.conversationId]: expected }, scope, scope), null);
});

test("late old terminal observations cannot remove a new request or changed fingerprint", () => {
  const observation = observePendingTerminal(receipt(), expected, { [expected.conversationId]: expected }, scope, scope)!;
  for (const entry of [
    { ...expected, operationId: "new-turn" }, { ...expected, requestFingerprint: "different" },
    { ...expected, savedTurn: false }, { ...expected, conversationId: "other" },
  ]) {
    const pending = { [expected.conversationId]: entry, [legacy.conversationId]: legacy };
    assert.equal(removeObservedPendingTerminal(pending, expected, observation, scope, scope), pending);
    assert.equal(observePendingTerminal(receipt(), expected, pending, scope, scope), null);
  }
  assert.equal(observePendingTerminal(receipt(), { ...expected, requestFingerprint: undefined }, { [expected.conversationId]: expected }, scope, scope), null);
});

test("completed, active and unknown records keep their existing reconciliation path and never start history", () => {
  for (const status of ["completed", "running", "queued", "waiting", "unknown"]) {
    const candidate = receipt();
    candidate.status = status;
    candidate.turn.status = status;
    candidate.terminal!.status = status;
    const pending = { [expected.conversationId]: expected };
    assert.equal(publishPendingTerminal({
      snapshot: candidate, expected, current: pending, readScope: scope, currentScope: scope,
      publish: () => assert.fail("non-failure changed pending"), refreshHistory: () => assert.fail("non-failure started history"),
    }), false);
    assert.equal(pending[expected.conversationId], expected);
  }
});

test("terminal publication performs no transport request and user-saved evidence never restores a draft", () => {
  const snapshot = receipt();
  const turn = { ...snapshot.turn, events: snapshot.events };
  assert.equal(savedTurnCanRestoreUnwrittenDraft(turn, { id: expected.conversationId, messages: [] }, expected.conversationId, expected.operationId!), false);
  const unwritten = { ...turn, events: snapshot.events.map((event) => event.name === "turn.failed"
    ? { ...event, details: { ...event.details, user_persistence: "not_written" } } : event) };
  assert.equal(savedTurnCanRestoreUnwrittenDraft(unwritten, { id: expected.conversationId, messages: [] }, expected.conversationId, expected.operationId!), true);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => assert.fail("terminal display must not POST, resend or perform any network request");
  try {
    assert.equal(publishPendingTerminal({
      snapshot, expected, current: { [expected.conversationId]: expected }, readScope: scope, currentScope: scope,
      publish: () => undefined, refreshHistory: () => Promise.resolve(),
    }), true);
  } finally { globalThis.fetch = originalFetch; }
});
