import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { composerMentionSelectionRequest, consumeLegacyToolMentionSnapshot, materializeLegacyToolMentions, replaceComposerToolMentions, resolveComposerToolMentions } from "./composerToolMentions";
import { composerExtensionItems, composerFileMentionWidget, composerServiceMentionWidget, composerSkillMentionWidget, composerToolMentionWidget, reconcileComposerSemanticDraft } from "./composerWidgets";
import { anchorComposerMentionWidget, confirmedComposerMentionRange, updateConfirmedComposerWidgets } from "./composerMentionAnchors";
import type { ComposerExtensionItem } from "../renderers/types";

const tools: ComposerExtensionItem[] = [
  { id: "drive_read", label: "Drive Read", ui: { group_id: "google_drive", group_label: "Google Drive", service_id: "google_drive" } },
  { id: "drive_write", label: "Drive Write", ui: { group_id: "google_drive", group_label: "Google Drive", service_id: "google_drive" } },
  { id: "browser_open", label: "Browser Open", ui: { group_id: "browser", service_id: "browser" } },
  { id: "not_implemented", label: "Unavailable", disabled: true, ui: { group_id: "google_drive", service_id: "google_drive" } },
];

test("typing and plain text paste never confirm tool or service selections", () => {
  const draft = resolveComposerToolMentions("@drive_read @google_drive @-browser", [], tools);
  assert.deepEqual(draft, { include: [], exclude: [], toolIds: [], widgets: [] });
});

test("confirmed tool selection produces the visible turn selection and deletion clears it", () => {
  const draft = resolveComposerToolMentions("Please @drive_read", [composerToolMentionWidget(tools[0], "@drive_read")], tools);
  assert.deepEqual(draft.toolIds, ["drive_read"]);
  assert.equal(draft.widgets[0].metadata?.source, "composer_at_mention");
  assert.deepEqual(composerMentionSelectionRequest(draft, "auto"), {
    mode: "manual", include: [{ kind: "tool", id: "drive_read" }], exclude: [], scope: "turn", must_use: true,
  });
  assert.deepEqual(resolveComposerToolMentions("Please", draft.widgets, tools).toolIds, []);
});

test("repeated tool and service anchors remain visible while wire selections stay unique", () => {
  for (const widget of [composerToolMentionWidget(tools[0]), composerServiceMentionWidget({ id: "google_drive", label: "Google Drive", toolIds: ["drive_read", "drive_write"] })]) {
    const syntax = String((widget.metadata?.mention as Record<string, unknown>).syntax);
    const text = `${syntax} ${syntax} ${syntax}`;
    const anchors = [0, syntax.length + 1].map((start) => anchorComposerMentionWidget(widget, text, start));
    const draft = resolveComposerToolMentions(text, [...anchors, anchors[0]], tools);
    assert.equal(draft.widgets.length, 2);
    assert.deepEqual(draft.widgets.map((item) => confirmedComposerMentionRange(item, text)?.start), [0, syntax.length + 1]);
    assert.equal(new Set(draft.toolIds).size, draft.toolIds.length);
    assert.equal(draft.include.length, widget.type === "tool" ? 1 : 2);
    assert.deepEqual(draft.widgets.map((item) => item.id), [widget.id, widget.id]);
  }
});

test("service selection expands available tools and service exclusion stays visible", () => {
  const draft = resolveComposerToolMentions("@google_drive @-browser", materializeLegacyToolMentions("", { include: [{ kind: "service", id: "google_drive" }], exclude: [{ kind: "service", id: "browser" }] }, [], tools).widgets.map((widget) => anchorComposerMentionWidget(widget, "@google_drive @-browser", "@google_drive @-browser".indexOf(String((widget.metadata?.mention as Record<string, unknown>).syntax)))), tools);
  assert.deepEqual(draft.toolIds, ["drive_read", "drive_write"]);
  assert.deepEqual(draft.include, [{ kind: "tool", id: "drive_read" }, { kind: "tool", id: "drive_write" }]);
  assert.deepEqual(draft.exclude, [{ kind: "service", id: "browser" }]);
  assert.equal(draft.widgets[1].id, "exclude:mention-service:browser");
  assert.deepEqual(resolveComposerToolMentions("@google_drive", draft.widgets, tools).exclude, []);
});

