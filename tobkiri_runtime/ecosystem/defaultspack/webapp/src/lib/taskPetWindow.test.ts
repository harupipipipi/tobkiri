import assert from "node:assert/strict";
import test from "node:test";
import { isTaskPetPresentation, isTaskPetWindowMessage, taskPetWindowName, taskPetWindowUrl } from "./taskPetWindow";

const presentation = {
  profileId: "profile-a",
  enabled: true,
  view: { key: JSON.stringify(["profile-a", "conversation-a", "turn-a"]), revision: 3, mood: "thinking", label: "実行中", title: "作業中", detail: "確認しています" },
};

test("pet window accepts only a bounded profile-bound display projection", () => {
  assert.equal(isTaskPetPresentation(presentation), true);
  assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, key: JSON.stringify(["other", "conversation-a", "turn-a"]) } }), false);
  assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, title: "x".repeat(161) } }), false);
  assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, mood: "failed" } }), false);
  assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, label: "x\u0001" } }), false);
  assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, key: JSON.stringify(["profile-a", 123, "turn-a"]) } }), false);
});

test("browser bridge message validation rejects unknown and cross-profile state", () => {
  assert.equal(isTaskPetWindowMessage({ type: "task-pet-ready" }), true);
  assert.equal(isTaskPetWindowMessage({ type: "task-pet-hidden", profileId: "profile-a" }), true);
  assert.equal(isTaskPetWindowMessage({ type: "task-pet-state", presentation }), true);
  assert.equal(isTaskPetWindowMessage({ type: "task-pet-state", presentation: { ...presentation, view: { ...presentation.view, key: JSON.stringify(["profile-b", "conversation-a", "turn-a"]) } } }), false);
  assert.equal(isTaskPetWindowMessage({ type: "sync_task_pet", presentation }), false);
});

test("ordinary English text is accepted and C0 and C1 controls are rejected", () => {
  assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, title: "Task is useful", detail: "full response" } }), true);
  for (const code of [0, 10, 31, 127, 128, 159]) {
    assert.equal(isTaskPetPresentation({ ...presentation, view: { ...presentation.view, label: String.fromCharCode(code) } }), false);
  }
});

test("browser popup URL and name are stable per same-origin Profile", () => {
  assert.equal(taskPetWindowUrl("https://localhost/p/a/chat?old=1#frag", "profile-a"), "https://localhost/p/profile-a/chat?surface=task-pet");
  assert.equal(taskPetWindowName("profile a"), "tobkiri-task-pet-profile%20a");
});
