import test from "node:test";
import assert from "node:assert/strict";
import { canonicalChatGroupUpdate, commitHistoryMembershipMove, type HistoryMembershipConversation } from "./historyMembership";

test("membership set removes stale aliases and titles without mutating source", () => {
  const nested = { one: true };
  const source = { group_id: "old", groupId: "legacy", group_title: "Old", groupTitle: "Legacy", tag: "keep", nested };
  const prior = { ...source };
  assert.deepEqual(canonicalChatGroupUpdate(source, "group-new"), {
    group_id: "group-new", metadata: { tag: "keep", nested, group_id: "group-new" },
  });
  assert.deepEqual(source, prior);
});

test("membership clear removes both stale aliases and leaves unrelated metadata", () => {
  assert.deepEqual(canonicalChatGroupUpdate({ group_id: "old", groupId: "older", groupTitle: "Old", workspace_id: "workspace-one" }, null), {
    group_id: null, metadata: { workspace_id: "workspace-one" },
  });
  assert.deepEqual(canonicalChatGroupUpdate(undefined, null), { group_id: null, metadata: {} });
  assert.deepEqual(canonicalChatGroupUpdate(null, "group-one"), { group_id: "group-one", metadata: { group_id: "group-one" } });
});

test("membership target requires an exact bounded canonical ASCII identity", () => {
  for (const target of [undefined, 0, false, {}, "", " group", "group ", "../group", "a/b", "日本語", "a".repeat(257)]) {
    assert.throws(() => canonicalChatGroupUpdate({}, target as string), /identity/);
  }
  assert.equal(canonicalChatGroupUpdate({}, "a".repeat(256)).group_id?.length, 256);
  for (const metadata of ["bad", 3, []]) {
    assert.throws(() => canonicalChatGroupUpdate(metadata as unknown as Record<string, unknown>, null), /metadata/);
  }
});

const source: HistoryMembershipConversation = { id: "chat-one", conversation_revision: 3, group_id: "old", metadata: { groupId: "old", group_title: "Old", keep: true } };
const ports = () => ({
  assertCurrent: () => {},
  projects: async () => ({ projects: [{ id: "group-new", title: "Canonical" }] }),
  update: async (id: string, updates: ReturnType<typeof canonicalChatGroupUpdate>, revision: number) => ({ id, ...updates, conversation_revision: revision + 1 }),
});

test("membership commit uses captured CAS and canonical Project title", async () => {
  const result = await commitHistoryMembershipMove(source, "group-new", ports());
  assert.deepEqual(result, { id: "chat-one", conversation_revision: 4, group_id: "group-new", metadata: { keep: true, group_id: "group-new", group_title: "Canonical" } });
  const cleared = await commitHistoryMembershipMove(source, null, { ...ports(), projects: async () => { throw new Error("Should not fetch on clear"); } });
  assert.deepEqual(cleared.metadata, { keep: true });
  assert.equal(cleared.group_id, null);
});

test("unknown Project cannot cause a write", async () => {
  let writes = 0;
  await assert.rejects(commitHistoryMembershipMove(source, "unknown", {
    ...ports(), update: async (...args) => { writes += 1; return ports().update(...args); },
  }), /unavailable/);
  assert.equal(writes, 0);
});

test("Profile fence rejects changes after lookup and during write", async () => {
  for (const phase of ["read", "write"]) {
    let current = true;
    let writes = 0;
    await assert.rejects(commitHistoryMembershipMove(source, "group-new", {
      assertCurrent: () => { if (!current) throw new Error("Stale Profile"); },
      projects: async () => { if (phase === "read") current = false; return ports().projects(); },
      update: async (...args) => { writes += 1; current = false; return ports().update(...args); },
    }), /Stale Profile/);
    assert.equal(writes, phase === "read" ? 0 : 1);
  }
});

test("wrong acknowledgement identity, revision or membership is rejected", async () => {
  for (const patch of [{ id: "other" }, { group_id: "other" }, { conversation_revision: 3 }, { metadata: { groupId: "group-new" } }]) {
    await assert.rejects(commitHistoryMembershipMove(source, "group-new", {
      ...ports(), update: async (...args) => ({ ...await ports().update(...args), ...patch }),
    }), /acknowledgement/);
  }
});
