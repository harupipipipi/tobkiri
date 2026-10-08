import assert from "node:assert/strict";
import test from "node:test";
import { loadTaskPetPreference, taskPetPreferenceKey } from "./taskPet";
import { enableTaskPetNotifications, saveTaskPetNotifications, subscribeTaskPetNotifications } from "./taskPetPreferences";
function memoryStorage() {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
}
test("existing true/false values and profile keys remain unchanged", () => {
  const storage = memoryStorage();
  storage.values.set(taskPetPreferenceKey("a", "notifications"), "true");
  storage.values.set(taskPetPreferenceKey("b", "notifications"), "false");
  assert.equal(loadTaskPetPreference(storage, "a", "notifications"), true);
  assert.equal(loadTaskPetPreference(storage, "b", "notifications"), false);
  assert.equal(saveTaskPetNotifications("a", false, storage), true);
  assert.equal(storage.getItem(taskPetPreferenceKey("a", "notifications")), "false");
  assert.equal(loadTaskPetPreference(storage, "b", "notifications"), false);
});
test("same-tab writes publish only the matching profile and failed writes do not publish", () => {
  const storage = memoryStorage();
  let a = 0; let b = 0;
  const stopA = subscribeTaskPetNotifications("a", () => { a++; });
  const stopB = subscribeTaskPetNotifications("b", () => { b++; });
  saveTaskPetNotifications("a", true, storage);
  assert.equal(a, 1); assert.equal(b, 0);
  assert.equal(saveTaskPetNotifications("a", false, { ...storage, setItem: () => { throw new Error("quota"); } }), false);
  assert.equal(a, 1); assert.equal(loadTaskPetPreference(storage, "a", "notifications"), true);
  stopA(); stopB(); saveTaskPetNotifications("a", false, storage); assert.equal(a, 1);
});
test("explicit enable requests permission and only granted responses persist", async () => {
  const storage = memoryStorage(); let requests = 0;
  const requestPermission = async (): Promise<NotificationPermission> => { requests++; return "granted"; };
  assert.equal(requests, 0);
  assert.equal(await enableTaskPetNotifications({ profileId: "a", requestPermission, isCurrent: () => true, storage }), "enabled");
  assert.equal(requests, 1); assert.equal(loadTaskPetPreference(storage, "a", "notifications"), true);
  for (const permission of ["denied", "default"] as const) {
    assert.equal(await enableTaskPetNotifications({ profileId: "b", requestPermission: async () => permission, isCurrent: () => true, storage }), permission);
    assert.equal(storage.getItem(taskPetPreferenceKey("b", "notifications")), null);
  }
});
test("storage and permission errors report failure without claiming opt-in", async () => {
  const storage = memoryStorage();
  const broken = { ...storage, setItem: () => { throw new Error("quota"); } };
  assert.equal(await enableTaskPetNotifications({ profileId: "a", requestPermission: async () => "granted", isCurrent: () => true, storage: broken }), "storage-error");
  assert.equal(loadTaskPetPreference(storage, "a", "notifications"), false);
  assert.equal(await enableTaskPetNotifications({ profileId: "a", requestPermission: async () => { throw new Error("blocked"); }, isCurrent: () => true, storage }), "request-error");
});
test("profile change and unmount fence pending permission responses", async () => {
  for (const reason of ["profile", "unmount"]) {
    const storage = memoryStorage(); let profile = "a"; let mounted = true;
    let resolve!: (permission: NotificationPermission) => void;
    const result = enableTaskPetNotifications({ profileId: "a", requestPermission: () => new Promise<NotificationPermission>((done) => { resolve = done; }), isCurrent: () => mounted && profile === "a", storage });
    if (reason === "profile") profile = "b"; else mounted = false;
    resolve("granted"); assert.equal(await result, "stale"); assert.equal(storage.values.size, 0);
  }
  let requests = 0;
  assert.equal(await enableTaskPetNotifications({ profileId: "a", requestPermission: async () => { requests++; return "granted"; }, isCurrent: () => false, storage: memoryStorage() }), "stale");
  assert.equal(requests, 0);
});
test("other-tab storage events refresh only matching profiles, clear, and clean up", () => {
  const callbacks = new Set<(event: { key: string | null }) => void>();
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { configurable: true, value: { addEventListener: (_type: string, callback: (event: { key: string | null }) => void) => callbacks.add(callback), removeEventListener: (_type: string, callback: (event: { key: string | null }) => void) => callbacks.delete(callback) } });
  try {
    let updates = 0; const stop = subscribeTaskPetNotifications("a", () => { updates++; });
    for (const callback of callbacks) callback({ key: taskPetPreferenceKey("b", "notifications") });
    assert.equal(updates, 0);
    for (const callback of callbacks) callback({ key: taskPetPreferenceKey("a", "notifications") });
    for (const callback of callbacks) callback({ key: null });
    assert.equal(updates, 2); stop(); assert.equal(callbacks.size, 0);
  } finally { if (original) Object.defineProperty(globalThis, "window", original); else Reflect.deleteProperty(globalThis, "window"); }
});
