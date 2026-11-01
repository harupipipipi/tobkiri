import test from "node:test";
import assert from "node:assert/strict";
import type { DroppedWidget } from "../renderers/types";
import { anchorComposerMentionWidget } from "./composerMentionAnchors";
import { buildComposerMcpCandidates, composerMcpMentionWidget } from "./composerEntityCandidates";
import { composerReferencePreflightDraftIsCurrent, composerSelectableToolSnapshotKey, isSavedTurnHistoryReferenceWidget, prepareComposerReferenceSubmission, requiresComposerReferenceRefresh } from "./composerReferenceSubmission";

function history(id = "one", profileId = "p", kind: "chat" | "group" = "chat"): DroppedWidget {
  return { id: `${kind}:${profileId}:${id}`, type: kind === "chat" ? "conversation" : "group", widgetKind: "history_context", sourceItemId: id, label: id, enabled: true,
    metadata: { source: "composer_at_mention", mention: { kind, id, profileId, label: id, memberIds: kind === "chat" ? [id] : ["one"], syntax: `@${kind}:${id}` },
      history_reference: { schema: "io.tobkiri.history-reference.v1", kind, profile_id: profileId, id } } };
}
function prepare(source: string, widgets: DroppedWidget[], submitted = source.trim()) {
  return prepareComposerReferenceSubmission({ source, submitted, widgets, profileId: "p", tools: [] });
}
const servers = [{ server_id: "custom", name: "Custom", connected: true, status: "connected", inspect: { tools: ["unrelated/tool", "disabled", "removed"] } }];
const tools = [{ id: "unrelated/tool", label: "Tool" }, { id: "disabled", label: "Disabled", disabled: true }];
function mcp(source: string): DroppedWidget {
  return anchorComposerMentionWidget(composerMcpMentionWidget(buildComposerMcpCandidates(servers, tools)[0])!, source, source.indexOf("@mcp:"));
}

test("trim and escaped slash normalization preserve confirmed history occurrences", () => {
  const source = "  //say @chat:one  ";
  const widget = anchorComposerMentionWidget(history(), source, source.indexOf("@chat:"));
  const result = prepare(source, [widget], "/say @chat:one");
  assert.deepEqual(result.chatReferences, [{ kind: "chat", profile_id: "p", id: "one" }]);
  assert.equal(isSavedTurnHistoryReferenceWidget(result.widgets[0], "p", "/say @chat:one"), true);
});
test("raw typed references and unanchored or disabled widgets do not become semantic", () => {
  assert.deepEqual(prepare("@chat:one", []).chatReferences, []);
  assert.deepEqual(prepare("@chat:one", [history()]).widgets, []);
  const confirmed = anchorComposerMentionWidget(history(), "@chat:one", 0);
  assert.deepEqual(prepare("@chat:one", [{ ...confirmed, enabled: false }]).chatReferences, []);
});
test("duplicate identities resolve once while foreign profile and malformed member snapshots fail", () => {
  const widget = anchorComposerMentionWidget(history(), "@chat:one", 0);
  assert.equal(prepare("@chat:one", [widget, widget]).chatReferences.length, 1);
  assert.throws(() => prepare("@chat:one", [anchorComposerMentionWidget(history("one", "foreign"), "@chat:one", 0)]), /下書きは保持/);
  const malformed = { ...widget, metadata: { ...widget.metadata, mention: { ...(widget.metadata?.mention as object), memberIds: ["other"] } } };
  assert.throws(() => prepare("@chat:one", [malformed]), /下書きは保持/);
});
test("sixteen unique confirmed references are accepted and seventeen fail", () => {
  const source = Array.from({ length: 17 }, (_, i) => `@chat:c${i}`).join(" ");
  const widgets = Array.from({ length: 17 }, (_, i) => anchorComposerMentionWidget(history(`c${i}`), source, source.indexOf(`@chat:c${i}`)));
  assert.equal(prepare(source, widgets.slice(0, 16)).chatReferences.length, 16);
  assert.throws(() => prepare(source, widgets), /下書きは保持/);
});
test("MCP refresh rebuilds exact live registry intersection and preserves confirmation", () => {
  const source = " @mcp:custom ";
  const widget = mcp(source);
  assert.equal(requiresComposerReferenceRefresh([widget], source), true);
  const result = prepareComposerReferenceSubmission({ source, submitted: source.trim(), widgets: [widget], profileId: "p", tools, mcpServers: servers });
  assert.deepEqual((result.widgets[0].metadata?.service as { tool_ids: string[] }).tool_ids, ["unrelated/tool"]);
  assert.equal(requiresComposerReferenceRefresh(result.widgets, source.trim()), true);
  const changed = prepareComposerReferenceSubmission({ source, submitted: source.trim(), widgets: [widget], profileId: "p", tools: [...tools, { id: "new/exact", label: "New" }], mcpServers: [{ ...servers[0], inspect: { tools: ["new/exact"] } }] });
  assert.deepEqual((changed.widgets[0].metadata?.service as { tool_ids: string[] }).tool_ids, ["new/exact"]);
});
test("missing disconnected or empty MCP tool registry rejects without prefix guessing", () => {
  const source = "@mcp:custom";
  for (const mcpServers of [undefined, [], [{ ...servers[0], connected: false }], [{ ...servers[0], inspect: { tools: ["custom/guessed"] } }]]) {
    assert.throws(() => prepareComposerReferenceSubmission({ source, submitted: source, widgets: [mcp(source)], profileId: "p", tools, mcpServers }), /MCP.*下書きは保持/);
  }
  assert.equal(requiresComposerReferenceRefresh([], source), false);
});
test("ordinary widgets pass unchanged and arbitrary custom widgets cannot use history exception", () => {
  const custom = { id: "custom", type: "panel", label: "Custom" };
  assert.equal(prepare("hello", [custom]).widgets[0], custom);
  const widget = anchorComposerMentionWidget(history(), "@chat:one", 0);
  assert.equal(isSavedTurnHistoryReferenceWidget({ ...widget, type: "panel" }, "p", "@chat:one"), false);
  assert.equal(isSavedTurnHistoryReferenceWidget({ ...widget, id: "custom" }, "p", "@chat:one"), false);
  assert.equal(isSavedTurnHistoryReferenceWidget(widget, "foreign", "@chat:one"), false);
});
test("group references retain canonical group identity and reject stale source anchors", () => {
  const source = "@group:team";
  const widget = anchorComposerMentionWidget(history("team", "p", "group"), source, 0);
  const result = prepare(source, [widget]);
  assert.deepEqual(result.chatReferences, [{ kind: "group", profile_id: "p", id: "team" }]);
  assert.equal(isSavedTurnHistoryReferenceWidget(result.widgets[0], "p", source), true);
  assert.deepEqual(prepare(`${source} changed`, [widget]).chatReferences, []);
});