test("exclusion wins a conflicting mention and none mode emits no includes", () => {
  const draft = resolveComposerToolMentions("@drive_read @-google_drive", materializeLegacyToolMentions("", { exclude: [{ kind: "service", id: "google_drive" }] }, ["drive_read"], tools).widgets.map((widget) => anchorComposerMentionWidget(widget, "@drive_read @-google_drive", "@drive_read @-google_drive".indexOf(String((widget.metadata?.mention as Record<string, unknown>).syntax)))), tools);
  assert.deepEqual(draft.toolIds, []);
  assert.deepEqual(composerMentionSelectionRequest(draft, "none").include, []);
  assert.deepEqual(resolveComposerToolMentions("@drive_read", updateConfirmedComposerWidgets("@drive_read @-google_drive", "@drive_read", draft.widgets, { start: 11, end: 27 }), tools).toolIds, ["drive_read"]);
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
  assert.deepEqual(resolveComposerToolMentions("@coding/files/read", [composerServiceMentionWidget({ id: "coding/files/read", label: "coding/files/read", toolIds: ["read_file"] })], items).include, [{ kind: "tool", id: "read_file" }]);
  assert.deepEqual(resolveComposerToolMentions("@files", [composerServiceMentionWidget({ id: "files", label: "files", toolIds: ["read_file"] })], items).include, [{ kind: "tool", id: "read_file" }]);
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
  const draft = resolveComposerToolMentions("@-browser", materializeLegacyToolMentions("", { exclude: [{ kind: "service", id: "browser" }] }, [], tools).widgets.map((widget) => anchorComposerMentionWidget(widget, "@-browser", "@-browser".indexOf(String((widget.metadata?.mention as Record<string, unknown>).syntax)))), tools);
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
  const original = resolveComposerToolMentions("Find @-browser", materializeLegacyToolMentions("", { exclude: [{ kind: "service", id: "browser" }] }, [], tools).widgets.map((widget) => anchorComposerMentionWidget(widget, "Find @-browser", "Find @-browser".indexOf(String((widget.metadata?.mention as Record<string, unknown>).syntax)))), tools);
  const enabled = replaceComposerToolMentions("Find @-browser", original.widgets, tools, ["browser_open"]);
  assert.equal(enabled.value, "Find @browser_open ");
  const draft = resolveComposerToolMentions(enabled.value, enabled.widgets, tools);
  assert.deepEqual(draft.toolIds, ["browser_open"]);
  assert.deepEqual(draft.exclude, []);
});


test("semantic ownership does not confirm longer raw mentions", () => {
  const items = [{ id: "a", label: "A" }, { id: "ab", label: "AB" }];
  const draft = resolveComposerToolMentions("@A @AB", [composerToolMentionWidget(items[0])], items);
  assert.deepEqual(draft.toolIds, ["a"]);
});

test("service mentions remain compatible with saved submission without opening custom widget context", async () => {
  const { isSavedTurnToolMentionWidget } = await import("./composerToolMentions");
  const draft = resolveComposerToolMentions("@google_drive @-browser", materializeLegacyToolMentions("", { include: [{ kind: "service", id: "google_drive" }], exclude: [{ kind: "service", id: "browser" }] }, [], tools).widgets.map((widget) => anchorComposerMentionWidget(widget, "@google_drive @-browser", "@google_drive @-browser".indexOf(String((widget.metadata?.mention as Record<string, unknown>).syntax)))), tools);
  assert.ok(draft.widgets.every(isSavedTurnToolMentionWidget));
  assert.equal(isSavedTurnToolMentionWidget({ id: "custom", type: "service", label: "Custom" }), false);
  assert.equal(isSavedTurnToolMentionWidget(composerSkillMentionWidget({ id: "skill", label: "Skill" })), false);
});

test("overlapping service and individual mentions produce one target per actual tool", () => {
  const draft = resolveComposerToolMentions("@google_drive @drive_read", materializeLegacyToolMentions("", { include: [{ kind: "service", id: "google_drive" }] }, ["drive_read"], tools).widgets.map((widget) => anchorComposerMentionWidget(widget, "@google_drive @drive_read", "@google_drive @drive_read".indexOf(String((widget.metadata?.mention as Record<string, unknown>).syntax)))), tools);
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
    resolveComposerToolMentions(fixture.text, [composerServiceMentionWidget({ id: "github", label: "github", toolIds: fixture.frontendItems.map((item: ComposerExtensionItem) => item.id) })], fixture.frontendItems), "auto",
  ), fixture.request);
});


