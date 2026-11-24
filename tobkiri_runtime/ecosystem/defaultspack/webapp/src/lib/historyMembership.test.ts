import test from "node:test";
import assert from "node:assert/strict";
import { canonicalChatGroupUpdate, commitHistoryMembershipMove, HistoryMembershipMutations, type HistoryMembershipConversation } from "./historyMembership";
import { ConversationMutationBarrier } from "./conversationMutationBarrier";

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

test("membership lookup and ACK settle before a same-conversation send reads its context", async () => {
  const mutations = new HistoryMembershipMutations();
  let acknowledge!: () => void;
  let current: HistoryMembershipConversation = source;
  const write = new Promise<void>((resolve) => { acknowledge = resolve; }).then(() => {
    current = { ...source, group_id: "group-new", conversation_revision: 4 };
  });
  mutations.record("store", source.id, "group-new", write);
  let reads = 0;
  const send = mutations.settleAndRead("store", source.id, async () => { reads += 1; return current; });
  await Promise.resolve();
  assert.equal(reads, 0);
  acknowledge();
  const result = await send;
  assert.equal(result.conversation.id, source.id);
  assert.equal(result.conversation.group_id, "group-new");
  assert.equal(result.failure, undefined);
});

test("model writes begun during membership lookup settle before the controller owner read", async () => {
  const memberships = new HistoryMembershipMutations();
  const models = new ConversationMutationBarrier();
  let acknowledgeMembership!: () => void;
  let acknowledgeModel!: () => void;
  memberships.record("store", source.id, "group-new", new Promise<void>((resolve) => { acknowledgeMembership = resolve; }));
  let reads = 0;
  const sending = memberships.settleAndRead("store", source.id, async () => {
    await models.wait("store", source.id);
    reads += 1;
    return { ...source, group_id: "group-new", conversation_revision: 5 };
  });
  models.record("store", source.id, new Promise<void>((resolve) => { acknowledgeModel = resolve; }));
  acknowledgeMembership();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(reads, 0);
  acknowledgeModel();
  assert.equal((await sending).conversation.conversation_revision, 5);
  assert.equal(reads, 1);
});

test("failed ACK stops once only after fresh reconciliation, and read failure keeps the intent", async () => {
  const mutations = new HistoryMembershipMutations();
  mutations.record("store", source.id, "group-new", Promise.reject(new Error("CAS conflict")));
  await assert.rejects(mutations.settleAndRead("store", source.id, async () => { throw new Error("owner unavailable"); }), /owner unavailable/);
  const stopped = await mutations.settleAndRead("store", source.id, async () => source);
  assert.match(String(stopped.failure), /CAS conflict/);
  const retry = await mutations.settleAndRead("store", source.id, async () => source);
  assert.equal(retry.failure, undefined);
});

test("late obsolete ACK failure cannot overwrite a newer successful membership operation", async () => {
  const mutations = new HistoryMembershipMutations();
  let rejectOld!: (error: Error) => void;
  const old = mutations.record("store", source.id, "old-target", new Promise((_, reject) => { rejectOld = reject; }));
  const latest = mutations.record("store", source.id, "group-new", Promise.resolve());
  const sending = mutations.settleAndRead("store", source.id, async () => ({ ...source, group_id: "group-new" }));
  await Promise.resolve();
  rejectOld(new Error("obsolete failure"));
  assert.equal(mutations.isCurrent(old), false);
  assert.equal(mutations.isCurrent(latest), true);
  assert.equal((await sending).failure, undefined);
});

test("membership writes added while waiting are drained before the fresh revision read", async () => {
  const mutations = new HistoryMembershipMutations();
  let first!: () => void;
  let second!: () => void;
  mutations.record("store", source.id, "first", new Promise<void>((resolve) => { first = resolve; }));
  let reads = 0;
  const waiting = mutations.settleAndRead("store", source.id, async () => { reads += 1; return { ...source, group_id: "second" }; });
  mutations.record("store", source.id, "second", new Promise<void>((resolve) => { second = resolve; }));
  first(); await Promise.resolve(); await Promise.resolve();
  assert.equal(reads, 0);
  second();
  assert.equal((await waiting).conversation.group_id, "second");
});

test("a membership operation recorded during owner refetch invalidates the older read", async () => {
  const mutations = new HistoryMembershipMutations();
  let reads = 0;
  const result = await mutations.settleAndRead("store", source.id, async () => {
    reads += 1;
    if (reads === 1) {
      mutations.record("store", source.id, "latest", Promise.resolve());
      return source;
    }
    return { ...source, group_id: "latest", conversation_revision: 5 };
  });
  assert.equal(reads, 2);
  assert.equal(result.conversation.group_id, "latest");
});

test("failure reconciliation for an older intent cannot clear a newer successful intent", async () => {
  const mutations = new HistoryMembershipMutations();
  mutations.record("store", source.id, "failed-target", Promise.reject(new Error("failed")));
  let reads = 0;
  const result = await mutations.settleAndRead("store", source.id, async () => {
    reads += 1;
    if (reads === 1) {
      mutations.record("store", source.id, "new-success", Promise.resolve());
      return source;
    }
    return { ...source, group_id: "new-success", conversation_revision: 5 };
  });
  assert.equal(result.failure, undefined);
  assert.equal(result.conversation.group_id, "new-success");
});

test("a mismatched owner read never reconciles away the failed intended membership", async () => {
  const mutations = new HistoryMembershipMutations();
  mutations.record("store", source.id, "intended", Promise.reject(new Error("write failed")));
  await assert.rejects(mutations.settleAndRead("store", source.id, async () => ({ ...source, id: "other" })), /another identity/);
  const result = await mutations.settleAndRead("store", source.id, async () => source);
  assert.match(String(result.failure), /write failed/);
});
