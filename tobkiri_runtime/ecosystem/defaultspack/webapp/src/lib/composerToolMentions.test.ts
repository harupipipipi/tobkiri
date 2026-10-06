import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { composerMentionSelectionRequest, consumeLegacyToolMentionSnapshot, materializeLegacyToolMentions, replaceComposerToolMentions, resolveComposerToolMentions } from "./composerToolMentions";
import { composerExtensionItems, composerFileMentionWidget, composerServiceMentionWidget, composerSkillMentionWidget, composerToolMentionWidget, reconcileComposerSemanticDraft } from "./composerWidgets";
import type { ComposerExtensionItem } from "../renderers/types";

const tools: ComposerExtensionItem[] = [
  { id: "drive_read", label: "Drive Read", ui: { group_id: "google_drive", group_label: "Google Drive", service_id: "google_drive" } },
  { id: "drive_write", label: "Drive Write", ui: { group_id: "google_drive", group_label: "Google Drive", service_id: "google_drive" } },
  { id: "browser_open", label: "Browser Open", ui: { group_id: "browser", service_id: "browser" } },
  { id: "not_implemented", label: "Unavailable", disabled: true, ui: { group_id: "google_drive", service_id: "google_drive" } },
];

test("typing/pasting a tool id produces the visible turn selection and deletion clears it", () => {
  const draft = resolveComposerToolMentions("Please @drive_read", [], tools);
  assert.deepEqual(draft.toolIds, ["drive_read"]);
  assert.equal(draft.widgets[0].metadata?.source, "composer_at_mention");
  assert.deepEqual(composerMentionSelectionRequest(draft, "auto"), {
    mode: "manual", include: [{ kind: "tool", id: "drive_read" }], exclude: [], scope: "turn", must_use: true,
  });
  assert.deepEqual(resolveComposerToolMentions("Please", draft.widgets, tools).toolIds, []);
});

test("service selection expands available tools and service exclusion stays visible", () => {
  const draft = resolveComposerToolMentions("@google_drive @-browser", [], tools);
  assert.deepEqual(draft.toolIds, ["drive_read", "drive_write"]);
  assert.deepEqual(draft.include, [{ kind: "tool", id: "drive_read" }, { kind: "tool", id: "drive_write" }]);
  assert.deepEqual(draft.exclude, [{ kind: "service", id: "browser" }]);
  assert.equal(draft.widgets[1].id, "exclude:mention-service:browser");
  assert.deepEqual(resolveComposerToolMentions("@google_drive", draft.widgets, tools).exclude, []);
});

test("exclusion wins a conflicting mention and none mode emits no includes", () => {
  const draft = resolveComposerToolMentions("@drive_read @-google_drive", [], tools);
  assert.deepEqual(draft.toolIds, []);
  assert.deepEqual(composerMentionSelectionRequest(draft, "none").include, []);
  assert.deepEqual(resolveComposerToolMentions("@drive_read", draft.widgets, tools).toolIds, ["drive_read"]);
});

test("legacy pins/exclusions materialize into deletable input without unavailable tools", () => {
  const migrated = materializeLegacyToolMentions("Review", {
    include: [{ kind: "service", id: "google_drive" }],
    exclude: [{ kind: "service", id: "browser" }],
  }, ["drive_read", "not_implemented"], tools);
  assert.equal(migrated.value, "Review @google_drive @drive_read @-browser ");
  assert.deepEqual(resolveComposerToolMentions(migrated.value, migrated.widgets, tools).toolIds, ["drive_read", "drive_write"]);
  assert.deepEqual(resolveComposerToolMentions("Review", migrated.widgets, tools), { include: [], exclude: [], toolIds: [], widgets: [] });
  assert.equal(materializeLegacyToolMentions(migrated.value, {}, ["drive_read"], tools).value, migrated.value);
});

test("semantic service/skill/file ownership wins colliding tool labels without deleting prose", () => {
  const items = [
    { id: "first", label: "Shared", ui: { group_id: "group", group_label: "Shared" } },
    { id: "second", label: "Other", ui: { group_id: "group", group_label: "Shared" } },
    { id: "settings_tool", label: "Settings" }, { id: "file_tool", label: "README.md" },
  ];
  const widgets = [
    composerServiceMentionWidget({ id: "group", label: "Shared", toolIds: ["first", "second"] }),
    composerSkillMentionWidget({ id: "settings_assistant", label: "Settings" }), composerFileMentionWidget("README.md"),
  ];
  const draft = resolveComposerToolMentions("@Shared @Settings @README.md", widgets, items);
  assert.deepEqual(draft.toolIds, ["first", "second"]);
  assert.deepEqual(draft.include, [{ kind: "tool", id: "first" }, { kind: "tool", id: "second" }]);
  const removed = replaceComposerToolMentions("@Shared keep @Settings @README.md", widgets, items, []);
  assert.equal(removed.value, " keep @Settings @README.md");
  assert.deepEqual(removed.widgets.map((widget) => widget.type), ["skill", "file"]);
});

test("presentation groups expand exact tools instead of becoming backend service ids", () => {
  const items = [{ id: "read_file", label: "Read", ui: { group_id: "coding/files/read", service_id: "files" } }];
  assert.deepEqual(resolveComposerToolMentions("@coding/files/read", [], items).include, [{ kind: "tool", id: "read_file" }]);
  assert.deepEqual(resolveComposerToolMentions("@files", [], items).include, [{ kind: "tool", id: "read_file" }]);
});

