import assert from "node:assert/strict";
import test from "node:test";
import type { DroppedWidget } from "../renderers/types";
import { anchorComposerMentionWidget } from "./composerMentionAnchors";
import { buildComposerMcpCandidates, composerMcpMentionWidget } from "./composerEntityCandidates";
import { ComposerFollowupDraftChangedError, prepareComposerFollowupInput } from "./composerFollowupInput";

const source = "  @chat:one  ";
const history: DroppedWidget = anchorComposerMentionWidget({
  id: "chat:p:one", type: "conversation", widgetKind: "history_context", sourceItemId: "one", label: "One", enabled: true,
  metadata: { source: "composer_at_mention", mention: { kind: "chat", id: "one", profileId: "p", label: "One", memberIds: ["one"], syntax: "@chat:one" },
    history_reference: { schema: "io.tobkiri.history-reference.v1", kind: "chat", profile_id: "p", id: "one" } },
}, source, 2);
const snapshot = () => {
  const now = Date.now();
  return { kind: "tobkiri.chat.reference.snapshot.v1", profile_id: "p", store_revision: 1, project_revision: 1,
    snapshot_time: now, expires_at: now + 599_000, next_cursor: null, truncated: false,
    references: [{ kind: "chat", id: "one", label: "Server label", conversation_ids: ["one"], member_count: 1,
      membership_complete: true, snapshot_digest: `sha256:${"a".repeat(64)}` }] };
};
const input = () => ({ source, submitted: source.trim(), widgets: [history], profileId: "p", tools: [], mode: "auto" as const,
  listMcpServers: async () => ({ servers: [] }), resolveChatReferences: async () => snapshot(), isCurrent: () => true });

test("follow-up input sends confirmed lean IDs without frontend labels or member authority", async () => {
  const result = await prepareComposerFollowupInput(input());
  assert.deepEqual(result.chat_references, [{ kind: "chat", profile_id: "p", id: "one" }]);
  assert.deepEqual(result.tool_selection, { mode: "auto", include: [], exclude: [], scope: "turn", must_use: false });
});

test("plain typed references neither resolve nor become follow-up semantic IDs", async () => {
  const result = await prepareComposerFollowupInput({ ...input(), widgets: [], resolveChatReferences: async () => { throw new Error("unexpected read"); } });
  assert.equal(result.chat_references, undefined);
});

test("a different submitted prompt cannot silently discard confirmed draft references", async () => {
  await assert.rejects(prepareComposerFollowupInput({ ...input(), submitted: "Another prompt" }), /入力と送信内容/);
});

test("missing foreign-profile or changed reference IDs fail admission", async () => {
  for (const raw of [null, { ...snapshot(), profile_id: "foreign" }, { ...snapshot(), references: [] }]) {
    await assert.rejects(prepareComposerFollowupInput({ ...input(), resolveChatReferences: async () => raw }), /参照/);
  }
});

test("an edited draft while history resolves cannot return a follow-up payload", async () => {
  let current = true;
  await assert.rejects(prepareComposerFollowupInput({ ...input(), isCurrent: () => current,
    resolveChatReferences: async () => { current = false; return snapshot(); } }), ComposerFollowupDraftChangedError);
});

test("MCP follow-ups bind current exact available tool IDs and reject disconnected servers", async () => {
  const text = "@mcp:server";
  const tools = [{ id: "live/tool", label: "Live" }, { id: "old/tool", label: "Old" }];
  const servers = [{ server_id: "server", connected: true, status: "connected", inspect: { tools: ["old/tool"] } }];
  const widget = anchorComposerMentionWidget(composerMcpMentionWidget(buildComposerMcpCandidates(servers, tools)[0])!, text, 0);
  const args = { ...input(), source: text, submitted: text, tools, widgets: [widget],
    listMcpServers: async () => ({ servers: [{ ...servers[0], inspect: { tools: ["live/tool"] } }] }) };
  const result = await prepareComposerFollowupInput(args);
  assert.deepEqual(result.tool_selection, { mode: "manual", include: [{ kind: "tool", id: "live/tool" }], exclude: [], scope: "turn", must_use: true });
  await assert.rejects(prepareComposerFollowupInput({ ...args, listMcpServers: async () => ({ servers: [{ ...servers[0], connected: false }] }) }), /MCP/);
});

test("unsupported review or special widgets cannot silently lose their context", async () => {
  await assert.rejects(prepareComposerFollowupInput({ ...input(), mode: "review" }), /機能の確認/);
  await assert.rejects(prepareComposerFollowupInput({ ...input(), widgets: [{ id: "special", type: "custom", label: "Special" }] }), /添付コンテキスト/);
});
