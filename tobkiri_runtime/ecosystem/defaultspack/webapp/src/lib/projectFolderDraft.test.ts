import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_PROJECT_FOLDERS, appendProjectFolderSelections, consumeProjectFolderSelections,
  emptyProjectFolderDraft, normalizeProjectFolderSelections, projectFolderLabels,
  projectFolderSelectionSet, removeProjectFolderSelection, setPrimaryProjectFolderSelection,
} from "./projectFolderDraft";

const choice = (selection_id: string, display_name = "source", expires_in_ms = 1000) => ({ selection_id, display_name, expires_in_ms });

test("legacy single selection normalizes to one primary and multi set preserves explicit primary", () => {
  const first = choice("a");
  assert.deepEqual(normalizeProjectFolderSelections(first), { selections: [first], primary_selection_id: "a" });
  const set = { selections: [first, choice("b")], primary_selection_id: "b" };
  assert.deepEqual(normalizeProjectFolderSelections(set), set);
  assert.throws(() => normalizeProjectFolderSelections({ ...set, primary_selection_id: "missing" }), /primary folder/);
});

test("native picker response seeds one or two folders and permits a second primary", () => {
  const first = { ...choice("a"), cancelled: false };
  const second = { ...choice("b"), cancelled: false };
  for (const selections of [[first], [first, second]]) {
    const reply = { ...first, selections, primary_selection_id: first.selection_id };
    const draft = appendProjectFolderSelections(emptyProjectFolderDraft(), reply, 100);
    assert.deepEqual(projectFolderSelectionSet(draft, 200), {
      selections, primary_selection_id: first.selection_id,
    });
    assert.throws(() => normalizeProjectFolderSelections({
      ...reply, primary_selection_id: "outside",
    }), /primary folder/);
    assert.throws(() => setPrimaryProjectFolderSelection(draft, "outside"), /primary folder/);
    if (selections.length === 2) {
      const updated = setPrimaryProjectFolderSelection(draft, second.selection_id);
      assert.deepEqual(projectFolderSelectionSet(updated, 200), {
        selections, primary_selection_id: second.selection_id,
      });
      assert.equal(draft.primary_selection_id, first.selection_id);
    }
  }
});

test("add retains tickets with matching names, skips repeated identity without refreshing expiry", () => {
  const draft = appendProjectFolderSelections(emptyProjectFolderDraft(), choice("a"), 100);
  const next = appendProjectFolderSelections(draft, {
    selections: [choice("a"), choice("b")], primary_selection_id: "b",
  }, 300);
  assert.equal(next.selections.length, 2);
  assert.deepEqual(next.acquiredAtById, { a: 100, b: 300 });
  assert.equal(next.primary_selection_id, "a");
  assert.deepEqual(projectFolderLabels(next), ["source (1)", "source (2)"]);
  assert.equal(draft.selections.length, 1);
  assert.throws(() => projectFolderSelectionSet(next, 1100), /expired/);
});

test("changing primary and removing folders keeps deterministic scalar binding", () => {
  let draft = appendProjectFolderSelections(emptyProjectFolderDraft(), {
    selections: [choice("a"), choice("b"), choice("c")], primary_selection_id: "b",
  }, 100);
  draft = setPrimaryProjectFolderSelection(draft, "c");
  draft = removeProjectFolderSelection(draft, "c");
  assert.equal(draft.primary_selection_id, "a");
  assert.deepEqual(draft.selections.map((selection) => selection.selection_id), ["a", "b"]);
  draft = removeProjectFolderSelection(removeProjectFolderSelection(draft, "a"), "b");
  assert.deepEqual(draft, emptyProjectFolderDraft());
  assert.throws(() => setPrimaryProjectFolderSelection(draft, "unknown"), /primary folder/);
});

test("bounds reject whole addition without mutating the existing draft", () => {
  const selections = Array.from({ length: MAX_PROJECT_FOLDERS }, (_, index) => choice(`ticket-${index}`));
  const draft = appendProjectFolderSelections(emptyProjectFolderDraft(), {
    selections, primary_selection_id: selections[0].selection_id,
  }, 100);
  assert.throws(() => appendProjectFolderSelections(draft, choice("overflow"), 100), /at most 32/);
  assert.equal(draft.selections.length, MAX_PROJECT_FOLDERS);
  assert.throws(() => normalizeProjectFolderSelections({ selections: [...selections, choice("overflow")], primary_selection_id: "ticket-0" }), /between 1 and 32/);
  assert.throws(() => normalizeProjectFolderSelections({ selections: [choice("a"), choice("a")], primary_selection_id: "a" }), /duplicate/);
  assert.throws(() => normalizeProjectFolderSelections(choice("a", "folder", 0)), /invalid/);
});

test("tickets expire locally and cannot prepare twice after a mount attempt", () => {
  const draft = appendProjectFolderSelections(emptyProjectFolderDraft(), choice("a"), 100);
  assert.deepEqual(projectFolderSelectionSet(draft, 1099).selections, [choice("a")]);
  assert.throws(() => projectFolderSelectionSet(draft, 1100), /expired/);
  const submitted = consumeProjectFolderSelections(draft);
  assert.throws(() => projectFolderSelectionSet(submitted, 200), /already submitted/);
  const repeated = appendProjectFolderSelections(submitted, choice("a"), 300);
  assert.throws(() => projectFolderSelectionSet(repeated, 300), /already submitted/);
  const removed = removeProjectFolderSelection(submitted, "a");
  assert.throws(() => appendProjectFolderSelections(removed, choice("a"), 300), /already submitted/);
  const replaced = appendProjectFolderSelections(removed, choice("fresh"), 300);
  assert.equal(projectFolderSelectionSet(replaced, 400).primary_selection_id, "fresh");
  assert.deepEqual(submitted.selections, draft.selections);
});
