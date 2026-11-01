import test from "node:test";
import assert from "node:assert/strict";
import { createModelPinStore, isModelPinIdentity, modelPinStorageKey, orderPinnedModels, resolveModelPinScope, createModelPinOperationGuard } from "./modelPins";
const id = (model: string) => JSON.stringify(["connection", "local", model]);
const storage = () => {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); } };
};

test("pin unpin reload and independent profiles notify same tab", () => {
  const db = storage(); const store = createModelPinStore(db);
  let calls = 0; const unsubscribe = store.subscribe("a", () => { calls++; });
  store.toggle("a", id("one")); store.toggle("b", id("two"));
  assert.deepEqual(store.getSnapshot("a"), [id("one")]);
  assert.deepEqual(createModelPinStore(db).getSnapshot("a"), [id("one")]);
  assert.deepEqual(store.getSnapshot("b"), [id("two")]);
  store.toggle("a", id("one")); assert.deepEqual(store.getSnapshot("a"), []);
  assert.equal(calls, 2); unsubscribe(); store.toggle("a", id("one")); assert.equal(calls, 2);
  assert.notEqual(modelPinStorageKey("a/b"), modelPinStorageKey("a%2Fb"));
});

test("decode duplicates migrate valid legacy data and fail closed on corruption", () => {
  const db = storage(); const key = modelPinStorageKey("a");
  db.values.set(key, JSON.stringify([id("one"), id("one")]));
  assert.deepEqual(createModelPinStore(db).getSnapshot("a"), [id("one")]);
  assert.deepEqual(JSON.parse(db.values.get(key)!), { version: 1, pins: [id("one")] });
  for (const raw of ["bad", '{"version":2,"pins":[]}', JSON.stringify(["secret"]),
    JSON.stringify({ version: 1, pins: [id("one"), "invalid"] }),
    JSON.stringify({ version: 1, pins: Array(501).fill(id("one")) })]) {
    db.values.set(key, raw); assert.deepEqual(createModelPinStore(db).getSnapshot("a"), []);
  }
});

test("identity tuples reject metadata and missing components", () => {
  assert.equal(isModelPinIdentity(id("one")), true);
  assert.equal(isModelPinIdentity(JSON.stringify(["catalog", "openai", "one"])), true);
  for (const tuple of [["connection", "", "one"], ["connection", "local", " "],
    ["other", "local", "one"], ["connection", "local", "one", "metadata"],
    ["catalog", "x".repeat(513), "one"]]) assert.equal(isModelPinIdentity(JSON.stringify(tuple)), false);
});

test("storage exceptions retain memory and unresolved profile never writes", () => {
  const store = createModelPinStore({ getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); } });
  store.toggle("a", id("one")); store.storageChanged("a");
  assert.deepEqual(store.getSnapshot("a"), [id("one")]);
  store.toggle("", id("one")); assert.deepEqual(store.getSnapshot(""), []);
});

test("external storage changes refresh subscribers with stable snapshots", () => {
  const db = storage(); const store = createModelPinStore(db);
  const old = store.getSnapshot("a"); assert.equal(old, store.getSnapshot("a"));
  let calls = 0; store.subscribe("a", () => { calls++; });
  db.values.set(modelPinStorageKey("a"), JSON.stringify({ version: 1, pins: [id("one")] }));
  store.storageChanged("a"); assert.equal(calls, 1);
  const next = store.getSnapshot("a"); store.storageChanged("a"); assert.equal(next, store.getSnapshot("a"));
  store.storageChanged("a", null); assert.deepEqual(store.getSnapshot("a"), []); assert.equal(calls, 2);
});

test("loaded rows follow pin insertion order with stable unpinned rows and no synthetic rows", () => {
  const items = ["b", "a", "c", "d", "a"];
  assert.deepEqual(orderPinnedModels(items, [id("missing"), id("c"), id("a")], id), ["c", "a", "a", "b", "d"]);
  assert.deepEqual(items, ["b", "a", "c", "d", "a"]);
  assert.deepEqual(orderPinnedModels(items, [], id), items);
});


test("memory-only stores keep pins when requested to reload", () => {
  const store = createModelPinStore();
  store.toggle("a", id("one"));
  store.storageChanged("a");
  assert.deepEqual(store.getSnapshot("a"), [id("one")]);
});


