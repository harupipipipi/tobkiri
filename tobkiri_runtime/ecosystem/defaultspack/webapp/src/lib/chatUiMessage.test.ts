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