test("empty confirmed backend group remains a valid reference", () => {
  const widget = history("empty", "p", "group");
  (widget.metadata?.mention as { memberIds: string[] }).memberIds = [];
  const source = "@group:empty";
  assert.equal(prepare(source, [anchorComposerMentionWidget(widget, source, 0)]).chatReferences.length, 1);
});

test("preflight waits reject attachment and selectable-tool changes before starting", async () => {
  const widgets: DroppedWidget[] = [];
  const attachedFiles = [{ id: "one" }];
  const captured = { input: "@chat:one", widgets, attachedFiles, profileId: "p", toolSnapshotKey: composerSelectableToolSnapshotKey(tools) };
  for (const change of [
    { attachedFiles: [...attachedFiles, { id: "two" }] },
    { toolSnapshotKey: composerSelectableToolSnapshotKey(tools.map((tool) => ({ ...tool, disabled: true }))) },
  ]) {
    let release!: () => void;
    const response = new Promise<void>((resolve) => { release = resolve; });
    let started = false;
    let current = captured;
    const preflight = (async () => {
      await response;
      if (composerReferencePreflightDraftIsCurrent(captured, current)) started = true;
    })();
    current = { ...captured, ...change };
    release();
    await preflight;
    assert.equal(started, false);
  }
});
test("preflight accepts content-identical catalog rerenders and rejects ownership changes", () => {
  const captured = { input: "draft", widgets: [], attachedFiles: [], profileId: "p", toolSnapshotKey: composerSelectableToolSnapshotKey(tools) };
  assert.equal(composerReferencePreflightDraftIsCurrent(captured, { ...captured, toolSnapshotKey: composerSelectableToolSnapshotKey([...tools].reverse().map((tool) => ({ ...tool }))) }), true);
  for (const change of [{ input: "edited" }, { widgets: [] }, { attachedFiles: [] }, { profileId: "foreign" }]) {
    assert.equal(composerReferencePreflightDraftIsCurrent(captured, { ...captured, ...change }), false);
  }
  assert.equal(composerSelectableToolSnapshotKey([{ id: "one", label: "One" }]), composerSelectableToolSnapshotKey([{ id: "one", label: "Other", disabled: false }]));
  assert.notEqual(composerSelectableToolSnapshotKey([{ id: "one", label: "One" }]), composerSelectableToolSnapshotKey([{ id: "one", label: "One", ui: { service_id: "service" } }]));
  assert.notEqual(composerSelectableToolSnapshotKey([{ id: "one", label: "One" }]), composerSelectableToolSnapshotKey([{ id: "one", label: "One" }, { id: "one", label: "Duplicate" }]));
});

test("catalog snapshots track direct service and source-pack bindings", () => {
  const tool = { id: "one", label: "One", serviceId: "first", sourcePackId: "pack" };
  const key = composerSelectableToolSnapshotKey([tool]);
  assert.notEqual(key, composerSelectableToolSnapshotKey([{ ...tool, serviceId: "second" }]));
  assert.notEqual(key, composerSelectableToolSnapshotKey([{ ...tool, sourcePackId: "other" }]));
  assert.equal(key, composerSelectableToolSnapshotKey([{ id: "one", label: "One", sourcePackId: "pack", ui: { service_id: "first" } }]));
});
