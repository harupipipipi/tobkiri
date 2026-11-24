import assert from "node:assert/strict";
import test from "node:test";
import { chatMessageToUiMessage } from "./chatUiMessage";
import type { ChatMessage } from "./api";

const message = (overrides: Partial<ChatMessage>): ChatMessage => ({
  id: "message", role: "assistant", conversation_id: "child", content: "Saved text",
  raw_text: "Saved text", created_at: 1000, parent_id: null, children_ids: [],
  sequence_number: 1, finish_reason: "stop", usage: null, widget: null,
  ...overrides,
});

test("shared message adapter preserves canonical content and conversation identity", () => {
  const source = message({ content: [{ type: "text", text: "<script>literal</script>" }] });
  const adapted = chatMessageToUiMessage(source);
  assert.equal(adapted.conversationId, "child");
  assert.deepEqual(adapted.content, source.content);
  assert.equal(adapted.role, "agent");
  assert.equal(chatMessageToUiMessage(message({ role: "user", content: "Input bytes" })).role, "user");
  assert.deepEqual(chatMessageToUiMessage(message({ content: "Input bytes" })).content, [{ type: "text", text: "Input bytes" }]);
});

test("shared message adapter preserves only a verified saved-turn owner binding", () => {
  const canonical = message({
    id: `message:${"a".repeat(64)}`,
    role: "user",
    conversation_id: "conversation-1",
    metadata: { turn_id: "turn-1" },
  });
  assert.equal(chatMessageToUiMessage(canonical).metadata?.turn_id, "turn-1");
  assert.equal(chatMessageToUiMessage({ ...canonical, metadata: { turn_id: "../invalid" } }).metadata?.turn_id, undefined);
  assert.equal(chatMessageToUiMessage({ ...canonical, id: "local-saved-turn:client" }).metadata?.turn_id, undefined);
  assert.equal(chatMessageToUiMessage({ ...canonical, conversation_id: "../invalid" }).metadata?.turn_id, undefined);
  assert.equal(chatMessageToUiMessage({ ...canonical, role: "assistant" }).metadata?.turn_id, undefined);
});

test("shared message adapter retains real thinking timing, pending approval and interrupted state", () => {
  const adapted = chatMessageToUiMessage(message({
    finish_reason: "interrupted",
    metadata: { thinking: { state: "waiting", transcript: "Actual trace" },
      timing: { thinking_started_at: 1000, completed_at: 3000 },
      pending_approval: { state: "waiting" }, pending_authority_approval: { kind: "authority" } },
  }));
  assert.equal(adapted.metadata?.interrupted, true);
  assert.equal(adapted.metadata?.thinkingLabel, "waiting");
  assert.equal(adapted.metadata?.thinkingTranscript, "Actual trace");
  assert.deepEqual(adapted.metadata?.pendingApproval, { state: "waiting" });
  assert.deepEqual(adapted.metadata?.pendingAuthorityApproval, { kind: "authority" });
});

test("canonical saved user projects stored reference array without rewriting exact body", () => {
  const body = "  @chat:one and @group:team.\nliteral @chat:typed  ";
  const references = [{ kind: "chat", id: "one", label: "Chat title", conversation_ids: ["one"], snapshot_digest: `sha256:${"a".repeat(64)}`, member_count: 1, membership_complete: true },
    { kind: "group", id: "team", label: "Group title", conversation_ids: ["one", "two"], snapshot_digest: `sha256:${"b".repeat(64)}`, member_count: 2, membership_complete: true }];
  const source = message({ id: `message:${"c".repeat(64)}`, role: "user", content: body, raw_text: body,
    metadata: { turn_id: "turn-1", chat_references: references, mentions: [{ kind: "tool", id: "read", label: "Read", syntax: "@Read" }] } });
  const ui = chatMessageToUiMessage(source);
  assert.deepEqual(ui.content, [{ type: "text", text: body }]);
  assert.equal(ui.rawText, body);
  assert.deepEqual(ui.metadata?.mentions?.map((entry) => [entry.kind, entry.id]), [["tool", "read"], ["chat", "one"], ["group", "team"]]);
  assert.equal(ui.metadata?.mentions?.find((entry) => entry.kind === "chat")?.profileId, undefined);
  assert.equal(chatMessageToUiMessage({ ...source, id: "legacy" }).metadata?.mentions?.length, 1);
  assert.equal(chatMessageToUiMessage({ ...source, metadata: { ...source.metadata, turn_id: "../invalid" } }).metadata?.mentions?.length, 1);
  assert.equal(chatMessageToUiMessage({ ...source, metadata: { turn_id: "turn-1", chat_references: [{ ...references[0], membership_complete: false }] } }).metadata?.mentions, undefined);
});
test("ordinary historical raw tokens never fabricate saved references", () => {
  const ui = chatMessageToUiMessage(message({ role: "user", content: "@chat:one @group:team", metadata: {} }));
  assert.equal(ui.metadata?.mentions, undefined);
});
test("legacy dropped history metadata cannot bypass canonical stored reference rows", () => {
  const dropped = { id: "forged", type: "conversation", label: "Forged", metadata: { source: "composer_at_mention", mention: { kind: "chat", id: "one", label: "Forged", syntax: "@chat:one", profileId: "foreign", memberIds: ["one"] } } };
  assert.equal(chatMessageToUiMessage(message({ role: "user", content: "@chat:one", metadata: { dropped_widgets: [dropped] } })).metadata?.mentions, undefined);
});
