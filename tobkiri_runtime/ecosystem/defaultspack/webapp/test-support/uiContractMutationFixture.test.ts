import assert from "node:assert/strict";
import test from "node:test";
import { pendingSavedTurnChainForConversation } from "../src/App";
import { api, type SavedTurn } from "../src/lib/api";
import { canonicalRequestQuery } from "../e2e/contractRequestMatcher";
import { completedSavedTurnFixtureEvents, completedSavedTurnFixtureList, createConversationFixtureState, conversationFixtureMutation, FixtureMutationError, settingsFixtureMutation } from "./uiContractMutationFixture";
const settings = { changes: { general: { manual_runtime_mode_selection: true } }, expected_revision: 1 };
const preferences = { mode: "manual", include: [{ kind: "service", id: "github" }], exclude: [], scope: "conversation", strategy: null, must_use: false, review: false, preview_id: null };
const conversation = { conversation_id: "c-smoke", updates: { metadata: { tool_preferences: preferences } }, expected_conversation_revision: 1 };
const rejects = (fn: () => unknown, status: number) => assert.throws(fn, (error) => error instanceof FixtureMutationError && error.status === status);

test("Settings fixture returns only changed fields and increments the expected revision", () => {
  const receipt = settingsFixtureMutation(settings, 1);
  assert.deepEqual(receipt, { values: settings.changes, document_revision: 2 });
  assert.deepEqual(settingsFixtureMutation({ ...settings, expected_revision: 2 }, 2).document_revision, 3);
  rejects(() => settingsFixtureMutation(settings, receipt.document_revision), 409);
});
test("Settings fixture rejects legacy, unknown, malformed and mixed writes without a receipt", () => {
  for (const payload of [null, { values: settings.changes }, { patches: [] }, { ...settings, expected_revision: "1" },
    { ...settings, changes: {} }, { ...settings, changes: { general: { manual_runtime_mode_selection: "true" } } },
    { ...settings, changes: { general: { manual_runtime_mode_selection: true, extra: true } } },
    { ...settings, changes: { ...settings.changes, tools: { action_approval_mode: "full" } } }, { ...settings, approved: true }]) {
    rejects(() => settingsFixtureMutation(payload, 1), 400);
  }
  assert.equal(settingsFixtureMutation(settings, 1).document_revision, 2);
});
test("conversation fixture persists a detached canonical preferences snapshot and enforces CAS", () => {
  const receipt = conversationFixtureMutation(conversation, 1);
  assert.deepEqual(receipt, { metadata: conversation.updates.metadata, conversation_revision: 2 });
  assert.notEqual(receipt.metadata.tool_preferences, preferences);
  rejects(() => conversationFixtureMutation(conversation, receipt.conversation_revision), 409);
  const disabled = { ...conversation, expected_conversation_revision: 2, updates: { metadata: { tool_preferences: { ...preferences, include: [] } } } };
  assert.deepEqual(conversationFixtureMutation(disabled, 2).metadata.tool_preferences.include, []);
});
test("conversation fixture rejects other targets, fields and malformed preferences", () => {
  for (const payload of [null, { ...conversation, conversation_id: "another-chat" }, { ...conversation, expected_conversation_revision: "1" },
    { ...conversation, updates: { title: "unrequested" } }, { ...conversation, updates: { ...conversation.updates, model: "unregistered" } },
    { ...conversation, updates: { metadata: { tool_preferences: { ...preferences, include: [{ kind: "service", id: "unknown" }] } } } },
    { ...conversation, updates: { metadata: { tool_preferences: { ...preferences, review: "true" } } } },
    { ...conversation, updates: { metadata: { tool_preferences: { ...preferences, extra: true } } } }]) {
    rejects(() => conversationFixtureMutation(payload, 1), 400);
  }
  assert.equal(conversationFixtureMutation(conversation, 1).conversation_revision, 2);
});

