import test from "node:test";
import assert from "node:assert/strict";

import {
  loadTaskPetEnabled,
  saveTaskPetEnabled,
  shouldSendDesktopNotification,
  taskPetViewModel,
  truncateTaskPetText,
} from "../lib/taskPet";

function preferenceStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  };
}

test("task pet shows the current task and activity while thinking", () => {
  const view = taskPetViewModel(
    "thinking",
    "GitHub リポジトリにタスクを見守るペットを追加する",
    "フロントエンドのテストを実行中",
  );

  assert.equal(view.label, "思考中");
  assert.equal(view.title, "GitHub リポジトリにタスクを見守るペットを追加する");
  assert.equal(view.detail, "フロントエンドのテストを実行中");
});

test("task pet reports completion and clips long task text", () => {
  const task = "長いタスク".repeat(30);
  const view = taskPetViewModel("completed", task);

  assert.equal(view.label, "完了");
  assert.equal(view.title, "できたよ！");
  assert.match(view.detail, /… を完了しました。$/);
  assert.ok(view.detail.length < task.length);
  assert.equal(truncateTaskPetText("  space   is normalized  ", 40), "space is normalized");
});

test("desktop notification is limited to granted background tabs", () => {
  assert.equal(shouldSendDesktopNotification("granted", "hidden"), true);
  assert.equal(shouldSendDesktopNotification("granted", "visible"), false);
  assert.equal(shouldSendDesktopNotification("default", "hidden"), false);
  assert.equal(shouldSendDesktopNotification("denied", "hidden"), false);
});

test("task pet display preference defaults on and survives reload", () => {
  const storage = preferenceStorage();

  assert.equal(loadTaskPetEnabled(storage), true);
  assert.equal(saveTaskPetEnabled(storage, false), true);
  assert.equal(loadTaskPetEnabled(storage), false);
  assert.equal(saveTaskPetEnabled(storage, true), true);
  assert.equal(loadTaskPetEnabled(storage), true);
});

test("task pet keeps the existing display state when preference storage fails", () => {
  const failingStorage = {
    getItem: () => {
      throw new Error("storage unavailable");
    },
    setItem: () => {
      throw new Error("storage unavailable");
    },
  };

  assert.equal(loadTaskPetEnabled(failingStorage), true);
  assert.equal(saveTaskPetEnabled(failingStorage, false), false);
});
