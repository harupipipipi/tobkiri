import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { loadComposerEntityCatalog, useComposerEntityCatalog } from "./useComposerEntityCatalog";
import { api } from "../../lib/api";
import { anchorComposerMentionWidget } from "../../lib/composerMentionAnchors";
import { composerToolMentionWidget, filterComposerToolMentions } from "../../lib/composerWidgets";
import { resolveComposerToolMentions } from "../../lib/composerToolMentions";

function historySnapshot() {
  const now = Date.now();
  return {
    kind: "tobkiri.chat.reference.snapshot.v1", profile_id: "local",
    store_revision: 1, project_revision: 1, snapshot_time: now,
    expires_at: now + 600_000, next_cursor: null, truncated: false,
    references: [{ kind: "chat", id: "fixture-chat", label: "Fixture chat",
      conversation_ids: ["fixture-chat"], snapshot_digest: `sha256:${"a".repeat(64)}`,
      member_count: 1, membership_complete: true }],
  };
}

test("formally unavailable MCP connections stay optional and make zero legacy list calls", async () => {
  let mcpCalls = 0;
  const originalRead = api.listMcpServers;
  api.listMcpServers = async () => { mcpCalls++; throw new Error("Unadmitted legacy read"); };
  try {
    const result = await loadComposerEntityCatalog("local", async () => historySnapshot());
    assert.equal(mcpCalls, 0);
    assert.equal(result.catalog.candidates[0]?.id, "fixture-chat");
    assert.deepEqual(result.servers, []);
    assert.equal(result.status, undefined);
    assert.equal(result.mcpStatus, "MCPの接続先は現在参照できません。");
    const tools = [{ id: "registered_mcp_read", label: "Registered MCP read", tags: ["mcp"] }];
    assert.deepEqual(filterComposerToolMentions(tools, "MCP"), tools);
    const input = "@Registered MCP read";
    const widget = anchorComposerMentionWidget(composerToolMentionWidget(tools[0]), input, 0);
    const draft = resolveComposerToolMentions(input, [widget], tools);
    assert.deepEqual(draft.toolIds, ["registered_mcp_read"]);
    assert.deepEqual(draft.include, [{ kind: "tool", id: "registered_mcp_read" }]);
    assert.equal(draft.widgets.length, 1);
  } finally { api.listMcpServers = originalRead; }
});

test("a client-supplied available MCP candidate cannot create an unprovided catalog read", async () => {
  let confirm!: ReturnType<typeof useComposerEntityCatalog>["confirm"];
  let listCalls = 0;
  const originalRead = api.listMcpServers;
  api.listMcpServers = async () => { listCalls++; throw new Error("Unadmitted legacy read"); };
  function Fixture() {
    confirm = useComposerEntityCatalog({ profileId: "local", tools: [], enabled: true }).confirm;
    return null;
  }
  try {
    renderToStaticMarkup(createElement(Fixture));
    await assert.rejects(confirm({ kind: "mcp", id: "unprovided", label: "MCP",
      syntax: "@mcp:unprovided", available: true, status: "connected", toolIds: ["registered_mcp_read"] }), { message: "MCPの接続先は現在参照できません。" });
    assert.equal(listCalls, 0);
  } finally { api.listMcpServers = originalRead; }
});

test("an optional MCP failure cannot turn a successful history read into failure", async () => {
  const result = await loadComposerEntityCatalog("local", async () => historySnapshot(),
    async () => { throw new Error("Optional read failed"); });
  assert.equal(result.catalog.candidates[0]?.id, "fixture-chat");
  assert.equal(result.status, undefined);
  assert.ok(result.mcpStatus);
  assert.deepEqual(result.servers, []);
});

test("history failure remains explicit and creates no reference candidates", async () => {
  const result = await loadComposerEntityCatalog("local", async () => {
    throw new Error("History read failed");
  });
  assert.ok(result.status);
  assert.deepEqual(result.catalog.candidates, []);
  assert.deepEqual(result.catalog.references, []);
  assert.deepEqual(result.servers, []);
});

test("malformed optional MCP data cannot supply candidates or hide history failure", async () => {
  for (const malformed of [null, {}, { servers: null }]) {
    const result = await loadComposerEntityCatalog("local", async () => { throw new Error("History failed"); },
      async () => malformed);
    assert.ok(result.status);
    assert.ok(result.mcpStatus);
    assert.deepEqual(result.catalog.candidates, []);
    assert.deepEqual(result.servers, []);
  }
});
