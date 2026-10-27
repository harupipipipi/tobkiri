import assert from "node:assert/strict";
import test from "node:test";
import { savedChatReferenceMentions } from "./savedChatReferenceMentions";
const row = { kind: "chat", id: "one", label: "Saved chat", conversation_ids: ["one"], snapshot_digest: `sha256:${"a".repeat(64)}`, member_count: 1, membership_complete: true };
test("stored rows project display identity without profile or authority fields", () => {
  assert.deepEqual(savedChatReferenceMentions([row, row]), [{ kind: "chat", id: "one", label: "Saved chat", syntax: "@chat:one", memberIds: ["one"] }]);
  assert.equal(savedChatReferenceMentions({ references: [row] }).length, 0);
  assert.equal(savedChatReferenceMentions([{ ...row, expires_at: 0 }]).length, 1);
  assert.deepEqual(savedChatReferenceMentions([{ ...row, kind: "group", conversation_ids: [], member_count: 0 }])[0]?.memberIds, []);
});
test("malformed incomplete oversized and inconsistent snapshots cannot project", () => {
  const malformed = [ { ...row, id: "../one" }, { ...row, label: "x".repeat(257) }, { ...row, label: "bad\nlabel" }, { ...row, snapshot_digest: "short" }, { ...row, membership_complete: false }, { ...row, member_count: 2 }, { ...row, conversation_ids: ["other"] }, { ...row, conversation_ids: ["one", "one"], member_count: 2 }, { ...row, kind: "group", conversation_ids: Array.from({ length: 257 }, (_, i) => `c${i}`), member_count: 257 } ];
  for (const candidate of malformed) assert.deepEqual(savedChatReferenceMentions([candidate]), []);
  assert.deepEqual(savedChatReferenceMentions(Array.from({ length: 17 }, () => row)), []);
});