test("emails, URLs, escaped and unknown @ remain ordinary text", () => {
  const text = "a@drive_read https://site/@drive_read \\@drive_read @missing @not_implemented";
  assert.deepEqual(resolveComposerToolMentions(text, [], tools).toolIds, []);
  assert.equal(replaceComposerToolMentions(text, [], tools, []).value, text);
});

test("sidebar selection inserts inline targets and preserves text and file widgets", () => {
  const widgets = [composerToolMentionWidget(tools[0]), composerFileMentionWidget("README.md")];
  const selected = replaceComposerToolMentions("Use @Drive Read then @README.md", widgets, tools, ["browser_open"]);
  assert.equal(selected.value, "Use  then @README.md @browser_open ");
  assert.deepEqual(resolveComposerToolMentions(selected.value, selected.widgets, tools).toolIds, ["browser_open"]);
  assert.ok(selected.widgets.some((widget) => widget.type === "file"));
});

test("negative semantic widgets survive reconciliation without selecting tools", () => {
  const draft = resolveComposerToolMentions("@-browser", [], tools);
  const reconciled = reconcileComposerSemanticDraft({ text: "@-browser", droppedWidgets: draft.widgets, selectedToolIds: [] });
  assert.equal(reconciled.droppedWidgets.length, 1);
  assert.deepEqual(reconciled.selectedToolIds, []);
});

test("authenticated service/readiness metadata keeps missing tools unavailable", () => {
  const projected = composerExtensionItems([{ id: "drive_read", label: "Read", category: "tool", ui: { group_id: "custom" }, tool_info: { service_id: "google_drive", setup_state: { status: "missing" } } }]);
  assert.equal(projected[0].ui?.service_id, "google_drive");
  assert.equal(projected[0].disabled, true);
  assert.deepEqual(resolveComposerToolMentions("@drive_read @google_drive", [], projected).toolIds, []);
});

test("explicit sidebar selection removes the overlapping inline exclusion", () => {
  const original = resolveComposerToolMentions("Find @-browser", [], tools);
  const enabled = replaceComposerToolMentions("Find @-browser", original.widgets, tools, ["browser_open"]);
  assert.equal(enabled.value, "Find @browser_open ");
  const draft = resolveComposerToolMentions(enabled.value, enabled.widgets, tools);
  assert.deepEqual(draft.toolIds, ["browser_open"]);
  assert.deepEqual(draft.exclude, []);
});


test("semantic ownership respects token boundaries without hiding longer raw mentions", () => {
  const items = [{ id: "a", label: "A" }, { id: "ab", label: "AB" }];
  const draft = resolveComposerToolMentions("@A @AB", [composerToolMentionWidget(items[0])], items);
  assert.deepEqual(draft.toolIds, ["a", "ab"]);
});

test("service mentions remain compatible with saved submission without opening custom widget context", async () => {
  const { isSavedTurnToolMentionWidget } = await import("./composerToolMentions");
  const draft = resolveComposerToolMentions("@google_drive @-browser", [], tools);
  assert.ok(draft.widgets.every(isSavedTurnToolMentionWidget));
  assert.equal(isSavedTurnToolMentionWidget({ id: "custom", type: "service", label: "Custom" }), false);
  assert.equal(isSavedTurnToolMentionWidget(composerSkillMentionWidget({ id: "skill", label: "Skill" })), false);
});

test("overlapping service and individual mentions produce one target per actual tool", () => {
  const draft = resolveComposerToolMentions("@google_drive @drive_read", [], tools);
  assert.deepEqual(draft.include, [{ kind: "tool", id: "drive_read" }, { kind: "tool", id: "drive_write" }]);
  assert.deepEqual(draft.toolIds, ["drive_read", "drive_write"]);
});

test("reload of a migrated draft consumes old selections before another chat opens", () => {
  const snapshot = { current: ["drive_read"] };
  const completed = ["profile:chat-1"];
  assert.deepEqual(consumeLegacyToolMentionSnapshot(snapshot, "profile:chat-1", completed), []);
  assert.deepEqual(snapshot.current, []);
  const selectedForNextChat = consumeLegacyToolMentionSnapshot(snapshot, "profile:chat-2", completed);
  assert.equal(materializeLegacyToolMentions("Fresh draft", {}, selectedForNextChat, tools).value, "Fresh draft");
});

test("an unmigrated draft consumes the startup snapshot exactly once", () => {
  const snapshot = { current: ["drive_read"] };
  const initial = consumeLegacyToolMentionSnapshot(snapshot, "profile:chat-1", []);
  assert.equal(materializeLegacyToolMentions("Draft", {}, initial, tools).value, "Draft @drive_read ");
  assert.deepEqual(consumeLegacyToolMentionSnapshot(snapshot, "profile:chat-2", []), []);
});

test("service wire shape matches the shared backend compatibility fixture", () => {
  const fixture = JSON.parse(readFileSync(new URL("../../../../../tests/fixtures/composer_service_mentions.json", import.meta.url), "utf8"));
  assert.deepEqual(composerMentionSelectionRequest(
    resolveComposerToolMentions(fixture.text, [], fixture.frontendItems), "auto",
  ), fixture.request);
});
