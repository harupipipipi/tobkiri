import assert from "node:assert/strict";
import test from "node:test";
import { createRef } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TaskPet, type TaskPetHandle } from "./TaskPet";
import { TaskPetNotificationSettings } from "./TaskPetNotificationSettings";
test("TaskPet publisher renders no always-on launcher markup", () => {
  const controllerRef = createRef<TaskPetHandle>();
  assert.equal(renderToStaticMarkup(<TaskPet profileId="a" scope={null} snapshot={null} snapshotProfileId={null} controllerRef={controllerRef} onError={() => undefined} />), "");
});
test("settings label completion and failure separately from error banners", () => {
  const markup = renderToStaticMarkup(<TaskPetNotificationSettings profileId="a" locale="ja" />);
  assert.match(markup, /ペットのタスク完了通知/); assert.match(markup, /タスク完了・失敗時/);
  assert.match(markup, /エラーバナーの通知とは別/); assert.match(markup, /role="switch" aria-checked="false"/);
});

test("Settings reports denied permission without requesting it or changing opt-in", () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  let requests = 0;
  let writes = 0;
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    Notification: { permission: "denied", requestPermission: () => { requests++; } },
    localStorage: { getItem: () => "true", setItem: () => { writes++; } },
  } });
  try {
    const markup = renderToStaticMarkup(<TaskPetNotificationSettings profileId="a" locale="ja" />);
    assert.match(markup, /通知が拒否されています/);
    assert.match(markup, /\/pet/);
    assert.equal(requests, 0);
    assert.equal(writes, 0);
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else Reflect.deleteProperty(globalThis, "window");
  }
});
