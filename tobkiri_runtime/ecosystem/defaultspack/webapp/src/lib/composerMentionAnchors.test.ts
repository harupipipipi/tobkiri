import test from "node:test";
import assert from "node:assert/strict";
import { anchorComposerMentionWidget, confirmedComposerMentionRange, deduplicateComposerWidgets, updateConfirmedComposerWidgets } from "./composerMentionAnchors";
import { composerToolMentionWidget, composerSkillMentionWidget, composerFileMentionWidget, composerServiceMentionWidget } from "./composerWidgets";

const tool = composerToolMentionWidget({ id: "a", label: "A" });
test("an anchor confirms only one identical occurrence", () => {
  const text = "@A @A";
  const widget = anchorComposerMentionWidget(tool, text, 3);
  assert.deepEqual(confirmedComposerMentionRange(widget, text), { start: 3, end: 5, syntax: "@A" });
  assert.equal(confirmedComposerMentionRange(widget, "@A"), null);
  assert.equal(confirmedComposerMentionRange(tool, text)?.start, 0);
});
test("draft presentation deduplication retains current occurrences and stable widget IDs", () => {
  const text = "@A @A @A";
  const first = anchorComposerMentionWidget(tool, text, 0);
  const second = anchorComposerMentionWidget(tool, text, 3);
  const custom = { id: "custom", type: "panel", label: "Old" };
  const latest = { ...custom, label: "Latest" };
  assert.deepEqual(deduplicateComposerWidgets([first, second, first, custom, latest], text), [first, second, latest]);
  assert.deepEqual(deduplicateComposerWidgets([tool, tool], text), [tool]);
  assert.deepEqual(deduplicateComposerWidgets([first, second], "changed"), [second]);
});

test("inserting an identical typed mention leaves it unconfirmed", () => {
  const widget = anchorComposerMentionWidget(tool, "@A", 0);
  const next = updateConfirmedComposerWidgets("@A", "@A @A", [widget], { start: 2, end: 2 });
  assert.equal(confirmedComposerMentionRange(next[0], "@A @A")?.start, 0);
});
test("native edits and composition text retain untouched repeated anchors only", () => {
  let text = "@A @A @A";
  let widgets = [0, 3, 6].map((start) => anchorComposerMentionWidget(tool, text, start));
  for (const { start, insertion } of [{ start: 0, insertion: "先 " }, { start: 5, insertion: "日本語 " }, { start: 14, insertion: " 後" }]) {
    const next = text.slice(0, start) + insertion + text.slice(start);
    widgets = updateConfirmedComposerWidgets(text, next, widgets, { start, end: start });
    text = next;
    assert.equal(widgets.length, 3);
    assert.ok(widgets.every((widget) => confirmedComposerMentionRange(widget, text)));
  }
  const middle = confirmedComposerMentionRange(widgets[1], text)!;
  const next = text.slice(0, middle.start) + text.slice(middle.end);
  widgets = updateConfirmedComposerWidgets(text, next, widgets, middle);
  assert.equal(widgets.length, 2);
  assert.deepEqual(widgets.map((widget) => confirmedComposerMentionRange(widget, next)?.start), [2, 10]);
});

test("deleting the confirmed occurrence cannot transfer to a typed duplicate", () => {
  const widget = anchorComposerMentionWidget(tool, "@A @A", 0);
  assert.deepEqual(updateConfirmedComposerWidgets("@A @A", "@A", [widget], { start: 0, end: 3 }), []);
  assert.deepEqual(updateConfirmedComposerWidgets("@A @A", "@A", [widget]), []);
});
test("Unicode native insertion moves untouched UTF-16 ranges", () => {
  const widget = anchorComposerMentionWidget(tool, "🙂 @A", 3);
  const [moved] = updateConfirmedComposerWidgets("🙂 @A", "先🙂 @A", [widget]);
  assert.deepEqual(confirmedComposerMentionRange(moved, "先🙂 @A"), { start: 4, end: 6, syntax: "@A" });
});
test("same-text replacement and edits inside syntax invalidate confirmation", () => {
  const widget = anchorComposerMentionWidget(tool, "@A", 0);
  assert.deepEqual(updateConfirmedComposerWidgets("@A", "@A", [widget], { start: 0, end: 2 }), []);
  assert.deepEqual(updateConfirmedComposerWidgets("@A", "@AB", [widget]), []);
  assert.deepEqual(updateConfirmedComposerWidgets("@A", "\\@A", [widget]), []);
});
test("negative service, skill and file widgets retain exact identity and custom widgets pass through", () => {
  const widgets = [composerServiceMentionWidget({ id: "service", label: "Service", toolIds: [] }), composerSkillMentionWidget({ id: "skill", label: "Skill" }), composerFileMentionWidget("README.md")];
  const text = "@Service @Skill @README.md";
  assert.deepEqual(widgets.map((widget) => confirmedComposerMentionRange(widget, text)?.syntax), ["@Service", "@Skill", "@README.md"]);
  const negative = { ...tool, metadata: { ...tool.metadata, mention: { ...(tool.metadata?.mention as object), syntax: "@-A", intent: "exclude" } } };
  assert.equal(confirmedComposerMentionRange(anchorComposerMentionWidget(negative, "@-A", 0), "@-A")?.syntax, "@-A");
  const custom = { id: "custom", type: "panel", label: "Custom" };
  assert.deepEqual(updateConfirmedComposerWidgets("x", "y", [custom]), [custom]);
});
test("email URL escapes longer names and half-surrogate offsets are rejected", () => {
  for (const text of ["a@A", "https://site/@A", "\\@A", "@AB"]) assert.equal(confirmedComposerMentionRange(tool, text), null);
  assert.equal(confirmedComposerMentionRange(anchorComposerMentionWidget(tool, "🙂@A", 1), "🙂@A")?.start, 2);
});
test("native selection deletion moves another confirmed occurrence without transferring removed identity", () => {
  const other = composerToolMentionWidget({ id: "b", label: "B" });
  const previous = "@A @B @A";
  const widgets = [anchorComposerMentionWidget(tool, previous, 0), anchorComposerMentionWidget(other, previous, 3)];
  const next = updateConfirmedComposerWidgets(previous, "@B @A", widgets, { start: 0, end: 3 });
  assert.deepEqual(next.map((widget) => widget.sourceItemId), ["b"]);
  assert.deepEqual(confirmedComposerMentionRange(next[0], "@B @A"), { start: 0, end: 2, syntax: "@B" });
});
test("punctuation permits a mention while period plus a word extends it", () => {
  assert.equal(confirmedComposerMentionRange(tool, "@A.")?.end, 2);
  assert.equal(confirmedComposerMentionRange(tool, "@A.foo"), null);
  assert.equal(confirmedComposerMentionRange(tool, "お願い@A、確認")?.start, 3);
});

test("submit normalization preserves the confirmed occurrence through trim and slash prefix", async () => {
  const { transformConfirmedComposerWidgetsForSubmit } = await import("./composerMentionAnchors");
  const source = "  // @A @A  ";
  const widget = anchorComposerMentionWidget(tool, source, 5);
  const [moved] = transformConfirmedComposerWidgetsForSubmit(source, "/ @A @A", [widget]);
  assert.deepEqual(confirmedComposerMentionRange(moved, "/ @A @A"), { start: 2, end: 4, syntax: "@A" });
  assert.deepEqual(transformConfirmedComposerWidgetsForSubmit(source, "@A", [widget]), []);
});