test("conversation fixture requires an actual mode string and positive integer revision", () => {
  for (const mode of [["auto"], ["manual"], null, true, 1, { mode: "auto" }]) {
    rejects(() => conversationFixtureMutation({ ...conversation, updates: { metadata: { tool_preferences: { ...preferences, mode } } } }, 1), 400);
  }
  for (const expected_conversation_revision of [0, -1, 1.5, true]) {
    rejects(() => conversationFixtureMutation({ ...conversation, expected_conversation_revision }, 1), 400);
  }
});
test("conversation fixture carries unrelated metadata unchanged and replaces only the submitted snapshot", () => {
  const metadata = { unrelated: { label: "keep", nested: [1, 2] }, tool_preferences: { obsolete: true } };
  const payload = { ...conversation, updates: { metadata: { unrelated: metadata.unrelated, tool_preferences: preferences } } };
  const receipt = conversationFixtureMutation(payload, 1, metadata);
  assert.deepEqual(receipt.metadata, payload.updates.metadata);
  assert.notEqual(receipt.metadata.unrelated, metadata.unrelated);
  assert.equal("obsolete" in receipt.metadata.tool_preferences, false);
  rejects(() => conversationFixtureMutation(conversation, 1, metadata), 400);
  rejects(() => conversationFixtureMutation({ ...payload, updates: { metadata: { ...payload.updates.metadata, unrelated: { label: "changed" } } } }, 1, metadata), 400);
  assert.deepEqual(metadata.tool_preferences, { obsolete: true });
});
test("saved-turn completion cannot reset later conversation CAS and stale writes leave state unchanged", () => {
  const state = createConversationFixtureState(1, { unrelated: { label: "preserved" } });
  const patch = (expected_conversation_revision: number) => ({ ...conversation, expected_conversation_revision,
    updates: { metadata: { ...state.snapshot().metadata, tool_preferences: preferences } } });
  assert.equal(state.mutate(patch(1)).conversation_revision, 2);
  assert.equal(state.completeTurn("c-smoke", 2).conversation_revision, 3);
  assert.equal(state.mutate(patch(3)).conversation_revision, 4);
  assert.equal(state.snapshot().conversation_revision, 4);
  assert.deepEqual(state.snapshot().metadata.unrelated, { label: "preserved" });
  const before = state.snapshot();
  rejects(() => state.completeTurn("c-smoke", 2), 409);
  rejects(() => state.mutate(patch(3)), 409);
  rejects(() => state.completeTurn("another-chat", 4), 400);
  rejects(() => state.completeTurn("c-smoke", "4"), 400);
  assert.deepEqual(state.snapshot(), before);
  const detached = state.snapshot();
  detached.metadata.unrelated = "changed";
  assert.deepEqual(state.snapshot(), before);
  assert.equal(state.completeTurn("c-smoke", 4).conversation_revision, 5);
});

test("Settings fixture permits only a canonical downgrade to human approval", () => {
  const payload = { expected_revision: 3, changes: { tools: { action_approval_mode: "ask" } } };
  assert.deepEqual(settingsFixtureMutation(payload, 3), { values: payload.changes, document_revision: 4 });
  rejects(() => settingsFixtureMutation(payload, 4), 409);
  for (const mode of ["full", "delegate", "auto", true, null]) {
    rejects(() => settingsFixtureMutation({ ...payload, changes: { tools: { action_approval_mode: mode } } }, 3), 400);
  }
  rejects(() => settingsFixtureMutation({ ...payload, changes: { tools: { action_approval_mode: "ask", action_approval_control_visible: false } } }, 3), 400);
});

test("completed saved-turn list binds the exact owner receipt and cannot invent an unseen root", () => {
  const query = new URLSearchParams({ conversation_id: "c-smoke" });
  const turn = {
    id: "turn-smoke", conversation_id: "c-smoke", status: "completed", revision: 3,
    result_reference: {
      conversation_id: "c-smoke", conversation_revision: 2,
      user_message_id: "m-saved-user", assistant_message_id: "m-saved-assistant",
      outcome_digest: `sha256:${"f".repeat(64)}`,
    },
  };
  const pending = completedSavedTurnFixtureList(query, null);
  assert.deepEqual(pending, { turns: [] });
  assert.equal(pendingSavedTurnChainForConversation(pending.turns, "c-smoke", turn.id).state, "unsettled");
  const result = completedSavedTurnFixtureList(query, turn);
  assert.deepEqual(result, { turns: [turn] });
  assert.equal(pendingSavedTurnChainForConversation(result.turns, "c-smoke", turn.id).state, "settled");
  assert.equal(pendingSavedTurnChainForConversation(result.turns, "c-smoke", "unseen-turn").state, "unsettled");
  result.turns[0].result_reference!.conversation_revision = 99;
  assert.equal(completedSavedTurnFixtureList(query, turn).turns[0].result_reference!.conversation_revision, 2);
  for (const search of ["", "conversation_id=other", "conversation_id=c-smoke&conversation_id=c-smoke", "conversation_id=c-smoke&extra=true"]) {
    rejects(() => completedSavedTurnFixtureList(new URLSearchParams(search), turn), 400);
  }
  rejects(() => completedSavedTurnFixtureList(query, { ...turn, conversation_id: "other" }), 400);
  rejects(() => completedSavedTurnFixtureList(query, { ...turn, status: "running" }), 400);
});

const completedOwner = (): SavedTurn => ({
  id: "turn-smoke", conversation_id: "c-smoke", status: "completed", revision: 3,
  request_id: "saved-turn.turn-smoke", events: [],
  result_reference: {
    conversation_id: "c-smoke", conversation_revision: 2,
    user_message_id: "m-saved-user", assistant_message_id: "m-saved-assistant",
    outcome_digest: `sha256:${"f".repeat(64)}`,
  },
});
const completedEventQuery = () => new URLSearchParams({ turn_id: "turn-smoke", conversation_id: "c-smoke" });

