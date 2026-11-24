import assert from "node:assert/strict";
import test from "node:test";
import { HISTORY_REFERENCE_SCHEMA, HISTORY_REFERENCE_MAX_BYTES, HISTORY_REFERENCE_MAX_MEMBERS,
  buildHistoryChatReference, buildHistoryGroupReference, decodeHistoryReferenceDrop,
  confirmResolvedHistoryReference as confirmHistoryReference, historyReferenceMention,
  canonicalHistoryTagGroupId, type HistoryReferenceDragPayload, type ConfirmedHistoryReference } from "./historyReferences";
const FIXTURE_NOW = 1_700_000_000_000;
const confirmResolvedHistoryReference: typeof confirmHistoryReference = (candidate, resolved, nowMs = FIXTURE_NOW) =>
  confirmHistoryReference(candidate, resolved, nowMs);
const chat = (id = "chat:uuid-1", label = "Same label"): HistoryReferenceDragPayload => ({
  schema: HISTORY_REFERENCE_SCHEMA, kind: "chat", profile_id: "profile-1", id, label });
const snapshot = (candidate: HistoryReferenceDragPayload, ids = candidate.kind === "chat" ? [candidate.id] : ["current"]) => ({
  kind: "tobkiri.chat.reference.snapshot.v1", profile_id: candidate.profile_id,
  store_revision: 1, project_revision: 1, next_cursor: null, truncated: false, snapshot_time: FIXTURE_NOW, expires_at: FIXTURE_NOW + 600_000,
  references: [{ kind: candidate.kind, id: candidate.id, label: "Server label",
    conversation_ids: ids, member_count: ids.length, membership_complete: true, snapshot_digest: `sha256:${"a".repeat(64)}` }],
});
const decode = (value: unknown) => decodeHistoryReferenceDrop(JSON.stringify(value), "profile-1");

test("strict decoder rejects primitives coercion authority hints and unknown schema", () => {
  for (const value of [null, true, 42, [], "chat", {}, { ...chat(), schema: "v2" },
    { ...chat(), id: 42 }, { ...chat(), label: {} }, { ...chat(), profile_id: [] },
    { ...chat(), approved: true }, { ...chat(), member_ids: [] }]) assert.equal(decode(value), null);
  for (const raw of ["", "{", null, {}, 123]) assert.equal(decodeHistoryReferenceDrop(raw, "profile-1"), null);
});

test("canonical bounded IDs and exact profile are required", () => {
  assert.equal(decode(chat())?.id, "chat:uuid-1");
  for (const id of ["../chat", "/chat", "a b", "a\n", "a".repeat(257), "<chat>", ""]) assert.equal(decode({ ...chat(), id }), null);
  for (const profile of ["profile-2", "../profile-1", "profile-1\n", "profile-1/"]) assert.equal(decode({ ...chat(), profile_id: profile }), null);
  assert.equal(decodeHistoryReferenceDrop(JSON.stringify(chat()), ""), null);
});

test("labels strip HTML and controls without changing identity", () => {
  const candidate = decode(chat("chat-1", "<b>Chat</b>\u0000\u202e label"))!;
  assert.equal(candidate.label, "Chat label");
  assert.equal(candidate.id, "chat-1");
  assert.equal(decode(chat("chat-1", ""))?.label, "chat-1");
  assert.equal(decode(chat("chat-1", "x".repeat(257))), null);
});

test("raw UTF8 bytes and member count are bounded without truncation", () => {
  assert.equal(decodeHistoryReferenceDrop(" ".repeat(HISTORY_REFERENCE_MAX_BYTES + 1), "profile-1"), null);
  assert.equal(decodeHistoryReferenceDrop("界".repeat(HISTORY_REFERENCE_MAX_BYTES / 2), "profile-1"), null);
  assert.equal(decode({ ...chat(), kind: "group", member_ids: Array.from({ length: HISTORY_REFERENCE_MAX_MEMBERS + 1 }, (_, i) => `c-${i}`) }), null);
  assert.equal(decode({ ...chat(), kind: "group", member_ids: ["valid", {}] }), null);
  assert.equal(decode({ ...chat(), kind: "group" }), null);
});

test("builders capture profile and all unique recursive members", () => {
  const group = { id: "display-group", sourceGroupId: "group:stable", title: "Group", chats: [{ id: "chat-1" }],
    subGroups: [{ id: "subgroup", title: "Sub", chats: [{ id: "chat-1" }, { id: "chat-2" }], subGroups: [] }] };
  const snapshot = buildHistoryGroupReference(group, "profile-1")!;
  assert.equal(snapshot.id, "group:stable");
  assert.deepEqual(snapshot.member_ids, ["chat-1", "chat-2"]);
  assert.equal(snapshot.schema, HISTORY_REFERENCE_SCHEMA);
  assert.equal(buildHistoryChatReference({ id: "chat-1", title: "Chat" }, "../bad"), null);
  assert.equal(buildHistoryGroupReference({ ...group, chats: Array.from({ length: HISTORY_REFERENCE_MAX_MEMBERS + 1 }, (_, i) => ({ id: `chat-${i}` })) }, "profile-1"), null);
});

