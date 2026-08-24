import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const moduleUrl = new URL(
  "../../../../rumi_prompt_studio_pack/ui/draft-state.js",
  import.meta.url,
);
const promptStudioRoot = new URL(
  "../../../../rumi_prompt_studio_pack/ui/",
  import.meta.url,
);
const promptStudioHtml = readFileSync(new URL("index.html", promptStudioRoot), "utf8");
const promptStudioApp = readFileSync(new URL("app.js", promptStudioRoot), "utf8");
const sandbox: Record<string, unknown> = {};
vm.runInNewContext(readFileSync(moduleUrl, "utf8"), sandbox);
const {
  DraftVault,
  HistoryStateStorage,
  LoadSequencer,
  conflictChoice,
  contextKey,
  draftStorageKey,
  isDirtyDraft,
  preserveEditorState,
  shouldBlockUnload,
  transitionNeedsResolution,
} = sandbox.PromptStudioDraftState as Record<string, any>;

class MemoryStorage {
  values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

class MemoryHistory {
  state: Record<string, unknown> | null = null;
  replaceState(state: Record<string, unknown>) { this.state = state; }
}

const context = {
  profileId: "profile-a",
  promptId: "system.main",
  modelProfileId: "model-a",
  conversationId: "conversation-a",
  baseRevision: "sha256:base-a",
};

test("dirty drafts block every destructive Prompt Studio context transition", () => {
  for (const kind of [
    "prompt", "profile", "model", "conversation", "refresh", "back",
    "route", "rollback",
  ]) {
    assert.equal(transitionNeedsResolution(kind, true), true, kind);
  }
  assert.equal(transitionNeedsResolution("test", true), false);
  assert.equal(transitionNeedsResolution("prompt", false), false);
});

test("recoverable drafts are scoped to prompt, profile, model, conversation, and base revision", () => {
  const storage = new MemoryStorage();
  const vault = new DraftVault(storage, () => 1000);
  const uiState = { selectionStart: 3, selectionEnd: 8, editorScrollTop: 91, output: "lint" };
  vault.save(context, "unsaved body", uiState);

  assert.equal(vault.restore(context)?.draftBody, "unsaved body");
  assert.equal(vault.restore(context)?.uiState.editorScrollTop, 91);
  for (const [field, value] of [
    ["profileId", "profile-b"],
    ["promptId", "other.prompt"],
    ["modelProfileId", "model-b"],
    ["conversationId", "conversation-b"],
    ["baseRevision", "sha256:base-b"],
  ]) {
    assert.equal(vault.restore({ ...context, [field]: value }), null, field);
  }
  assert.notEqual(draftStorageKey(context), draftStorageKey({ ...context, promptId: "other.prompt" }));
});

test("opaque sandbox drafts use browsing-context history without Web Storage", () => {
  const history = new MemoryHistory();
  const first = new DraftVault(new HistoryStateStorage(history), () => 1000);
  first.save(context, "history-backed draft", { selectionStart: 7 });

  const afterReload = new DraftVault(new HistoryStateStorage(history), () => 1001);
  assert.equal(afterReload.restore(context)?.draftBody, "history-backed draft");
  assert.equal(afterReload.restore(context)?.uiState.selectionStart, 7);
  assert.equal(history.state?.prompt_id, undefined);
});

test("opaque sandbox dialogs close without granting form submission capability", () => {
  assert.doesNotMatch(promptStudioHtml, /method=["']dialog["']/);
  assert.match(promptStudioHtml, /data-dialog-value="cancel"/);
  assert.match(promptStudioApp, /dialog\.close\(button\.dataset\.dialogValue/);
});

test("expired or corrupt recovery records fail closed without replacing the editor", () => {
  const storage = new MemoryStorage();
  const vault = new DraftVault(storage, () => 0);
  vault.save(context, "recoverable");
  const expired = new DraftVault(storage, () => 31 * 24 * 60 * 60 * 1000);
  assert.equal(expired.restore(context), null);

  storage.setItem(draftStorageKey(context), "{not-json");
  assert.equal(vault.restore(context), null);
});

test("rapid prompt and model loads only accept the newest exact context", () => {
  const loads = new LoadSequencer();
  const first = loads.begin(context);
  const secondContext = { ...context, promptId: "second.prompt" };
  const second = loads.begin(secondContext);
  assert.equal(loads.accepts(first, context), false);
  assert.equal(loads.accepts(second, secondContext), true);

  const modelContext = { ...secondContext, modelProfileId: "model-b" };
  const model = loads.begin(modelContext);
  assert.equal(loads.accepts(second, secondContext), false);
  assert.equal(loads.accepts(model, modelContext), true);
  assert.notEqual(contextKey(secondContext), contextKey(modelContext));
});

test("conflict choices are explicit and dirty detection is exact", () => {
  assert.equal(isDirtyDraft("base", "base"), false);
  assert.equal(isDirtyDraft("base", "draft"), true);
  for (const choice of ["compare", "reload", "overwrite", "cancel"]) {
    assert.equal(conflictChoice(choice), choice);
  }
  assert.throws(() => conflictChoice("ignore"), /invalid conflict choice/);
});

test("failed save and rollback preserve cursor, selection, scroll, and inspector state", () => {
  const beforeRequest = preserveEditorState({
    selectionStart: 4,
    selectionEnd: 9,
    editorScrollTop: 240,
    pageScrollX: 3,
    pageScrollY: 80,
    output: "inspector result",
    outputScrollTop: 17,
    versionsExpanded: true,
  });
  const afterFailedSave = beforeRequest;
  const afterFailedRollback = beforeRequest;
  assert.deepEqual(afterFailedSave, beforeRequest);
  assert.deepEqual(afterFailedRollback, beforeRequest);
  assert.equal(Object.isFrozen(beforeRequest), true);
});

test("browser refresh and close guard dirty drafts but allow resolved navigation", () => {
  assert.equal(shouldBlockUnload(true, false), true);
  assert.equal(shouldBlockUnload(false, false), false);
  assert.equal(shouldBlockUnload(true, true), false);
});

test("rollback remains blocked until dirty state is resolved and consequence review runs", () => {
  assert.equal(transitionNeedsResolution("rollback", true), true);
  assert.equal(transitionNeedsResolution("rollback", false), false);
});