test("confirmed tools are rejected when unavailable, disabled, or edited away", () => {
  const widget = composerToolMentionWidget(tools[0], "@drive_read");
  assert.deepEqual(resolveComposerToolMentions("@drive_read", [widget], []).toolIds, []);
  assert.deepEqual(resolveComposerToolMentions("@drive_read", [{ ...widget, enabled: false }], tools).toolIds, []);
  assert.deepEqual(resolveComposerToolMentions("\\@drive_read", [widget], tools).toolIds, []);
  assert.deepEqual(resolveComposerToolMentions("@drive_read_more", [widget], tools).toolIds, []);
});

test("sidebar changes preserve unconfirmed @ prose while confirming selected ids", () => {
  const next = replaceComposerToolMentions("Explain @drive_read", [], tools, ["browser_open"]);
  assert.equal(next.value, "Explain @drive_read @browser_open ");
  assert.deepEqual(resolveComposerToolMentions(next.value, next.widgets, tools).toolIds, ["browser_open"]);
});


test("sidebar removal deletes only the confirmed duplicate and moves retained skill anchors", () => {
  const text = "@Drive Read @Drive Read @Settings";
  const widget = anchorComposerMentionWidget(composerToolMentionWidget(tools[0]), text, 0);
  const skill = anchorComposerMentionWidget(composerSkillMentionWidget({ id: "settings", label: "Settings" }), text, 24);
  const next = replaceComposerToolMentions(text, [widget, skill], tools, ["browser_open"]);
  assert.equal(next.value, " @Drive Read @Settings @browser_open ");
  assert.deepEqual(resolveComposerToolMentions(next.value, next.widgets, tools).toolIds, ["browser_open"]);
  assert.equal(confirmedComposerMentionRange(next.widgets.find((item) => item.type === "skill")!, next.value)?.start, 13);
});

test("a stale anchor cannot activate another identical typed tool occurrence", () => {
  const widget = anchorComposerMentionWidget(composerToolMentionWidget(tools[0]), "@Drive Read @Drive Read", 0);
  assert.deepEqual(resolveComposerToolMentions("@Drive Read", [widget], tools).toolIds, []);
});

test("confirmed MCP server resolves only authenticated enabled inspected tool ids", () => {
  const widget = composerServiceMentionWidget({ id: "exact-server", label: "mcp:exact-server", toolIds: ["drive_read", "not_implemented", "invented"] });
  widget.metadata = { ...widget.metadata, mention: { ...(widget.metadata?.mention as object), kind: "mcp", id: "exact-server", syntax: "@mcp:exact-server" } };
  const text = "Use @mcp:exact-server";
  const anchored = anchorComposerMentionWidget(widget, text, 4);
  const draft = resolveComposerToolMentions(text, [anchored], tools);
  assert.deepEqual(draft.toolIds, ["drive_read"]);
  assert.deepEqual(composerMentionSelectionRequest(draft, "auto"), {
    mode: "manual", include: [{ kind: "tool", id: "drive_read" }], exclude: [], scope: "turn", must_use: true,
  });
  assert.deepEqual(resolveComposerToolMentions(text, [], tools).toolIds, []);
  const disconnected = { ...anchored, metadata: { ...anchored.metadata, service: { tool_ids: [] } } };
  assert.deepEqual(resolveComposerToolMentions(text, [disconnected], tools).toolIds, []);
});

test("new draft clearing confirmed widgets retains one migrated mention without tool authority", () => {
  const snapshot = { current: ["drive_read"] };
  const selected = consumeLegacyToolMentionSnapshot(snapshot, "profile:chat-1", []);
  const migrated = materializeLegacyToolMentions("", {}, selected, tools);
  assert.equal(migrated.value, "@drive_read ");
  assert.deepEqual(resolveComposerToolMentions(migrated.value, migrated.widgets, tools).toolIds, ["drive_read"]);
  const completed = ["profile:chat-1"];
  for (const key of ["profile:draft", "profile:draft"]) {
    const reset = materializeLegacyToolMentions(migrated.value, {}, consumeLegacyToolMentionSnapshot(snapshot, key, completed), tools);
    assert.equal(reset.value, "@drive_read ");
    assert.deepEqual(resolveComposerToolMentions(reset.value, reset.widgets, tools), {
      include: [], exclude: [], toolIds: [], widgets: [],
    });
    completed.push(key);
  }
});
