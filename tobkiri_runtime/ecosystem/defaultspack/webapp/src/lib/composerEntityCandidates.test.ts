import test from "node:test";
import assert from "node:assert/strict";
import { buildComposerHistoryCandidates, buildComposerMcpCandidates, composerMcpMentionWidget } from "./composerEntityCandidates";

const tools = [{ id: "custom_read", label: "Read" }, { id: "custom_write", label: "Write", disabled: true }];

test("history candidates preserve exact kind IDs and isolate profiles despite label collisions", () => {
  const refs = [
    { kind: "chat" as const, id: "same", label: "Shared", profileId: "one", syntax: "@chat:same" },
    { kind: "group" as const, id: "same", label: "Shared", profileId: "one", syntax: "@group:same" },
    { kind: "chat" as const, id: "other", label: "Shared", profileId: "two", syntax: "@chat:other" },
  ];
  assert.deepEqual(buildComposerHistoryCandidates([...refs, refs[0]], "one"), refs.slice(0, 2).map((ref) => ({ ...ref, available: true })));
  assert.deepEqual(buildComposerHistoryCandidates([{ ...refs[0], syntax: "@Shared" }], "one"), []);
});

test("MCP uses inspect registration IDs including custom prefixes and actual server ID", () => {
  const [candidate] = buildComposerMcpCandidates([{ server_id: "real-id", name: "Display", connected: true, status: "connected",
    tools: ["raw_read"], inspect: { tools: ["custom_read", "custom_write", "unknown"] } }], tools);
  assert.deepEqual(candidate, { kind: "mcp", id: "real-id", label: "Display", syntax: "@mcp:real-id", status: "connected", available: true, toolIds: ["custom_read"] });
  const widget = composerMcpMentionWidget(candidate)!;
  assert.equal(widget.metadata?.source, "composer_at_mention");
  assert.deepEqual(widget.metadata?.service, { id: "real-id", label: "Display", status: "connected", tool_ids: ["custom_read"] });
  assert.equal((widget.metadata?.mention as { kind: string }).kind, "mcp");
});

test("MCP raw tool names cannot manufacture membership", () => {
  const [candidate] = buildComposerMcpCandidates([{ server_id: "server", connected: true, status: "connected", tools: ["custom_read"] }], tools);
  assert.equal(candidate.available, false);
  assert.deepEqual(candidate.toolIds, []);
  assert.equal(composerMcpMentionWidget(candidate), null);
});

test("disconnected and disabled servers remain visible without selectable execution", () => {
  for (const server of [
    { server_id: "one", connected: false, status: "connected" },
    { server_id: "two", connected: true, status: "disabled" },
    { server_id: "three", status: "connected" },
  ]) {
    const [candidate] = buildComposerMcpCandidates([{ ...server, inspect: { tools: ["custom_read"] } }], tools);
    assert.equal(candidate.available, false);
    assert.equal(composerMcpMentionWidget(candidate), null);
  }
});

test("history candidate IDs accept canonical 256-character tags and reject 257", () => {
  const id = `tag:${"a".repeat(252)}`;
  const candidate = { kind: "group" as const, id, label: "Tag", profileId: "one", syntax: `@group:${id}` };
  assert.equal(id.length, 256);
  assert.equal(buildComposerHistoryCandidates([candidate], "one").length, 1);
  assert.deepEqual(buildComposerHistoryCandidates([{ ...candidate, id: `${id}a`, syntax: `@group:${id}a` }], "one"), []);
});
