import assert from "node:assert/strict";
import test from "node:test";
import { api } from "./api";
import { loadSavedCodingWorkspace, savedCodingWorkspace } from "./savedCodingWorkspace";

const workspaceId = "project-e54224d7167193cc2ad9a24be61a3b81";
const workspace = {
  workspace_id: workspaceId,
  label: "Project",
  root_path: "/workspace/project",
  trusted: false,
  metadata: { purpose: "test" },
  trust_granted_at: null,
  last_used_at: 1_700_000_000_000,
};

test("saved workspace bootstrap projects exact owner facts without inventing context", () => {
  const original = { workspace: { ...workspace, files: ["unverified"], branch: "unverified" } };
  const result = savedCodingWorkspace(workspaceId, original);
  assert.deepEqual(result, workspace);
  assert.equal(result.trusted, false);
  assert.ok(!("files" in result) && !("git" in result) && !("branch" in result));
  assert.notEqual(result.metadata, original.workspace.metadata);
  assert.deepEqual(original.workspace.files, ["unverified"]);
});

test("saved workspace supports owner platform roots and optional timestamps", () => {
  for (const root_path of ["/repo", "C:\\repo", "\\\\server\\repo"]) {
    const { trust_granted_at: _trust, last_used_at: _used, ...minimal } = workspace;
    assert.deepEqual(savedCodingWorkspace(workspaceId, { workspace: { ...minimal, root_path } }),
      { ...minimal, root_path });
  }
});

test("mismatched owner IDs and malformed mount facts fail closed", () => {
  for (const changes of [
    { workspace_id: "different" }, { label: "" }, { label: 3 },
    { root_path: "relative/path" }, { root_path: "/repo\u0000outside" },
    { trusted: "true" }, { trusted: undefined }, { metadata: [] }, { metadata: null },
    { last_used_at: Number.NaN }, { last_used_at: 1.5 }, { trust_granted_at: true },
  ]) {
    assert.throws(() => savedCodingWorkspace(workspaceId, { workspace: { ...workspace, ...changes } }));
  }
  for (const response of [null, [], {}, { workspace: null }, { workspace: [] }]) {
    assert.throws(() => savedCodingWorkspace(workspaceId, response));
  }
});

test("invalid workspace IDs stop before any read", async () => {
  let reads = 0;
  for (const id of ["", "../outside", "bad id", "a".repeat(129), 123, null]) {
    await assert.rejects(loadSavedCodingWorkspace(id as string, async () => {
      reads += 1;
      return { workspace };
    }));
  }
  assert.equal(reads, 0);
});

test("a rejected owner read is neither retried nor replaced with a fallback mount", async () => {
  let reads = 0;
  const failure = new Error("owner unavailable");
  await assert.rejects(loadSavedCodingWorkspace(workspaceId, async (id) => {
    reads += 1;
    assert.equal(id, workspaceId);
    throw failure;
  }), failure);
  assert.equal(reads, 1);
});

test("saved coding API performs only the existing exact-ID GET and verifies its result", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ operation: string; method: string; cache?: RequestCache; body?: BodyInit | null }> = [];
  let returnedId = workspaceId;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const marker = "/api/contracts/defaultspack/";
    const raw = String(input);
    assert.ok(raw.startsWith(marker));
    calls.push({ operation: decodeURIComponent(raw.slice(marker.length)),
      method: init?.method ?? "GET", cache: init?.cache, body: init?.body });
    return new Response(JSON.stringify({ success: true,
      data: { workspace: { ...workspace, workspace_id: returnedId } },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  try {
    assert.deepEqual(await api.getSavedCodingWorkspace(workspaceId), workspace);
    returnedId = "different";
    await assert.rejects(api.getSavedCodingWorkspace(workspaceId));
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calls, [0, 1].map(() => ({
    operation: `GET /api/coding/workspaces/get?workspace_id=${workspaceId}`,
    method: "GET", cache: "no-store", body: undefined,
  })));
});
