import test from "node:test";
import assert from "node:assert/strict";

import { crashDraftExport, recordCrash, recoverableDraftSnapshot, resetAffectedClientState } from "./crashRecovery";

function memoryStorage(initial: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(initial));
  return {
    get length() { return values.size; }, clear: () => values.clear(), key: (index) => [...values.keys()][index] ?? null,
    getItem: (key) => values.get(key) ?? null, removeItem: (key) => { values.delete(key); }, setItem: (key, value) => { values.set(key, value); },
  };
}

test("draft snapshot allowlists composer input and excludes secrets and unrelated storage", () => {
  const storage = memoryStorage({ "rumi-input": "unsaved prompt", "rumi-defaultspack-local-auth": "secret-token", "other": "private" });
  const snapshot = recoverableDraftSnapshot(storage);
  assert.deepEqual(snapshot?.drafts, { "rumi-input": "unsaved prompt" });
  const exported = crashDraftExport(snapshot!);
  assert.match(exported, /rumi.crash_drafts.v1/);
  assert.doesNotMatch(exported, /secret-token|"other"/);
});

test("repeated crash detector uses a bounded time window", () => {
  const storage = memoryStorage();
  assert.equal(recordCrash(storage, 100_000), 1);
  assert.equal(recordCrash(storage, 100_500), 2);
  assert.equal(recordCrash(storage, 200_000), 1);
});

test("safe reset preserves drafts but revokes legacy stored local auth", () => {
  const storage = memoryStorage({ "rumi-input": "draft", "rumi-workspace-tabs": "tabs", "rumi-defaultspack-local-auth": "credential" });
  resetAffectedClientState(storage);
  assert.equal(storage.getItem("rumi-workspace-tabs"), null);
  assert.equal(storage.getItem("rumi-input"), "draft");
  assert.equal(storage.getItem("rumi-defaultspack-local-auth"), null);
});