test("authenticated envelope supplies label and membership; mismatches and denial fail", () => {
  const candidate: HistoryReferenceDragPayload = { ...chat("group-1", "Forged display"), kind: "group", member_ids: ["removed", "forged"] };
  const good = snapshot(candidate);
  for (const resolved of [null, 42, "denied", [], { error: "denied" },
    { ...good, kind: "wrong" }, { ...good, profile_id: "other" },
    { ...good, references: [] }, { ...good, references: [...good.references, ...good.references] },
    { ...good, references: [{ ...good.references[0], id: "other" }] },
    { ...good, references: [{ ...good.references[0], kind: "chat" }] },
    { ...good, references: [{ ...good.references[0], conversation_ids: ["../escape"] }] },
    { ...good, references: [{ ...good.references[0], conversation_ids: ["z", "a"] }] },
    { ...good, references: [{ ...good.references[0], conversation_ids: ["a", "a"] }] },
    { ...good, verified: true }, { ...good, store_revision: null },
    { ...good, references: [{ ...good.references[0], snapshot_digest: "" }] }]) {
    assert.equal(confirmResolvedHistoryReference(candidate, resolved), null);
  }
  assert.equal(confirmResolvedHistoryReference(null as unknown as HistoryReferenceDragPayload, good), null);
  const confirmed = confirmResolvedHistoryReference(candidate, good)!;
  assert.deepEqual(confirmed.member_ids, ["current"]);
  assert.equal(confirmed.label, "Server label");
  assert(Object.isFrozen(confirmed));
  assert(Object.isFrozen(confirmed.member_ids));
  const mention = historyReferenceMention(confirmed);
  assert.equal(mention.syntax, "@group:group-1");
  assert.equal(mention.widget.type, "group");
  assert.deepEqual(mention.reference.memberIds, ["current"]);
  assert.equal(mention.reference.profileId, "profile-1");
  assert.deepEqual((mention.widget.metadata?.history_reference as HistoryReferenceDragPayload).member_ids, ["current"]);
});

test("chat envelopes resolve exactly their own conversation; empty groups stay empty", () => {
  const candidate = chat();
  assert.equal(confirmResolvedHistoryReference(candidate, snapshot(candidate, [])), null);
  assert.equal(confirmResolvedHistoryReference(candidate, snapshot(candidate, ["other"])), null);
  const group: HistoryReferenceDragPayload = { ...chat("group-1"), kind: "group", member_ids: ["removed"] };
  assert.deepEqual(confirmResolvedHistoryReference(group, snapshot(group, []))?.member_ids, []);
});

test("same labels retain distinct ID syntax and raw candidates cannot build widgets", () => {
  const mentions = ["chat-1", "chat-2"].map((id) => {
    const candidate = chat(id);
    return historyReferenceMention(confirmResolvedHistoryReference(candidate, snapshot(candidate))!);
  });
  assert.deepEqual(mentions.map((m) => m.syntax), ["@chat:chat-1", "@chat:chat-2"]);
  assert.deepEqual(mentions.map((m) => m.reference.label), ["Server label", "Server label"]);
  assert.equal(mentions[0].widget.type, "conversation");
  assert.throws(() => historyReferenceMention(chat() as ConfirmedHistoryReference), /unresolved/);
});


test("profile capture accepts canonical Unicode names but rejects oversized UTF8 and traversal", () => {
  assert.equal(buildHistoryChatReference({ id: "chat-1", title: "Chat" }, "名前 profile")?.profile_id, "名前 profile");
  for (const profile of ["x".repeat(129), "界".repeat(43), "a..b", " a", "a\\b", "a\u202eb"]) {
    assert.equal(buildHistoryChatReference({ id: "chat-1", title: "Chat" }, profile), null);
  }
});


test("group display snapshots include recursive chat children and reject cycles", () => {
  type Chat = { id: string; children?: Chat[] };
  const nested: Chat = { id: "chat-1", children: [{ id: "chat-2", children: [{ id: "chat-3" }] }] };
  const group = { id: "group-1", title: "Group", chats: [nested, { id: "chat-2" }], subGroups: [] };
  assert.deepEqual(buildHistoryGroupReference(group, "profile-1")?.member_ids, ["chat-1", "chat-2", "chat-3"]);
  nested.children!.push(nested);
  assert.equal(buildHistoryGroupReference(group, "profile-1"), null);
  type Group = { id: string; title: string; chats: Chat[]; subGroups: Group[] };
  const cyclic: Group = { id: "group-1", title: "Group", chats: [], subGroups: [] };
  cyclic.subGroups.push(cyclic);
  assert.equal(buildHistoryGroupReference(cyclic, "profile-1"), null);
});