test("completed saved-turn events preserve the committed owner, request and terminal result without inventing events", () => {
  const turn = completedOwner();
  const snapshot = completedSavedTurnFixtureEvents(completedEventQuery(), turn);
  assert.deepEqual(snapshot.turn, completedSavedTurnFixtureList(new URLSearchParams({ conversation_id: "c-smoke" }), turn).turns[0]);
  const identity = {
    turn_id: turn.id, conversation_id: turn.conversation_id, operation_id: turn.id,
    request_id: turn.request_id, turn_revision: turn.revision,
  };
  assert.deepEqual(snapshot, {
    ...identity, status: "completed", turn, events: [],
    terminal: { ...identity, status: "completed", result_reference: turn.result_reference, error: null },
  });
  snapshot.turn.result_reference!.conversation_revision = 99;
  snapshot.terminal!.result_reference!.assistant_message_id = "foreign-message";
  assert.equal(turn.result_reference!.conversation_revision, 2);
  assert.equal(turn.result_reference!.assistant_message_id, "m-saved-assistant");
  assert.deepEqual(completedSavedTurnFixtureEvents(completedEventQuery(), turn).turn, turn);
});

test("completed saved-turn events reject malformed, duplicate and unrelated query identities", () => {
  for (const search of ["", "turn_id=turn-smoke", "conversation_id=c-smoke", "turn_id=&conversation_id=c-smoke",
    "turn_id=../invalid&conversation_id=c-smoke", "turn_id=turn-smoke&conversation_id=foreign",
    "turn_id=turn-smoke&conversation_id=c-smoke&extra=true",
    "turn_id=turn-smoke&turn_id=turn-smoke&conversation_id=c-smoke",
    "turn_id=turn-smoke&conversation_id=c-smoke&conversation_id=c-smoke",
    "turn_id=turn-smoke&other=c-smoke"]) {
    rejects(() => completedSavedTurnFixtureEvents(new URLSearchParams(search), completedOwner()), 400);
  }
});

test("completed saved-turn events cannot manufacture an absent, foreign or incomplete owner receipt", () => {
  const owner = completedOwner();
  for (const turn of [null, { ...owner, id: "foreign-turn" }, { ...owner, conversation_id: "foreign" },
    { ...owner, status: "running" }, { ...owner, revision: 0 }, { ...owner, revision: 1.5 },
    { ...owner, request_id: undefined }, { ...owner, request_id: "invalid request" },
    { ...owner, events: undefined }, { ...owner, events: [{ name: "turn.completed" }] },
    { ...owner, guidance_parent_turn_id: "different-root" }, { ...owner, guidance_source_turn_id: owner.id },
    { ...owner, guidance_id: "unrequested-guidance" }, { ...owner, result_reference: undefined },
    { ...owner, result_reference: { ...owner.result_reference!, conversation_id: "foreign" } },
    { ...owner, result_reference: { ...owner.result_reference!, conversation_revision: 0 } },
    { ...owner, result_reference: { ...owner.result_reference!, assistant_message_id: "" } },
    { ...owner, result_reference: { ...owner.result_reference!, outcome_digest: "unknown" } }]) {
    rejects(() => completedSavedTurnFixtureEvents(completedEventQuery(), turn), 409);
  }
  rejects(() => completedSavedTurnFixtureEvents(new URLSearchParams({ turn_id: "unseen-turn", conversation_id: "c-smoke" }), owner), 409);
  for (const invalidId of [undefined, null, 123, true]) {
    for (const key of ["request_id", "user_message_id", "assistant_message_id"]) {
      const malformedOwner = structuredClone(owner);
      const target = key === "request_id" ? malformedOwner : malformedOwner.result_reference!;
      Object.assign(target, { [key]: invalidId });
      rejects(() => completedSavedTurnFixtureEvents(completedEventQuery(), malformedOwner), 409);
    }
  }
  assert.deepEqual(completedSavedTurnFixtureEvents(completedEventQuery(), owner).turn, owner);
});

test("completed events fixture survives the production canonical transport and unchanged pending-operation guard", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const owner = completedOwner();
  const calls: string[] = [];
  globalThis.fetch = async (url, init) => {
    const request = { url: () => String(url), method: () => init?.method ?? "GET" };
    const query = canonicalRequestQuery(request, "api/chat/turn/events", "GET");
    assert.ok(query);
    assert.equal(init?.cache, "no-store");
    assert.equal(init?.body, undefined);
    calls.push(String(url));
    return new Response(JSON.stringify({ success: true, data: completedSavedTurnFixtureEvents(query, owner) }));
  };
  const snapshot = await api.getSavedTurnEvents(owner.id, owner.conversation_id);
  assert.deepEqual(snapshot.turn, owner);
  assert.deepEqual(snapshot.terminal!.result_reference, owner.result_reference);
  assert.equal(calls.length, 1);
  // The old unhandled-route response is still rejected, rather than ignored.
  globalThis.fetch = async () => new Response(JSON.stringify({ success: true, data: {} }));
  await assert.rejects(api.getSavedTurnEvents(owner.id, owner.conversation_id), /do not match the pending operation/);
});
