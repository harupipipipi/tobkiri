import test from "node:test";
import assert from "node:assert/strict";
import {
  moveSidebarRailItem, normalizeSidebarRailLayout, readSidebarRailLayout,
  saveSidebarRailLayout, sidebarRailOrderChanged, sidebarRailStorageKey,
  sidebarRailPreferenceOrder, SidebarRailPointerGesture,
} from "./sidebarRailLayout";

const items = [{ id: "browser" }, { id: "tools", defaultVisible: false }, { id: "prompts" }];

test("normalization preserves saved order and switches while dropping obsolete and duplicate IDs", () => {
  assert.deepEqual(normalizeSidebarRailLayout({ version: 1,
    order: ["prompts", "removed", "prompts", "starred-tools"],
    visibility: { prompts: false, tools: true, browser: "false", removed: true },
  }, [...items, items[0], { id: "starred-tools" }]), {
    version: 1, order: ["prompts", "browser", "tools"],
    visibility: { browser: true, tools: true, prompts: false },
  });
});

test("unknown versions and malformed values use slot defaults without trusting inherited visibility", () => {
  assert.deepEqual(normalizeSidebarRailLayout({ version: 99, order: ["prompts"] }, items),
    normalizeSidebarRailLayout(null, items));
  const inherited = Object.create({ browser: false });
  assert.equal(normalizeSidebarRailLayout({ version: 1, visibility: inherited }, items).visibility.browser, true);
  assert.deepEqual(normalizeSidebarRailLayout({ version: 1, order: {}, visibility: [] }, items).order,
    ["browser", "tools", "prompts"]);
});

test("profile scopes use different keys including punctuation and unicode", () => {
  assert.notEqual(sidebarRailStorageKey("a:b"), sidebarRailStorageKey("a%3Ab"));
  assert.match(sidebarRailStorageKey("日本語"), /%/);
});

test("storage read reconciles new slots without performing a write", () => {
  let writes = 0;
  const result = readSidebarRailLayout({ getItem: () => JSON.stringify({ version: 1,
    order: ["prompts", "browser"], visibility: { prompts: false },
  }), setItem: () => { writes++; } }, "profile", items);
  assert.deepEqual(result.layout.order, ["prompts", "browser", "tools"]);
  assert.equal(result.layout.visibility.prompts, false);
  assert.equal(writes, 0);
});

test("corrupt or unavailable storage returns defaults and an explicit error", () => {
  for (const getItem of [() => "{", () => { throw new Error("denied"); }]) {
    const result = readSidebarRailLayout({ getItem, setItem: () => {} }, undefined, items);
    assert.ok(result.error);
    assert.deepEqual(result.layout, normalizeSidebarRailLayout(null, items));
  }
});

test("save success is reported after writing and a blocked write reports failure", () => {
  let stored = "";
  const layout = normalizeSidebarRailLayout(null, items);
  assert.deepEqual(saveSidebarRailLayout({ getItem: () => null,
    setItem: (_key, value) => { stored = value; } }, "test", layout), { ok: true });
  assert.deepEqual(JSON.parse(stored), layout);
  const result = saveSidebarRailLayout({ getItem: () => null,
    setItem: () => { throw new Error("quota"); } }, "test", layout);
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.error, /保存できません/);
});

test("reorder crosses category boundaries, retains hidden slots, and identifies no-op or cancel", () => {
  const original = ["browser", "tools", "prompts"];
  const next = moveSidebarRailItem(original, "browser", "prompts");
  assert.deepEqual(next, ["tools", "prompts", "browser"]);
  assert.deepEqual(original, ["browser", "tools", "prompts"]);
  assert.equal(sidebarRailOrderChanged(original, next), true);
  assert.equal(sidebarRailOrderChanged(original, original), false);
  assert.equal(sidebarRailOrderChanged(original, moveSidebarRailItem(original, "missing", "tools")), false);
});


test("primary touch taps retain click behavior while threshold movement captures drag semantics", () => {
  const gesture = new SidebarRailPointerGesture();
  const touch = { pointerId: 21, clientX: 10, clientY: 10, isPrimary: true, button: 0 };
  assert.equal(gesture.begin({ ...touch, isPrimary: false }), false);
  assert.equal(gesture.begin(touch), true);
  assert.equal(gesture.begin({ ...touch, pointerId: 22 }), false);
  assert.equal(gesture.move({ ...touch, pointerId: 22, clientY: 100 }), false);
  assert.equal(gesture.move({ ...touch, clientY: 15 }), false);
  assert.equal(gesture.finish(false, false), false);
  assert.equal(gesture.consumeClick(), false);
  gesture.begin(touch);
  assert.equal(gesture.move({ ...touch, clientY: 16 }), true);
  assert.equal(gesture.finish(false, true), true);
  assert.equal(gesture.consumeClick(), true);
  assert.equal(gesture.consumeClick(), false);
});

test("lost capture, pointer cancellation and unchanged drops suppress drag clicks without persistence", () => {
  for (const [cancelled, changed] of [[true, true], [true, false], [false, false]]) {
    const gesture = new SidebarRailPointerGesture();
    const pointer = { pointerId: 1, clientX: 0, clientY: 0, isPrimary: true, button: 0 };
    gesture.begin(pointer);
    gesture.move({ ...pointer, clientY: 30 });
    assert.equal(gesture.finish(cancelled, changed), false);
    assert.equal(gesture.consumeClick(), true);
    assert.equal(gesture.move({ ...pointer, clientY: 50 }), false);
  }
});

test("hydration preserves unloaded catalogue order and explicit visibility for later slots", () => {
  const saved = { version: 1, order: ["future", "browser", "starred-tools"],
    visibility: { future: false, browser: true, "starred-tools": true } };
  const loaded = readSidebarRailLayout({ getItem: () => JSON.stringify(saved),
    setItem: () => { throw new Error("must not write"); } }, "profile", [{ id: "browser" }]);
  assert.deepEqual(loaded.layout.order, ["browser"]);
  assert.deepEqual(loaded.preferences.order, ["future", "browser"]);
  assert.equal(loaded.preferences.visibility.future, false);
  const later = normalizeSidebarRailLayout(loaded.preferences, [{ id: "browser" }, { id: "future" }]);
  assert.deepEqual(later.order, ["future", "browser"]);
  assert.equal(later.visibility.future, false);
  assert.deepEqual(sidebarRailPreferenceOrder(loaded.preferences, [{ id: "new" }, { id: "browser" }]),
    ["future", "browser", "new"]);
});

test("unsaved defaults follow filter changes without freezing unrelated visibility", () => {
  const preferences = { version: 1 as const, order: ["browser"], visibility: { tools: true } };
  assert.equal(normalizeSidebarRailLayout(preferences, [{ id: "browser", defaultVisible: true }]).visibility.browser, true);
  assert.equal(normalizeSidebarRailLayout(preferences, [{ id: "browser", defaultVisible: false }]).visibility.browser, false);
  assert.equal(normalizeSidebarRailLayout(preferences, [{ id: "tools", defaultVisible: false }]).visibility.tools, true);
});
