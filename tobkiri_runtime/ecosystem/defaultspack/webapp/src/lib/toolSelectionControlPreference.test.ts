import assert from "node:assert/strict";
import test from "node:test";
import {
  readToolSelectionControlPreference,
  saveToolSelectionControlPreference,
  subscribeToolSelectionControlPreference,
  toolSelectionControlPreferenceKey,
} from "./toolSelectionControlPreference";

function memoryStorage() {
  const values = new Map<string, string>();
  let writes = 0;
  return {
    values,
    get writes() { return writes; },
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { writes++; values.set(key, value); },
  };
}

test("display preference defaults hidden, persists exact Booleans and isolates Profiles", () => {
  const storage = memoryStorage();
  assert.equal(readToolSelectionControlPreference("a", storage), false);
  assert.equal(storage.writes, 0);
  assert.equal(saveToolSelectionControlPreference("a", true, storage), true);
  assert.equal(readToolSelectionControlPreference("a", storage), true);
  assert.equal(readToolSelectionControlPreference("b", storage), false);
  assert.equal(saveToolSelectionControlPreference("b", false, storage), true);
  assert.equal(readToolSelectionControlPreference("a", storage), true);
  assert.equal(storage.getItem(toolSelectionControlPreferenceKey("b")!), "false");
  assert.notEqual(toolSelectionControlPreferenceKey("a:b"), toolSelectionControlPreferenceKey("a%3Ab"));
});

test("hydration never repairs invalid or unavailable display preferences", () => {
  const storage = memoryStorage();
  const key = toolSelectionControlPreferenceKey("a")!;
  for (const value of ["false", "1", "TRUE", "null", '{"visible":true}']) {
    storage.values.set(key, value);
    assert.equal(readToolSelectionControlPreference("a", storage), false);
  }
  for (const profileId of [undefined, "", "  ", "unavailable"]) {
    assert.equal(readToolSelectionControlPreference(profileId, storage), false);
    assert.equal(saveToolSelectionControlPreference(profileId, true, storage), false);
  }
  assert.equal(saveToolSelectionControlPreference("a", "true" as unknown as boolean, storage), false);
  assert.equal(storage.writes, 0);
});

test("storage failures preserve the prior display value and never publish success", () => {
  const storage = memoryStorage();
  saveToolSelectionControlPreference("a", true, storage);
  let changes = 0;
  const stop = subscribeToolSelectionControlPreference("a", () => { changes++; });
  try {
    assert.equal(saveToolSelectionControlPreference("a", false, null), false);
    assert.equal(saveToolSelectionControlPreference("a", false, {
      ...storage, setItem: () => { throw new Error("quota"); },
    }), false);
    assert.equal(changes, 0);
    assert.equal(readToolSelectionControlPreference("a", storage), true);
    assert.equal(readToolSelectionControlPreference("a", {
      ...storage, getItem: () => { throw new Error("blocked"); },
    }), false);
  } finally { stop(); }
});

test("Settings and composers receive only successful writes for their Profile", () => {
  const storage = memoryStorage();
  let settings = 0; let composer = 0; let otherProfile = 0;
  const stops = [
    subscribeToolSelectionControlPreference("a", () => { settings++; }),
    subscribeToolSelectionControlPreference("a", () => { composer++; }),
    subscribeToolSelectionControlPreference("b", () => { otherProfile++; }),
  ];
  try {
    saveToolSelectionControlPreference("a", true, storage);
    assert.deepEqual([settings, composer, otherProfile], [1, 1, 0]);
    assert.equal(readToolSelectionControlPreference("a", storage), true);
    saveToolSelectionControlPreference("a", false, storage);
    assert.deepEqual([settings, composer, otherProfile], [2, 2, 0]);
    assert.equal(readToolSelectionControlPreference("a", storage), false);
  } finally { stops.forEach((stop) => stop()); }
  saveToolSelectionControlPreference("a", true, storage);
  assert.deepEqual([settings, composer, otherProfile], [2, 2, 0]);
});

test("other-tab changes, clearing and subscription cleanup remain Profile-local", () => {
  const callbacks = new Set<(event: { key: string | null }) => void>();
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    addEventListener: (_type: string, callback: (event: { key: string | null }) => void) => callbacks.add(callback),
    removeEventListener: (_type: string, callback: (event: { key: string | null }) => void) => callbacks.delete(callback),
  } });
  try {
    let updates = 0;
    const stop = subscribeToolSelectionControlPreference("a", () => { updates++; });
    const emit = (key: string | null) => callbacks.forEach((callback) => callback({ key }));
    emit(toolSelectionControlPreferenceKey("b"));
    assert.equal(updates, 0);
    emit(toolSelectionControlPreferenceKey("a"));
    emit(null);
    assert.equal(updates, 2);
    stop();
    assert.equal(callbacks.size, 0);
    const stopUnavailable = subscribeToolSelectionControlPreference(undefined, () => assert.fail("no Profile"));
    assert.equal(callbacks.size, 0);
    stopUnavailable();
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else Reflect.deleteProperty(globalThis, "window");
  }
});
