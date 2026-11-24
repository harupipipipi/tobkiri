import assert from "node:assert/strict";
import test from "node:test";
import {
  mergeThreadTicketTurn, newestThreadTurn, readConversationThread, readThreadEventTurn, readThreadTurn, threadRequestPayload,
  threadTurnFailureNotice, threadTurnIsActive, threadTurnIsSaved,
} from "./conversationThreadState";
import type { ConversationThreadDefinition } from "./catalogViewRegistry";

const operation = { contribution_id: "fixture.send", contract_id: "fixture.turn.v1", operation_id: "fixture.send" };
const descriptor: ConversationThreadDefinition = {
  conversation_path: "thread.conversation", messages_path: "thread.messages",
  pending_turn_path: "thread.pending_turn", model_reference_path: "thread.context.model_reference",
  send: { operation, input: { operation: "send" }, turn_id_key: "turn_id", content_key: "content",
    context_bindings: { parent_conversation_id: "conversation_id" },
    source_bindings: { conversation_id: "conversation_id", expected_child_revision: "revision", expected_parent_revision: "parent_revision" } },
};
const turn = { id: "turn-1", conversation_id: "child", status: "running", revision: 1 };
const snapshot = () => ({ conversation_id: "child", revision: 3, parent_revision: 7,
  thread: { conversation: { id: "child", conversation_revision: 3, title: "Child", model: "parent-model" },
    messages: [{ id: "user-1", role: "user", content: "Input", created_at: 1000, metadata: { turn_id: "turn-1" } },
      { id: "assistant-1", role: "assistant", content: "Saved answer", created_at: 2000, finish_reason: "stop", metadata: { turn_id: "turn-1" } }],
    pending_turn: turn, context: { model_reference: { display_name: "Inherited model" } } } });

test("thread projection binds every message and turn to the canonical child", () => {
  const source = snapshot();
  const parsed = readConversationThread(source, descriptor)!;
  assert.equal(parsed.conversation.id, "child");
  assert.equal(parsed.modelLabel, "Inherited model");
  assert.equal(parsed.messages[0].conversation_id, "child");
  assert.equal("conversation_id" in source.thread.messages[0], false);
  assert.equal(readConversationThread({ ...source, thread: { ...source.thread,
    messages: [{ ...source.thread.messages[0], conversation_id: "parent" }] } }, descriptor), null);
  assert.equal(readConversationThread({ ...source, thread: { ...source.thread,
    messages: [source.thread.messages[0], source.thread.messages[0]] } }, descriptor), null);
  assert.equal(readConversationThread({ ...source, thread: { ...source.thread,
    pending_turn: { ...turn, conversation_id: "parent" } } }, descriptor), null);
  assert.equal(readConversationThread({ ...source, thread: { ...source.thread,
    conversation: { ...source.thread.conversation, conversation_revision: 0 } } }, descriptor), null);
});

test("send payload preserves exact text and captured owner revisions without mutating source", () => {
  const source = snapshot();
  const before = JSON.stringify(source);
  assert.deepEqual(threadRequestPayload(descriptor.send, source, { conversation_id: "parent" }, "turn-2", "  日本語\ninput  "), {
    operation: "send", parent_conversation_id: "parent", conversation_id: "child",
    expected_child_revision: 3, expected_parent_revision: 7, turn_id: "turn-2",
    content: [{ type: "text", text: "  日本語\ninput  " }],
  });
  assert.equal(JSON.stringify(source), before);
  assert.equal(threadRequestPayload(descriptor.send, source, {}, "turn-2", "input"), null);
  assert.equal(threadRequestPayload(descriptor.send, { ...source, revision: undefined }, { conversation_id: "parent" }, "turn-2", "input"), null);
  assert.equal(threadRequestPayload(descriptor.send, source, { conversation_id: "parent" }, "bad id", "input"), null);
});

