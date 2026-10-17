import test from "node:test";
import assert from "node:assert/strict";

import {
  addChangeRequestComment,
  changeRequestCommitEnabled,
  commitChangeRequest,
  createChangeRequest,
  exportChangeRequestPatch,
  getChangeRequest,
  listChangeRequests,
  runChangeRequestCheck,
  refreshChangeRequest,
  setChangeRequestViewedFile,
  submitChangeRequestDecision,
  updateChangeRequestComment,
} from "./changeRequests";

function routeKey(path: string): string {
  return `/${path}`;
}

function requestTarget(input: RequestInfo | URL): string {
  const raw = String(input);
  const marker = "/api/contracts/defaultspack/";
  const markerIndex = raw.indexOf(marker);
  if (markerIndex < 0) return raw;
  const operation = decodeURIComponent(raw.slice(markerIndex + marker.length));
  const separator = operation.indexOf(" ");
  return separator < 0 ? operation : operation.slice(separator + 1);
}

test("listChangeRequests uses canonical endpoint and normalizes backend snapshots", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl = "";
  let requestCache: RequestCache | undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestCache = init?.cache;
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        change_requests: [
          {
            id: "cr_1",
            status: "open",
            title: "Review workspace",
            file_stats: [
              { path: "notes.md", status: "untracked", additions: 1, deletions: 0 },
            ],
            latest_snapshot: {
              working_tree_hash: "sha256:abc",
              normalized_patch: "diff --git a/notes.md b/notes.md\nnew file mode 100644\n--- /dev/null\n+++ b/notes.md\n@@\n+hello\n",
              file_stats: [
                { path: "notes.md", status: "untracked", additions: 1, deletions: 0 },
                { path: "src/app.ts", status: "modified", additions: 2, deletions: 1 },
              ],
            },
          },
          { id: "cr_2", status: "closed" },
        ],
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await listChangeRequests({ workspace_id: "ws_1" });
    assert.equal(requestUrl, routeKey("api/change-requests?workspace_id=ws_1"));
    assert.equal(requestCache, "no-store");
    assert.equal(result.apiAvailable, true);
    assert.deepEqual(result.open.map((review) => review.id), ["cr_1"]);
    assert.deepEqual(result.closed.map((review) => review.id), ["cr_2"]);
    assert.deepEqual(result.reviews[0]?.files?.map((file) => [file.path, file.status]), [
      ["notes.md", "untracked"],
      ["src/app.ts", "modified"],
    ]);
    assert.equal(result.reviews[0]?.snapshot?.signature, "sha256:abc");
    assert.match(result.reviews[0]?.snapshot?.diff ?? "", /new file mode 100644/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listChangeRequests normalizes persisted summaries without nested snapshots", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      change_requests: [
        {
          id: "cr_summary",
          status: "open",
          working_tree_hash: "sha256:summary",
          normalized_patch: "diff --git a/app.ts b/app.ts\n--- a/app.ts\n+++ b/app.ts\n@@\n-old\n+new\n",
          file_stats: [
            { path: "app.ts", status: "modified", additions: 1, deletions: 1 },
          ],
        },
      ],
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await listChangeRequests();
    assert.deepEqual(result.reviews[0]?.files?.map((file) => [file.path, file.status]), [
      ["app.ts", "modified"],
    ]);
    assert.equal(result.reviews[0]?.snapshot?.signature, "sha256:summary");
    assert.match(result.reviews[0]?.snapshot?.diff ?? "", /diff --git/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("getChangeRequest hydrates detail records and drift state", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl = "";
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    requestUrl = requestTarget(input);
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        id: "cr hydrate",
        status: "open",
        is_stale: true,
        latest_snapshot: {
          working_tree_hash: "sha256:snapshot",
          normalized_patch: "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n@@\n-old\n+new\n",
          file_stats: [{ path: "src/app.ts", status: "modified", additions: 1, deletions: 1 }],
        },
        drift: {
          changed: true,
          previous_working_tree_hash: "sha256:snapshot",
          current_working_tree_hash: "sha256:current",
          changed_paths: ["src/app.ts"],
        },
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await getChangeRequest("cr hydrate");
    assert.equal(requestUrl, routeKey("api/change-requests/cr%20hydrate"));
    assert.equal(result?.is_stale, true);
    assert.equal(result?.drift?.current_working_tree_hash, "sha256:current");
    assert.deepEqual(result?.files?.map((file) => file.path), ["src/app.ts"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("create and refresh change requests use read-only canonical routes", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: requestTarget(input),
      method: init?.method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        change_request: {
          id: calls.length === 1 ? "cr_create" : "cr_refresh",
          status: "open",
        },
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const created = await createChangeRequest({ workspace_id: "ws_1" });
    const refreshed = await refreshChangeRequest("cr create", { workspace_id: "ws_1" });

    assert.equal(created?.id, "cr_create");
    assert.equal(refreshed?.id, "cr_refresh");
    assert.deepEqual(calls, [
      {
        url: routeKey("api/change-requests"),
        method: "POST",
        body: { domain: "change_request", source: "working_tree", workspace_id: "ws_1" },
      },
      {
        url: routeKey("api/change-requests/cr%20create/refresh"),
        method: "POST",
        body: { workspace_id: "ws_1" },
      },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("change request review actions use canonical mutation routes", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; method?: string; body?: unknown }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: requestTarget(input),
      method: init?.method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        change_request: {
          id: "cr_review",
          status: calls.length === 3 ? "approved" : "open",
          decision: calls.length === 3 ? "approved" : "none",
          comments: [{ id: "crc_1", kind: "suggestion", body: "tighten", suggested_patch: "diff --git a/a b/a\n" }],
          viewed_files: { "src/app.ts": { path: "src/app.ts", viewed: true } },
          checks: [{ id: "chk_1", command: "python -m pytest", status: "passed" }],
          suggested_checks: [{ id: "python__m_pytest", command: "python -m pytest" }],
        },
        check: { id: "chk_1", command: "python -m pytest", status: "passed" },
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    await addChangeRequestComment("cr review", { kind: "suggestion", body: "tighten", suggested_patch: "patch" });
    await updateChangeRequestComment("cr review", "crc 1", { resolved: true });
    const decided = await submitChangeRequestDecision("cr review", { decision: "approve" });
    await setChangeRequestViewedFile("cr review", "src/app.ts", true);
    const check = await runChangeRequestCheck("cr review", "python -m pytest");

    assert.equal(decided?.decision, "approved");
    assert.equal(check.check?.status, "passed");
    assert.deepEqual(calls.map((call) => [call.method, call.url]), [
      ["POST", routeKey("api/change-requests/cr%20review/comments")],
      ["PATCH", routeKey("api/change-requests/cr%20review/comments/crc%201")],
      ["POST", routeKey("api/change-requests/cr%20review/decision")],
      ["PATCH", routeKey("api/change-requests/cr%20review/viewed-files")],
      ["POST", routeKey("api/change-requests/cr%20review/checks/run")],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("commitChangeRequest is blocked in the default Phase 1 frontend", async () => {
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    called = true;
    assert.equal(init?.method, "POST");
    assert.deepEqual(JSON.parse(String(init?.body)), { message: "seal commit" });
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        approval_required: true,
        approval_request_id: "approval_1",
        display_summary: "Commit requires approval",
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    assert.equal(changeRequestCommitEnabled, false);
    const result = await commitChangeRequest("cr review", "seal commit");
    assert.equal(called, false);
    assert.equal(result?.blocked, true);
    assert.equal(result?.reason, "phase1_review_only");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("exportChangeRequestPatch uses canonical export route", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl = "";
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    assert.equal(init?.method, "POST");
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        filename: "cr_review.patch",
        patch: "diff --git a/app.ts b/app.ts\n",
        patch_bytes: 31,
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await exportChangeRequestPatch("cr review");
    assert.equal(requestUrl, routeKey("api/change-requests/cr%20review/export-patch"));
    assert.equal(result?.filename, "cr_review.patch");
    assert.match(result?.patch ?? "", /diff --git/);
    assert.equal(result?.patch_bytes, 31);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
