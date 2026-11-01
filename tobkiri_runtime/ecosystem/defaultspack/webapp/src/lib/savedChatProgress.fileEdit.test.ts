import test from "node:test";
import assert from "node:assert/strict";
import { mergeSavedChatProgress, type SavedChatProgressWitness } from "./savedChatProgress";
import { buildFileEditTimelineEntries } from "./fileEditTimeline";
import type { ThreadProgressPage } from "../host/threadProgressContract";

const now = 5000;
const digest = "a".repeat(64);
const witness: SavedChatProgressWitness = {
  turnId: "turn-1", conversationId: "chat-1", parentId: `message:${digest}`,
  conversationRevision: 4, inputDigest: `sha256:${digest}`, requestId: "saved-turn.test",
};
const receipt = {
  schema_version: 1, receipt_id: "file-edit:test", file_id: "file:test",
  status: "committed", operation: "create", profile_id: "profile-1",
  workspace_id: "workspace-1", root_id: "opaque-root", frame_id: "opaque-frame",
  path: "demo.txt", previous_path: null, occurred_at_ms: 100, sequence: 1,
  version: "file-edit:test", stats: { status: "available", lines_added: 3, lines_deleted: 0 },
};
const created = { created: true, path: receipt.path, workspace_id: receipt.workspace_id,
  file_edit_receipt: receipt };
const page = (value: unknown, toolId = "coding_file_create", status: "success" | "error" = "success"): ThreadProgressPage => ({
  version: "tobkiri.turn-progress.v1", progress_id: digest, provisional: true,
  binding: { turn_id: witness.turnId, conversation_id: witness.conversationId,
    parent_id: witness.parentId, request_id: witness.requestId,
    conversation_revision: witness.conversationRevision, input_digest: witness.inputDigest,
    ai_input_digest: `sha256:${digest}` },
  events: [
    { cursor: 1, event: { type: "tool_started", tool_id: toolId, tool_call_id: "call-1", arguments: { path: "demo.txt" } } },
    { cursor: 2, event: { type: "tool_completed", tool_id: toolId, tool_call_id: "call-1", status,
      content: JSON.stringify({ status, result: value, error: status === "error" ? "Failed" : null }) } },
  ], cursor: 2, provider_complete: true, expires_at_ms: now + 120000,
  canonical_turn_status: "running",
});

test("validated native progress exposes only the exact explicit create receipt", () => {
  const state = mergeSavedChatProgress(null, page(JSON.stringify(created)), witness, now)!;
  assert.equal(state.activity[0].file_edit_receipt, undefined);
  assert.deepEqual(state.activity[1].file_edit_receipt, receipt);
  assert.equal(state.activity[1].type, "tool_call_completed");
  assert.equal(state.activity[1].phase, "tool_call_completed");
  const entries = buildFileEditTimelineEntries([state.activity[1].file_edit_receipt, receipt]);
  assert.equal(entries.length, 1, "saved/live handover of one mutation must not add another row");
  assert.equal(entries[0].addedLines, 3);
  assert.equal(entries[0].deletedLines, 0);
});

test("read/list/model claims and failed or pending metadata do not become edits", () => {
  for (const [value, toolId, status] of [
    [{ path: "demo.txt", added_lines: 100 }, "coding_file_create", "success"],
    [created, "coding_file_read", "success"], [created, "list_modules", "success"],
    [created, "coding_file_create", "error"],
    [{ ...created, status: "pending" }, "coding_file_create", "success"],
    [{ ...created, error: "failed after save" }, "coding_file_create", "success"],
    [{ ...created, path: "foreign.txt" }, "coding_file_create", "success"],
    [{ ...created, workspace_id: "foreign" }, "coding_file_create", "success"],
    [{ data: created, result: { ...created, path: "foreign.txt" } }, "coding_file_create", "success"],
  ] as const) {
    const state = mergeSavedChatProgress(null, page(value, toolId, status), witness, now)!;
    assert.ok(state);
    assert.equal(state.activity[1].file_edit_receipt, undefined);
  }
});

test("owner mismatch, cursor replay and unmatched completion fail before receipt projection", () => {
  const valid = page(created);
  const state = mergeSavedChatProgress(null, valid, witness, now)!;
  assert.equal(mergeSavedChatProgress(state, valid, witness, now), null);
  assert.equal(mergeSavedChatProgress(null, { ...valid, binding: { ...valid.binding, request_id: "foreign" } }, witness, now), null);
  assert.equal(mergeSavedChatProgress(null, { ...valid, events: [valid.events[1]] }, witness, now), null);
});
