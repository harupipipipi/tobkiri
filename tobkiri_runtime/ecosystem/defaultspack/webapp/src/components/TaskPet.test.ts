import assert from "node:assert/strict";
import test from "node:test";
import type { SavedTurnEventSnapshot } from "../lib/api";
import { TaskPetCompletionObserver, loadTaskPetPreference, saveTaskPetPreference, shouldSendDesktopNotification, taskPetBoundViewModel, taskPetViewModel, type TaskPetScope } from "../lib/taskPet";

const scope: TaskPetScope = { profileId: "profile-a", conversationId: "conversation-a", turnId: "turn-a" };
function snapshot(status = "running", revision = 1): SavedTurnEventSnapshot {
  const identity = { turn_id: scope.turnId, conversation_id: scope.conversationId, operation_id: scope.turnId, request_id: "request-a" };
  return { ...identity, status, turn_revision: revision, events: [], turn: { id: scope.turnId, conversation_id: scope.conversationId, status, revision }, terminal: ["completed", "failed", "cancelled"].includes(status) ? { ...identity, turn_revision: revision, status, result_reference: status === "completed" ? { conversation_id: scope.conversationId, conversation_revision: 2, user_message_id: "user-a", assistant_message_id: "assistant-a", outcome_digest: `sha256:${"a".repeat(64)}` } : undefined } : null };
}

test("TaskPet completion requires an exact authoritative terminal receipt", () => {
  assert.equal(taskPetViewModel(scope, snapshot("running")).mood, "thinking");
  assert.equal(taskPetViewModel(scope, snapshot("waiting")).mood, "waiting");
  assert.equal(taskPetViewModel(scope, snapshot("completed", 2)).mood, "completed");
  assert.equal(taskPetViewModel(scope, snapshot("cancelled", 2)).mood, "cancelled");
  const value = snapshot("completed", 2); delete value.terminal!.result_reference;
  assert.equal(taskPetViewModel(scope, value).mood, "idle");
});
test("TaskPet rejects crossed conversation, turn, request and revision bindings", () => {
  for (const mutate of [
    (s: SavedTurnEventSnapshot) => { s.conversation_id = "other"; },
    (s: SavedTurnEventSnapshot) => { s.terminal!.turn_id = "other"; },
    (s: SavedTurnEventSnapshot) => { s.terminal!.request_id = "other"; },
    (s: SavedTurnEventSnapshot) => { s.terminal!.turn_revision = 1; },
    (s: SavedTurnEventSnapshot) => { s.turn.revision = 1; },
    (s: SavedTurnEventSnapshot) => { s.status = "running"; },
  ]) { const value = snapshot("completed", 2); mutate(value); assert.equal(taskPetViewModel(scope, value).mood, "idle"); }
});
test("idle, cancellation, historical completion and Profile changes cannot notify", () => {
  const observer = new TaskPetCompletionObserver();
  assert.equal(observer.observe(taskPetViewModel(scope, snapshot("completed", 2))), null);
  observer.observe(taskPetViewModel(scope, snapshot("running")));
  assert.equal(observer.observe(taskPetViewModel(scope, null)), null);
  assert.equal(observer.observe(taskPetViewModel(scope, snapshot("completed", 2))), null);
  observer.observe(taskPetViewModel(scope, snapshot("running")));
  assert.equal(observer.observe(taskPetViewModel(scope, snapshot("cancelled", 2))), null);
  observer.observe(taskPetViewModel(scope, snapshot("running")));
  assert.equal(observer.observe(taskPetViewModel({ ...scope, profileId: "profile-b" }, snapshot("completed", 2))), null);
});
test("completion notifies once and ignores stale terminal revisions", () => {
  const observer = new TaskPetCompletionObserver();
  observer.observe(taskPetViewModel(scope, snapshot("running", 3)));
  assert.equal(observer.observe(taskPetViewModel(scope, snapshot("completed", 2))), null);
  assert.equal(observer.observe(taskPetViewModel(scope, snapshot("completed", 4))), "completed");
  observer.observe(taskPetViewModel(scope, snapshot("running", 3)));
  assert.equal(observer.observe(taskPetViewModel(scope, snapshot("completed", 4))), null);
});
test("TaskPet never displays backend exception or secret payloads", () => {
  const value = snapshot("failed", 2); value.terminal!.error = "secret-token private-network traceback";
  const view = taskPetViewModel(scope, value);
  assert.equal(view.mood, "error");
  assert.doesNotMatch(JSON.stringify(view), /secret-token|private-network|traceback/);
});
test("notifications require opt-in and stop during approvals or visible tabs", () => {
  assert.equal(shouldSendDesktopNotification("granted", "hidden", false, false), false);
  assert.equal(shouldSendDesktopNotification("granted", "hidden", true, true), false);
  assert.equal(shouldSendDesktopNotification("default", "hidden", true, false), false);
  assert.equal(shouldSendDesktopNotification("granted", "visible", true, false), false);
  assert.equal(shouldSendDesktopNotification("granted", "hidden", true, false), true);
});
test("preferences are Profile-local and storage errors never claim success", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  assert.equal(loadTaskPetPreference(storage, "profile-a", "notifications"), false);
  assert.equal(saveTaskPetPreference(storage, "profile-a", "notifications", true), true);
  assert.equal(loadTaskPetPreference(storage, "profile-a", "notifications"), true);
  assert.equal(loadTaskPetPreference(storage, "profile-b", "notifications"), false);
  assert.equal(saveTaskPetPreference(null, "profile-a", "enabled", false), false);
  assert.equal(saveTaskPetPreference({ ...storage, setItem() { throw new Error("quota"); } }, "profile-a", "notifications", true), false);
});

test("same turn identifiers from another Profile remain unavailable", () => {
  assert.equal(taskPetBoundViewModel("profile-a", scope, "profile-b", snapshot("completed", 2)).mood, "idle");
  assert.equal(taskPetBoundViewModel("profile-a", scope, "profile-a", snapshot("completed", 2)).mood, "completed");
});

test("restart preserves opt-in but a historical terminal cannot trigger a new notification", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  saveTaskPetPreference(storage, "profile-a", "notifications", true);
  const restarted = new TaskPetCompletionObserver();
  assert.equal(loadTaskPetPreference(storage, "profile-a", "notifications"), true);
  assert.equal(restarted.observe(taskPetViewModel(scope, snapshot("completed", 2))), null);
  restarted.observe(taskPetViewModel(scope, snapshot("waiting", 3)));
  assert.equal(restarted.observe(taskPetViewModel(scope, snapshot("completed", 4))), "completed");
});

test("TaskPet actual stylesheet disables motion for reduced-motion preference", async () => {
  const { readFile } = await import("node:fs/promises");
  const css = await readFile(new URL("./TaskPet.css", import.meta.url), "utf8");
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[^]*\.task-pet-character,\s*\.task-pet-spinner\s*\{\s*animation:\s*none\s*!important/);
});
