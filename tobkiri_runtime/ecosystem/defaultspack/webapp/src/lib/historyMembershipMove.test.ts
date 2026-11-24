import assert from "node:assert/strict";
import test from "node:test";
import { HistoryMembershipMove, historyMembershipDecision, withoutHistoryChatPlacement, applyCanonicalHistoryOrganization } from "./historyMembershipMove";
import { applyHistoryOrganization, type HistoryOrganizationV1, type HistoryOrganizationGroup } from "../features/history/historyOrganization";

const builtin = { id: "group-today" };
const project = { id: "ui-project", sourceGroupId: "canonical-project", custom: true };

test("only canonical Project membership changes request owner writes", () => {
  assert.deepEqual(historyMembershipDecision(builtin, { id: "group-tags" }), { kind: "blocked" });
  assert.deepEqual(historyMembershipDecision(builtin, builtin), { kind: "local" });
  assert.deepEqual(historyMembershipDecision(builtin, project), { kind: "owner", targetProjectId: "canonical-project" });
  assert.deepEqual(historyMembershipDecision(project, builtin), { kind: "owner", targetProjectId: null });
  assert.deepEqual(historyMembershipDecision(project, { ...project, id: "other-ui-project" }), { kind: "local" });
});

test("membership acknowledgement serializes writes without claiming success early", async () => {
  const move = new HistoryMembershipMove();
  let resolve!: () => void;
  let writes = 0;
  const wait = new Promise<void>((done) => { resolve = done; });
  const first = move.run(() => { writes++; return wait; }, () => true);
  assert.equal(move.pending, true);
  assert.deepEqual(await move.run(async () => { writes++; }, () => true), { kind: "busy" });
  assert.equal(writes, 1);
  resolve();
  assert.deepEqual(await first, { kind: "acknowledged" });
  assert.equal(move.pending, false);
});

test("failure is distinguishable from stale profile or unmounted completion", async () => {
  const move = new HistoryMembershipMove();
  const error = new Error("Owner rejected membership");
  assert.deepEqual(await move.run(async () => { throw error; }, () => true), { kind: "failed", error });
  let resolve!: () => void;
  const wait = new Promise<void>((done) => { resolve = done; });
  const stale = move.run(() => wait, () => true);
  move.invalidate();
  const newer = move.run(async () => undefined, () => true);
  resolve();
  assert.deepEqual(await stale, { kind: "stale" });
  assert.deepEqual(await newer, { kind: "acknowledged" });
  assert.equal(move.pending, false);
});

test("placement cleanup preserves other order and allows owner canonical projection", () => {
  const organization: HistoryOrganizationV1 = { schemaVersion: 1, revision: 3, updatedAt: "now",
    groupChildren: { __root__: ["old", "new"] },
    chatGroups: { moved: "old", retained: "old" },
    chatOrder: { old: ["moved", "retained"], new: [] },
  };
  const sanitized = withoutHistoryChatPlacement(organization, "moved")!;
  assert.deepEqual(organization.chatOrder.old, ["moved", "retained"]);
  assert.deepEqual(sanitized.chatOrder.old, ["retained"]);
  assert.deepEqual(sanitized.chatGroups, { retained: "old" });
  assert.equal(sanitized.groupChildren, organization.groupChildren);
  const projected = applyHistoryOrganization([
    { id: "old", chats: [{ id: "retained" }], subGroups: [] },
    { id: "new", chats: [{ id: "moved" }], subGroups: [] },
  ], sanitized);
  assert.deepEqual(projected.find((group) => group.id === "new")!.chats, [{ id: "moved" }]);
});


test("owner projection defeats stale Project and built-in moves while preserving order and nesting", () => {
  const base: HistoryOrganizationGroup<{ id: string }>[] = [
    { id: "project-a", chats: [{ id: "a1" }, { id: "a2" }], subGroups: [] },
    { id: "project-b", chats: [{ id: "moved" }, { id: "b1" }], subGroups: [] },
    { id: "today", chats: [{ id: "dated" }], subGroups: [] },
    { id: "older", chats: [], subGroups: [] },
  ];
  const organization: HistoryOrganizationV1 = {
    schemaVersion: 1, revision: 4, updatedAt: "now",
    groupChildren: { __root__: ["project-a", "today", "older"], "project-a": ["project-b"] },
    chatGroups: { moved: "project-a", dated: "older", a1: "project-b" },
    chatOrder: { "project-a": ["moved", "a2", "a1"], "project-b": ["a1", "b1", "moved"], older: ["dated"] },
  };
  const projected = applyCanonicalHistoryOrganization(base, organization);
  const a = projected.find((group) => group.id === "project-a")!;
  assert.deepEqual(a.chats.map((chat) => chat.id), ["a2", "a1"]);
  assert.deepEqual(a.subGroups.map((group) => group.id), ["project-b"]);
  assert.deepEqual(a.subGroups[0].chats.map((chat) => chat.id), ["b1", "moved"]);
  assert.deepEqual(projected.find((group) => group.id === "today")!.chats.map((chat) => chat.id), ["dated"]);
  assert.deepEqual(projected.find((group) => group.id === "older")!.chats, []);
  assert.deepEqual(organization.chatGroups, { moved: "project-a", dated: "older", a1: "project-b" });
});