test("snapshot freshness revisions and digest are strict", () => {
  const candidate = chat();
  const good = { ...snapshot(candidate), snapshot_time: 1_000, expires_at: 601_000 };
  assert(confirmResolvedHistoryReference(candidate, good, 1_000));
  for (const patch of [{ expires_at: 1_000 }, { expires_at: 601_001 }, { snapshot_time: 61_001 },
    { snapshot_time: -1 }, { store_revision: "1" }, { project_revision: 1.5 },
    { expires_at: Number.POSITIVE_INFINITY }, { snapshot_time: null }]) {
    assert.equal(confirmResolvedHistoryReference(candidate, { ...good, ...patch }, 1_000), null);
  }
  assert.equal(confirmResolvedHistoryReference(candidate, good, 601_000), null);
  assert.equal(confirmResolvedHistoryReference(candidate, good, Number.NaN), null);
  for (const digest of ["a".repeat(64), `sha256:${"A".repeat(64)}`, "sha256:bad", ""]) {
    assert.equal(confirmResolvedHistoryReference(candidate, { ...good, references: [{ ...good.references[0], snapshot_digest: digest }] }, 1_000), null);
  }
  const confirmed = confirmResolvedHistoryReference(candidate, good, 1_000)!;
  assert.equal(historyReferenceMention(confirmed).widget.metadata?.source, "composer_at_mention");
});


test("tag bucket codec uses normalized Unicode codepoints and URL-safe UTF8 base64", () => {
  assert.equal(canonicalHistoryTagGroupId("Hello  WORLD"), "group-tag-aGVsbG8td29ybGQ");
  const normalized = "界".repeat(40);
  assert.equal(canonicalHistoryTagGroupId("界".repeat(41)), `group-tag-${Buffer.from(normalized).toString("base64url")}`);
  assert.equal(canonicalHistoryTagGroupId("/Path"), `group-tag-${Buffer.from("/path").toString("base64url")}`);
});

test("backend required member count and completeness match authoritative members", () => {
  const candidate = chat();
  const good = snapshot(candidate);
  for (const count of [undefined, 0, "1", -1, 4097]) {
    assert.equal(confirmResolvedHistoryReference(candidate, { ...good, references: [{ ...good.references[0], member_count: count }] }), null);
  }
  assert(confirmResolvedHistoryReference(candidate, { ...good, references: [{ ...good.references[0], member_count: 1 }] }));
});

test("helper widgets extract real composer semantic metadata after resolution", async () => {
  const { composerMentionMetadataFromWidgets } = await import("./composerWidgets");
  const candidate: HistoryReferenceDragPayload = { ...chat("group-1"), kind: "group", member_ids: ["forged"] };
  const mention = historyReferenceMention(confirmResolvedHistoryReference(candidate, snapshot(candidate, ["chat-1", "chat-2"]))!);
  assert.deepEqual(composerMentionMetadataFromWidgets([mention.widget]), [{
    kind: "group", id: "group-1", profileId: "profile-1", label: "Server label",
    syntax: "@group:group-1", memberIds: ["chat-1", "chat-2"],
  }]);
  const chatCandidate = chat();
  const chatMention = historyReferenceMention(confirmResolvedHistoryReference(chatCandidate, snapshot(chatCandidate))!);
  assert.deepEqual(composerMentionMetadataFromWidgets([chatMention.widget])[0].memberIds, [chatCandidate.id]);
});


test("required pagination and completeness fields match authentic single resolver envelope", () => {
  const candidate = chat();
  const fixture = JSON.parse(JSON.stringify(snapshot(candidate)));
  assert(confirmResolvedHistoryReference(candidate, fixture));
  for (const patch of [{ next_cursor: "next" }, { next_cursor: undefined }, { truncated: true }, { truncated: undefined }]) {
    assert.equal(confirmResolvedHistoryReference(candidate, { ...fixture, ...patch }), null);
  }
  for (const patch of [{ membership_complete: undefined }, { membership_complete: false }, { member_count: undefined }]) {
    assert.equal(confirmResolvedHistoryReference(candidate, { ...fixture, references: [{ ...fixture.references[0], ...patch }] }), null);
  }
  const group: HistoryReferenceDragPayload = { ...chat("group-large"), kind: "group", member_ids: [] };
  const incomplete = snapshot(group, []);
  incomplete.references[0].member_count = 300;
  incomplete.references[0].membership_complete = false;
  assert.equal(confirmResolvedHistoryReference(group, incomplete), null);
});

test("builders fail whole snapshots exceeding byte bounds rather than truncating", () => {
  const group = { id: "group-1", title: "Group", subGroups: [],
    chats: Array.from({ length: 256 }, (_, i) => ({ id: `${i}-` + "a".repeat(250) })) };
  assert.equal(buildHistoryGroupReference(group, "profile-1"), null);
  assert.equal(buildHistoryChatReference({ id: "chat-1", title: "😀".repeat(256) }, "profile-1")?.label, "😀".repeat(256));
  assert.equal(buildHistoryChatReference({ id: "chat-1", title: "😀".repeat(257) }, "profile-1"), null);
});
