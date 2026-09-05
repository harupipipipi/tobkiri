import assert from "node:assert/strict";
import test from "node:test";
import { installKeyboardOnlyFocusRings } from "./focusModality";

class FakeClassList {
  private values = new Set<string>();

  add(value: string) {
    this.values.add(value);
  }

  remove(value: string) {
    this.values.delete(value);
  }

  contains(value: string) {
    return this.values.has(value);
  }
}

test("focus rings activate on Tab and clear before pointer focus", () => {
  const target = new EventTarget();
  const classList = new FakeClassList();
  const doc = Object.assign(target, {
    documentElement: { classList },
  }) as unknown as Document;
  const cleanup = installKeyboardOnlyFocusRings(doc);

  target.dispatchEvent(Object.assign(new Event("keydown"), { key: "a" }));
  assert.equal(classList.contains("rumi-keyboard-focus"), false);

  target.dispatchEvent(Object.assign(new Event("keydown"), { key: "Tab" }));
  assert.equal(classList.contains("rumi-keyboard-focus"), true);

  target.dispatchEvent(new Event("pointerdown"));
  assert.equal(classList.contains("rumi-keyboard-focus"), false);

  cleanup();
});
