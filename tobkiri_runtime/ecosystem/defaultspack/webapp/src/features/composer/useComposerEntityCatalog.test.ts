import assert from "node:assert/strict";
import test from "node:test";
import { hasAdmittedComposerMcpRead, loadComposerEntityCatalog } from "./useComposerEntityCatalog";
import type { VerifiedFrontendHost } from "../../host/VerifiedFrontendHostContext";

const host: VerifiedFrontendHost = {
  activePlanHash: "plan",
  capabilities: {
    invokeAction: async () => { throw new Error("No actions in this fixture"); },
    readDataSource: async () => { throw new Error("No reads in this fixture"); },
  },
  catalog: {
    version: "rumi.ui.contribution.v1", profile_id: "local", profile_revision: "revision",
    activation_id: "activation", plan_hash: "plan", selected_entry_route: "/chat",
    catalog_hash: "catalog", contributions: [], diagnostics: [], quarantined_pack_ids: [],
  },
};

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

test("the shipped map and verified Profile do not declare an MCP read", async () => {
  assert.equal(hasAdmittedComposerMcpRead(host, "local"), false);
  assert.equal(hasAdmittedComposerMcpRead(null, "local"), false);
  assert.equal(hasAdmittedComposerMcpRead(host, "other"), false);
  let mcpCalls = 0;
  const readMcp = async () => { mcpCalls++; return { servers: [] }; };
  const result = await loadComposerEntityCatalog("local", async () => historySnapshot(),
    hasAdmittedComposerMcpRead(host, "local") ? readMcp : undefined);
  assert.equal(mcpCalls, 0);
  assert.equal(result.catalog.candidates[0]?.id, "fixture-chat");
  assert.deepEqual(result.servers, []);
  assert.equal(result.status, undefined);
  assert.equal(result.mcpStatus, undefined);
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