test("verified capture scopes preserve storage profile across revisions and fence stale operations", () => {
  const capture = { profile_id: "a", profile_revision: "1", activation_id: "activation",
    plan_hash: "plan", catalog_hash: "catalog", security_epoch: 1 };
  const old = resolveModelPinScope(capture, "plan");
  const next = resolveModelPinScope({ ...capture, profile_revision: "2", security_epoch: 2 }, "plan");
  assert.equal(old.enabled, true);
  assert.equal(old.profileId, next.profileId);
  assert.notEqual(old.fence, next.fence);
  assert.equal(modelPinStorageKey(old.profileId), modelPinStorageKey(next.profileId));
  for (const unavailable of [resolveModelPinScope(null, "plan"),
    resolveModelPinScope(capture, "other"), resolveModelPinScope(capture, undefined),
    resolveModelPinScope({ ...capture, activation_id: "" }, "plan"),
    resolveModelPinScope({ ...capture, profile_id: " " }, "plan")]) {
    assert.deepEqual(unavailable, { enabled: false, profileId: "", fence: "" });
  }
});

test("subscribers stay profile isolated and external corrupt reload clears pins", () => {
  const store = createModelPinStore(); let a = 0; let b = 0;
  const unsubscribe = store.subscribe("a", () => { a++; });
  store.subscribe("b", () => { b++; });
  const toggle = store.toggle;
  toggle("a", id("one")); assert.equal(a, 1); assert.equal(b, 0);
  store.storageChanged("a", "malformed"); assert.equal(a, 2); assert.deepEqual(store.getSnapshot("a"), []);
  unsubscribe(); toggle("a", id("one")); assert.equal(a, 2);
  toggle("b", id("two")); assert.equal(b, 1);
});

test("pin limit bounds writes and rejects control characters and metadata payloads", () => {
  const db = storage(); const store = createModelPinStore(db);
  for (let i = 0; i < 500; i++) store.toggle("a", id(String(i)));
  store.toggle("a", id("overflow")); assert.equal(store.getSnapshot("a").length, 500);
  store.toggle("a", id("0")); store.toggle("a", id("replacement"));
  assert.equal(store.getSnapshot("a").length, 500);
  assert.equal(store.getSnapshot("a").includes(id("replacement")), true);
  assert.equal(isModelPinIdentity(id("bad\nmodel")), false);
  const payload = JSON.parse(db.values.get(modelPinStorageKey("a"))!);
  assert.deepEqual(Object.keys(payload).sort(), ["pins", "version"]);
  db.values.set(modelPinStorageKey("meta"), JSON.stringify({ version: 1,
    pins: [{ identity: id("one"), apiKey: "private" }] }));
  assert.deepEqual(createModelPinStore(db).getSnapshot("meta"), []);
});


test("scope reactivation reloads external storage but preserves failed local writes", () => {
  const db = storage(); const store = createModelPinStore(db);
  store.toggle("a", id("old"));
  store.getSnapshot("b");
  db.values.set(modelPinStorageKey("a"), JSON.stringify({ version: 1, pins: [id("external")] }));
  store.storageChanged("a");
  assert.deepEqual(store.getSnapshot("a"), [id("external")]);
  let fail = false;
  const failing = createModelPinStore({ getItem: db.getItem, setItem(key, value) {
    if (fail) throw Error("quota"); db.setItem(key, value);
  } });
  failing.getSnapshot("a"); fail = true; failing.toggle("a", id("unsaved"));
  failing.storageChanged("a");
  assert.deepEqual(failing.getSnapshot("a"), [id("external"), id("unsaved")]);
  fail = false; failing.toggle("a", id("next"));
  assert.deepEqual(createModelPinStore(db).getSnapshot("a"), [id("external"), id("unsaved"), id("next")]);
});

test("operation leases reject stale scopes and unmounted callbacks with Strict Mode replay", () => {
  const guard = createModelPinOperationGuard();
  assert.equal(guard.isCurrent("a:plan1"), false);
  const dispose = guard.activate("a:plan1");
  assert.equal(guard.isCurrent("a:plan1"), true);
  assert.equal(guard.isCurrent("a:plan2"), false);
  dispose(); assert.equal(guard.isCurrent("a:plan1"), false);
  const strictReplay = guard.activate("a:plan1");
  assert.equal(guard.isCurrent("a:plan1"), true);
  const newer = guard.activate("b:plan1");
  strictReplay(); assert.equal(guard.isCurrent("b:plan1"), true);
  assert.equal(guard.isCurrent("a:plan1"), false);
  newer(); assert.equal(guard.isCurrent("b:plan1"), false);
});
