import assert from "node:assert/strict";
import test from "node:test";

import {
  HISTORY_ORGANIZATION_STORAGE_KEY,
  applyHistoryOrganization,
  loadHistoryOrganization,
  organizationFromGroups,
  parseHistoryOrganization,
  saveHistoryOrganization,
} from "./historyOrganization";

type Chat = { id: string; title: string };
type Group = { id: string; title: string; chats: Chat[]; subGroups: Group[] };

function memoryStorage(initial?: Record<string, string>) {
  const values = new Map(Object.entries(initial ?? {}));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    values,
  };
}

function baseGroups(): Group[] {
  return [
    {
      id: "alpha",
      title: "Alpha",
      chats: [{ id: "a", title: "A" }, { id: "hidden", title: "Hidden" }],
      subGroups: [],
    },
    {
      id: "beta",
      title: "Beta",
      chats: [{ id: "b", title: "B" }],
      subGroups: [],
    },
  ];
}

test("history organization survives refresh and restores membership, nesting, and order", () => {
  const storage = memoryStorage();
  const arranged: Group[] = [{
    id: "beta",
    title: "Beta",
    chats: [{ id: "hidden", title: "Hidden" }, { id: "b", title: "B" }],
    subGroups: [{ id: "alpha", title: "Alpha", chats: [{ id: "a", title: "A" }], subGroups: [] }],
  }];

  const saved = saveHistoryOrganization(arranged, 0, storage, "2026-08-28T00:00:00.000Z");
  assert.equal(saved.ok, true);
  const loaded = loadHistoryOrganization(storage);
  assert.equal(loaded.status, "ready");
  if (loaded.status !== "ready") return;

  const restored = applyHistoryOrganization(baseGroups(), loaded.organization);
  assert.deepEqual(restored.map((group) => group.id), ["beta"]);
  assert.deepEqual(restored[0]?.chats.map((chat) => chat.id), ["hidden", "b"]);
  assert.deepEqual(restored[0]?.subGroups.map((group) => group.id), ["alpha"]);
  assert.deepEqual(restored[0]?.subGroups[0]?.chats.map((chat) => chat.id), ["a"]);
});

test("saving a filtered arrangement keeps hidden chat membership and order", () => {
  const full = baseGroups();
  const before = organizationFromGroups(full, 0, "2026-08-28T00:00:00.000Z");
  const visibleIds = new Set(["a", "b"]);
  const visible = full.map((group) => ({
    ...group,
    chats: group.chats.filter((chat) => visibleIds.has(chat.id)),
  }));
  const movedVisible: Group[] = [
    { ...visible[1]!, chats: [visible[1]!.chats[0]!, visible[0]!.chats[0]!] },
    { ...visible[0]!, chats: [] },
  ];
  const merged: Group[] = movedVisible.map((group) => group.id === "beta"
    ? { ...group, chats: [group.chats[0]!, full[0]!.chats[1]!, group.chats[1]!] }
    : group);
  const after = organizationFromGroups(merged, before.revision + 1, "2026-08-28T00:01:00.000Z");

  assert.equal(after.chatGroups.hidden, "beta");
  assert.deepEqual(after.chatOrder.beta, ["b", "hidden", "a"]);
});

test("history storage fails closed on concurrent revision changes", () => {
  const storage = memoryStorage();
  assert.equal(saveHistoryOrganization(baseGroups(), 0, storage).ok, true);
  const stale = saveHistoryOrganization(baseGroups().reverse(), 0, storage);
  assert.deepEqual(stale, {
    ok: false,
    reason: "conflict",
    message: "History organization changed in another window.",
  });
});

test("history storage reports write failures without claiming saved", () => {
  const storage = {
    getItem: () => null,
    setItem: () => { throw new Error("quota"); },
    removeItem: () => undefined,
  };
  assert.deepEqual(saveHistoryOrganization(baseGroups(), 0, storage), {
    ok: false,
    reason: "unavailable",
    message: "History organization could not be saved.",
  });
});

test("history storage preserves corrupt raw data for recovery export", () => {
  const storage = memoryStorage({ [HISTORY_ORGANIZATION_STORAGE_KEY]: "{not-json" });
  const loaded = loadHistoryOrganization(storage);
  assert.equal(loaded.status, "corrupt");
  if (loaded.status !== "corrupt") return;
  assert.equal(loaded.raw, "{not-json");
  assert.throws(() => parseHistoryOrganization(loaded.raw));
});

test("organization apply rejects stored parent cycles and keeps every group reachable", () => {
  const cyclic = organizationFromGroups(baseGroups(), 1);
  cyclic.groupChildren = { __root__: [], alpha: ["beta"], beta: ["alpha"] };
  const restored = applyHistoryOrganization(baseGroups(), cyclic);
  const ids: string[] = [];
  const visit = (groups: Group[]) => groups.forEach((group) => {
    ids.push(group.id);
    visit(group.subGroups);
  });
  visit(restored);
  assert.deepEqual(ids.sort(), ["alpha", "beta"]);
});


test("Profile history organization survives restart without mixing identical conversation ids", () => {
  const stored = new Map<string, string>();
  const storage = { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, raw: string) => { stored.set(key, raw); }, removeItem: (key: string) => { stored.delete(key); } };
  const groupsA = [{ id: "project-a", chats: [{ id: "same-chat" }], subGroups: [] }];
  const groupsB = [{ id: "project-b", chats: [{ id: "same-chat" }], subGroups: [] }];
  assert.equal(saveHistoryOrganization(groupsA, 0, storage, "now", "profile/a").ok, true);
  assert.equal(loadHistoryOrganization(storage, "profile-b").status, "empty");
  assert.equal(saveHistoryOrganization(groupsB, 0, storage, "now", "profile-b").ok, true);
  const restartedA = loadHistoryOrganization(storage, "profile/a");
  const restartedB = loadHistoryOrganization(storage, "profile-b");
  assert.equal(restartedA.status, "ready");
  assert.equal(restartedB.status, "ready");
  if (restartedA.status === "ready" && restartedB.status === "ready") {
    assert.equal(restartedA.organization.chatGroups["same-chat"], "project-a");
    assert.equal(restartedB.organization.chatGroups["same-chat"], "project-b");
  }
});

test("History maps preserve prototype-shaped ids as data and ignore inherited membership", () => {
  const groups = [{ id: "constructor", chats: [{ id: "__proto__" }], subGroups: [] }];
  const organization = organizationFromGroups(groups, 1);
  const parsed = parseHistoryOrganization(JSON.stringify(organization));
  assert.equal(parsed.chatGroups["__proto__"], "constructor");
  assert.equal(parsed.chatGroups["toString"], undefined);
  assert.deepEqual(applyHistoryOrganization(groups, parsed), groups);
});