test("text limits count UTF-8 bytes and hidden authority bindings fail closed", () => {
  const source = snapshot();
  const context = { conversation_id: "parent" };
  assert.ok(threadRequestPayload(descriptor.send, source, context, "turn-2", "x".repeat(16384)));
  assert.equal(threadRequestPayload(descriptor.send, source, context, "turn-2", "日".repeat(5462)), null);
  assert.equal(threadRequestPayload(descriptor.send, source, context, "turn-2", " \n "), null);
  assert.equal(threadRequestPayload({ ...descriptor.send, input: { turn_id: "forged" } }, source, context, "turn-2", "text"), null);
  assert.equal(threadRequestPayload({ ...descriptor.send, input: { content: "forged" } }, source, context, "turn-2", "text"), null);
  assert.equal(threadRequestPayload({ ...descriptor.send, source_bindings: { context: "hidden" } },
    { ...source, hidden: { nested: { model: "override" } } }, context, "turn-2", "text"), null);
  assert.equal(threadRequestPayload({ operation, turn_id_key: "turn_id" }, source, {}, "turn-2", "unexpected content"), null);
});

test("events and buffered results cannot replace another child or ticket", () => {
  assert.deepEqual(readThreadEventTurn(turn, "child", "turn-1"), turn);
  assert.deepEqual(readThreadEventTurn({ status: "existing", turn }, "child", "turn-1"), turn);
  for (const result of [{ ...turn, conversation_id: "parent" }, { ...turn, id: "other" },
    { ...turn, turn_id: "other" }, { conversation_id: "parent", turn },
    { turn_id: "other", turn }, { ...turn, revision: 0 }]) {
    assert.equal(readThreadEventTurn(result, "child", "turn-1"), null);
  }
  assert.equal(readThreadTurn({ ...turn, status: "success" }, "child"), null);
});

test("terminal labels alone cannot clear a draft before the saved receipt and history agree", () => {
  const parsed = readConversationThread(snapshot(), descriptor)!;
  const completed = { ...turn, status: "completed" };
  assert.equal(threadTurnIsSaved(completed, parsed, "turn-1"), false);
  const saved = { ...completed, result_reference: { conversation_id: "child", conversation_revision: 3,
    user_message_id: "user-1", assistant_message_id: "assistant-1", outcome_digest: `sha256:${"a".repeat(64)}` } };
  assert.equal(threadTurnIsSaved(saved, parsed, "turn-1"), true);
  assert.equal(threadTurnIsSaved(saved, { ...parsed, conversation: { ...parsed.conversation, conversation_revision: 2 } }, "turn-1"), false);
  assert.equal(threadTurnIsSaved({ ...saved, result_reference: { ...saved.result_reference, outcome_digest: "forged" } }, parsed, "turn-1"), false);
  assert.equal(threadTurnIsSaved(saved, parsed, "other"), false);
  assert.equal(threadTurnIsActive(turn), true);
  assert.equal(threadTurnIsActive(saved), false);
  assert.match(threadTurnFailureNotice({ ...turn, status: "cancelled" })!, /停止を確認/);
  assert.match(threadTurnFailureNotice({ ...turn, status: "failed" })!, /完了できません/);
  assert.notEqual(threadTurnFailureNotice({ ...turn, status: "cancelled" }), threadTurnFailureNotice({ ...turn, status: "failed" }));
});

test("late reads cannot roll a ticket backward or resolve contradictory owner revisions", () => {
  const completed = { ...turn, status: "completed", revision: 3 };
  assert.deepEqual(newestThreadTurn(completed, turn), completed);
  assert.deepEqual(newestThreadTurn(turn, completed), completed);
  assert.equal(newestThreadTurn(completed, { ...completed, status: "failed" }), null);
  assert.equal(newestThreadTurn(completed, { ...completed, conversation_id: "parent" }), null);
  const ticket = { turnId: "turn-1", conversationId: "child", draft: "retained", source: {}, turn: completed };
  const conflict = mergeThreadTicketTurn(ticket, { ...completed, status: "failed" });
  assert.equal(conflict.contradictoryRevision, 3);
  assert.equal(mergeThreadTicketTurn(conflict, turn).turn?.revision, 3);
  assert.equal(mergeThreadTicketTurn(conflict, turn).contradictoryRevision, 3);
  assert.equal(mergeThreadTicketTurn(conflict, { ...completed, revision: 4 }).contradictoryRevision, undefined);
  assert.equal(mergeThreadTicketTurn(ticket, { ...turn, id: "other" }), ticket);
});
