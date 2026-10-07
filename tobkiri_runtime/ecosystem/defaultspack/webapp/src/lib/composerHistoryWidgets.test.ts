import test from "node:test";
import assert from "node:assert/strict";
import { composerMentionMetadataFromWidgets, normalizeComposerMentionMetadata, reconcileComposerSemanticDraft } from "./composerWidgets";

const mention = { kind: "group" as const, id: "g-one", label: "Same", syntax: "@Same", profileId: "profile-a", memberIds: ["a", "b"] };
const widget = { id: "history:g-one", type: "text" as const, label: "Same", enabled: true, metadata: { source: "composer_at_mention", mention } };

test("one group mention preserves resolved member IDs through widget metadata", () => {
  assert.deepEqual(composerMentionMetadataFromWidgets([widget]), [mention]);
  const draft = reconcileComposerSemanticDraft({ droppedWidgets: [widget], selectedToolIds: [], text: "@Same" });
  assert.equal(draft.droppedWidgets[0]?.enabled, true);
  assert.deepEqual(composerMentionMetadataFromWidgets(draft.droppedWidgets), [mention]);
});

test("edited or escaped group mention is suppressed atomically", () => {
  for (const text of ["@Different", "\\@Same"]) {
    const draft = reconcileComposerSemanticDraft({ droppedWidgets: [widget], selectedToolIds: [], text });
    assert.equal(draft.droppedWidgets.length, 0);
  }
});

test("untrusted normalization needs same-Profile catalogue and uses its members", () => {
  const forged = { ...mention, memberIds: ["injected"] };
  assert.deepEqual(normalizeComposerMentionMetadata([forged]), []);
  assert.deepEqual(normalizeComposerMentionMetadata([forged], { profileId: "profile-a", references: [mention] }), [mention]);
  assert.deepEqual(normalizeComposerMentionMetadata([forged], { profileId: "profile-b", references: [mention] }), []);
});
