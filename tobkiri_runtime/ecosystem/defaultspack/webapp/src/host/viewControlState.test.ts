import assert from "node:assert/strict";
import test from "node:test";
import { freshTextDraft, refreshTextDraft, textDraftDirty, viewOperationOutcome } from "./viewControlState";

test("dirty text survives failed writes and remote authoritative refresh", () => {
  const draft = { ...freshTextDraft("old"), value: "my edit" };
  assert.equal(refreshTextDraft(draft, "another edit"), draft);
  assert.equal(textDraftDirty(draft), true);
  assert.deepEqual(freshTextDraft("another edit"), { value: "another edit", baseline: "another edit", awaiting: null });
});

test("clean text follows source and only matching successful save clears dirty state", () => {
  assert.equal(refreshTextDraft(freshTextDraft("old"), "new").value, "new");
  const draft = { ...freshTextDraft("old"), value: "mine", awaiting: "mine" };
  assert.equal(refreshTextDraft(draft, "other"), draft);
  assert.equal(textDraftDirty(refreshTextDraft(draft, "mine")), false);
  assert.equal(textDraftDirty(refreshTextDraft({ ...draft, value: "new unsaved" }, "mine")), true);
});

test("approval, conflicts, and failures retain drafts rather than confirming saves", () => {
  assert.equal(viewOperationOutcome({ state: "awaiting_approval" }), "approval");
  assert.equal(viewOperationOutcome({ status: "conflict" }), "failed");
  assert.equal(viewOperationOutcome({ state: "denied" }), "failed");
  assert.equal(viewOperationOutcome({ status: "ok" }), "returned");
});
