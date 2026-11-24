import test from "node:test";
import assert from "node:assert/strict";
import { confirmedChatReferenceCatalog } from "./chatReferenceCatalog";

function snapshot(overrides: Record<string, unknown> = {}) {
  const now = Date.now();
  return {
    kind: "tobkiri.chat.reference.snapshot.v1", profile_id: "local",
    store_revision: 1, project_revision: 2,
    snapshot_time: now, expires_at: now + 600_000,
    references: [{ kind: "chat", id: "real-chat", label: "Shared",
      conversation_ids: ["real-chat"], snapshot_digest: `sha256:${"a".repeat(64)}`,
      member_count: 1, membership_complete: true }],
    next_cursor: null, truncated: false, ...overrides,
  };
}

test("catalog does not discover IDs from text or cross-profile snapshots", () => {
  assert.deepEqual(confirmedChatReferenceCatalog("@chat:made-up", "local"), { references: [], candidates: [] });
  assert.deepEqual(confirmedChatReferenceCatalog(snapshot({ profile_id: "other" }), "local"), { references: [], candidates: [] });
});

test("malformed and expired catalogs never create references", () => {
  for (const raw of [null, [], snapshot({ references: "text" }), snapshot({ unexpected: true }),
    snapshot({ snapshot_time: 1, expires_at: 2 })]) {
    assert.deepEqual(confirmedChatReferenceCatalog(raw, "local"), { references: [], candidates: [] });
  }
});

test("catalog keeps canonical identity and backend label for complete rows", () => {
  const result = confirmedChatReferenceCatalog(snapshot({ next_cursor: "cursor:+/=" }), "local");
  assert.equal(result.references.length, 1);
  assert.equal(result.references[0].id, "real-chat");
  assert.equal(result.references[0].syntax, "@chat:real-chat");
  assert.deepEqual(result.candidates[0], { kind: "chat", id: "real-chat", label: "Shared",
    profileId: "local", syntax: "@chat:real-chat", available: true });
  assert.equal(result.nextCursor, "cursor:+/=");
});

test("incomplete group snapshots cannot become selectable references", () => {
  const result = confirmedChatReferenceCatalog(snapshot({ references: [{ kind: "group", id: "large",
    label: "Large", conversation_ids: [], snapshot_digest: `sha256:${"a".repeat(64)}`, member_count: 257,
    membership_complete: false }] }), "local");
  assert.deepEqual(result.references, []);
  assert.equal(result.candidates.some((candidate) => candidate.available), false);
});

test("catalog cursor follows the backend 96-character boundary", () => {
  assert.equal(confirmedChatReferenceCatalog(snapshot({ next_cursor: "a".repeat(96) }), "local").nextCursor?.length, 96);
  assert.deepEqual(confirmedChatReferenceCatalog(snapshot({ next_cursor: "a".repeat(97) }), "local"), { references: [], candidates: [] });
});

test("catalog preserves canonical 256-character tag IDs and rejects 257", () => {
  const id = `tag:${"a".repeat(252)}`;
  const row = { kind: "group", id, label: "Tag", conversation_ids: [],
    snapshot_digest: `sha256:${"a".repeat(64)}`, member_count: 0, membership_complete: true };
  assert.equal(confirmedChatReferenceCatalog(snapshot({ references: [row] }), "local").references[0]?.id, id);
  assert.deepEqual(confirmedChatReferenceCatalog(snapshot({ references: [{ ...row, id: `${id}a` }] }), "local").references, []);
});
