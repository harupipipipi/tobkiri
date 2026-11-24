import test from "node:test";
import assert from "node:assert/strict";

import type { ChatMessage } from "./api";
import { fileEditTimelineFromMessages, isHumanOperatorCanvasPreview, toolPreviewsFromMessages } from "./toolPreviews";

const PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgo=";

function committedEditReceipt(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1, receipt_id: "file-edit:test-a", file_id: "file-a",
    status: "committed", operation: "patch", profile_id: "profile-a",
    workspace_id: "workspace-a", root_id: "root-a", frame_id: null,
    path: "src/a.ts", previous_path: null, occurred_at_ms: 1000,
    sequence: 1, version: "sha256:test-a",
    stats: { status: "available", lines_added: 2, lines_deleted: 1 },
    ...overrides,
  };
}

function contractTarget(url: string): string {
  const marker = "/api/contracts/defaultspack/";
  if (!url.includes(marker)) return url;
  return decodeURIComponent(url.slice(url.indexOf(marker) + marker.length));
}

function assistantMessage(patch: Partial<ChatMessage>): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: [],
    raw_text: "",
    created_at: 1000,
    conversation_id: "c1",
    parent_id: "u1",
    children_ids: [],
    sequence_number: 2,
    finish_reason: "stop",
    usage: null,
    widget: null,
    metadata: null,
    events: [],
    tool_logs: [],
    model: "test/model",
    ...patch,
  };
}

test("tool previews do not render raw tool log files", () => {
  const message = assistantMessage({
    tool_logs: [
      {
        tool_name: "computer_use",
        arguments: { action: "context" },
        result: {
          status: "ok",
          data: {
            result: "computer_use computer.context completed",
            is_error: false,
            widget: { type: "browser_computer" },
          },
        },
      },
    ],
  });

  assert.deepEqual(toolPreviewsFromMessages([message]), []);
});

test("tool previews keep browser/computer visual artifacts", () => {
  const message = assistantMessage({
    tool_logs: [
      {
        tool_name: "computer_use",
        tool_call_id: "call_1",
        arguments: { action: "click" },
        result: {
          status: "ok",
          data: {
            widget: {
              type: "browser_computer",
              visual_feedback: {
                data_url: PNG_DATA_URL,
                model_image_path: "/tmp/post-click-model.png",
              },
            },
          },
        },
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]);

  assert.equal(previews.some((preview) => preview.data.type === "file" && preview.data.filename?.endsWith(".tool")), false);
  assert.equal(previews.some((preview) => preview.data.type === "image" && preview.data.url === PNG_DATA_URL), true);
  assert.equal(previews.some((preview) => preview.data.type === "image" && preview.data.path === "/tmp/post-click-model.png"), true);
});

test("tool previews open html artifacts as real file previews without placeholder content", () => {
  const message = assistantMessage({
    tool_logs: [
      {
        tool_name: "coding_file_create",
        tool_call_id: "call_html",
        arguments: { path: "index.html" },
        result: {
          status: "ok",
          data: {
            path: "index.html",
            created: true,
          },
        },
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]);
  const html = previews.find((preview) => preview.data.type === "file" && preview.data.filename === "index.html");

  assert.equal(html?.data.type, "file");
  if (html?.data.type === "file") {
    assert.equal(html.data.mimeType, "text/html");
    assert.equal(html.data.content, undefined);
    assert.match(html.data.url ?? "", /artifact-file/);
  }
});

test("tool previews prefer conversation workspace artifact paths", () => {
  const message = assistantMessage({
    tool_logs: [
      {
        tool_name: "chart_create",
        arguments: { output_path: "charts/revenue.png" },
        result: {
          status: "ok",
          data: {
            path: "charts/revenue.png",
            workspace_path: "artifacts/charts/revenue.png",
          },
        },
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]);

  assert.equal(previews.length, 1);
  assert.equal(previews[0]?.data.type, "image");
  if (previews[0]?.data.type === "image") {
    assert.equal(previews[0].data.path, "artifacts/charts/revenue.png");
    assert.match(contractTarget(previews[0].data.url), /path=artifacts%2Fcharts%2Frevenue\.png/);
  }
});

test("tool previews ignore approval-required artifacts until the tool really executes", () => {
  const message = assistantMessage({
    tool_logs: [
      {
        tool_name: "coding_file_create",
        tool_call_id: "call_pending",
        arguments: { path: "pending.html" },
        result: {
          status: "ok",
          data: {
            path: "pending.html",
            result: "Tool 'coding_file_create' requires approval",
            is_error: false,
            widget: {
              type: "approval_request",
              approval_required: true,
              requires_approval: true,
              approval_request_id: "apr_1",
            },
          },
        },
      },
    ],
    events: [
      {
        type: "tool_call_completed",
        tool_name: "coding_file_create",
        tool_call_id: "call_pending_event",
        result: {
          status: "approval_required",
          artifact: { path: "pending-event.html" },
        },
      },
    ],
  });

  assert.deepEqual(toolPreviewsFromMessages([message]), []);
});

test("tool previews include opened localhost urls", () => {
  const message = assistantMessage({
    events: [
      {
        type: "tool_call_completed",
        tool_name: "browser_use",
        tool_call_id: "call_url",
        result: {
          status: "ok",
          data: {
            url: "http://127.0.0.1:5173/",
          },
        },
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]);

  assert.equal(previews.some((preview) => preview.data.type === "web" && preview.data.url === "http://127.0.0.1:5173/"), true);
});

test("tool previews ignore failed tool artifacts and generic remote hrefs", () => {
  const message = assistantMessage({
    tool_logs: [
      {
        tool_name: "coding_file_create",
        tool_call_id: "failed_call",
        result: {
          status: "error",
          data: {
            path: "broken.html",
            url: "http://127.0.0.1:5173/broken",
          },
        },
      },
    ],
    events: [
      {
        type: "browser_dom_snapshot",
        tool_name: "browser_use",
        dom_snapshot: {
          href: "https://example.com/noisy-link",
          children: [{ href: "http://127.0.0.1:5173/from-href" }],
        },
      },
    ],
  });

  assert.deepEqual(toolPreviewsFromMessages([message]), []);
});

test("tool previews dedupe normalized localhost urls", () => {
  const message = assistantMessage({
    events: [
      {
        type: "tool_call_completed",
        tool_name: "browser_use",
        tool_call_id: "call_url_1",
        result: { url: "http://localhost:5173/app#one" },
      },
      {
        type: "tool_call_completed",
        tool_name: "browser_use",
        tool_call_id: "call_url_2",
        result: { page_url: "http://localhost:5173/app#two" },
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]).filter((preview) => preview.data.type === "web");

  assert.equal(previews.length, 1);
  assert.equal(previews[0]?.data.type, "web");
  if (previews[0]?.data.type === "web") {
    assert.equal(previews[0].data.url, "http://localhost:5173/app");
  }
});

test("human operator canvas previews are detected from local session routes", () => {
  const previews = toolPreviewsFromMessages([
    assistantMessage({
      tool_logs: [
        {
          tool_name: "human_operator_canvas_open",
          tool_call_id: "call_human_operator",
          result: {
            status: "ok",
            data: {
              local_url: "http://127.0.0.1:8766/api/human-operator/conversations/c1/sessions/humanop_test",
            },
          },
        },
      ],
    }),
  ]);

  assert.equal(previews.length, 1);
  assert.equal(isHumanOperatorCanvasPreview(previews[0]), true);
});

test("edit timeline rejects failed output, array results and pending saved logs", () => {
  const receipt = committedEditReceipt();
  for (const wrapper of [
    { output: { is_error: true } },
    { result: [{ is_error: true }] },
    { status: "pending" },
  ]) {
    const saved = assistantMessage({ tool_logs: [{
      tool_name: "coding_file_write", file_edit_receipt: receipt, ...wrapper,
    }] });
    const streamed = assistantMessage({ events: [{
      type: "tool_call_completed", tool_name: "coding_file_write",
      file_edit_receipt: receipt, ...wrapper,
    }] });
    assert.deepEqual(fileEditTimelineFromMessages([saved]), [], JSON.stringify(wrapper));
    assert.deepEqual(fileEditTimelineFromMessages([streamed]), [], JSON.stringify(wrapper));
  }
});

test("edit timeline rejects failure and unfinished flags through nested envelopes", () => {
  const rejected = [
    ...["failed", "denied", "error", "cancelled", "pending", "running",
      "started", "queued", "in_progress", "pending_approval", "awaiting_approval",
      "waiting_for_approval"].map((status) => ({ status })),
    { is_error: true }, { isError: true }, { ok: false }, { success: false },
    { error: true }, { failed: true }, { denied: true }, { cancelled: true },
    { canceled: true }, { approval_required: true }, { requires_approval: true },
    { phase: "tool_call_started" }, { state: "running" }, { outcome: "denied" },
    { status: "ok", phase: "pending_approval" },
  ];
  for (const flag of rejected) {
    const nested = { data: [{ result: [{ output: [{ widget: flag }] }] }] };
    for (const wrapper of [flag, nested]) {
      const message = assistantMessage({
        tool_logs: [{ tool_name: "coding_file_patch", file_edit_receipt: committedEditReceipt(), ...wrapper }],
        events: [{ type: "tool_call_completed", tool_name: "coding_file_patch",
          file_edit_receipt: committedEditReceipt(), ...wrapper }],
      });
      assert.deepEqual(fileEditTimelineFromMessages([message]), [], JSON.stringify(wrapper));
    }
  }
});

test("edit timeline never derives confirmation from read tools, raw diffs or nested receipts", () => {
  const raw = {
    path: "src/a.ts", diff: "@@ -1 +1 @@\n-old\n+new",
    lines_added: 1, lines_deleted: 1, file_edit_receipt: committedEditReceipt(),
  };
  const message = assistantMessage({
    tool_logs: ["read", "list_modules", "coding_file_patch"].map((tool_name) => ({
      tool_name, arguments: raw, result: { status: "ok", ...raw },
    })),
    events: [
      { type: "tool_call_completed", tool_name: "coding_file_patch", result: raw },
      { type: "tool_call_started", file_edit_receipt: committedEditReceipt() },
      { type: "tool_call", phase: "tool_result", file_edit_receipt: committedEditReceipt() },
      { type: "tool_call_completed", file_edit_receipt: committedEditReceipt({ operation: "read" }) },
      { type: "tool_call_completed", file_edit_receipt: committedEditReceipt({ operation: "list_modules" }) },
      { type: "tool_call_completed", file_edit_receipt: committedEditReceipt({ status: "pending" }) },
      { type: "tool_call_completed", file_edit_receipt: committedEditReceipt({ status: "failed" }) },
    ],
  });
  assert.deepEqual(fileEditTimelineFromMessages([message]), []);
});

test("edit timeline deduplicates saved and streamed receipts while discarding conflicts", () => {
  const receipt = committedEditReceipt();
  const message = assistantMessage({
    tool_logs: [{ tool_name: "coding_file_write", file_edit_receipt: receipt }],
    events: [{ type: "tool_call_completed", file_edit_receipt: receipt }],
  });
  assert.equal(fileEditTimelineFromMessages([message], "profile-a").length, 1);
  const contradiction = assistantMessage({ events: [{ type: "tool_result",
    file_edit_receipt: committedEditReceipt({ version: "sha256:changed" }),
  }] });
  assert.deepEqual(fileEditTimelineFromMessages([message, contradiction]), []);
  assert.deepEqual(fileEditTimelineFromMessages([contradiction, message]), []);
});

test("edit timeline filters profile after conflict rejection and preserves workspace frame identity", () => {
  const receipt = committedEditReceipt();
  const foreign = committedEditReceipt({ receipt_id: "file-edit:foreign", profile_id: "other" });
  const otherWorkspace = committedEditReceipt({ receipt_id: "file-edit:workspace", workspace_id: "other-workspace", root_id: "other-root", frame_id: "frame-a" });
  const otherFrame = committedEditReceipt({ receipt_id: "file-edit:frame", frame_id: "frame-b" });
  const message = assistantMessage({ tool_logs: [receipt, foreign, otherWorkspace, otherFrame].map((file_edit_receipt) => ({ file_edit_receipt })) });
  const entries = fileEditTimelineFromMessages([message], "profile-a");
  assert.equal(entries.length, 3);
  assert.ok(entries.some((entry) => entry.workspaceId === "other-workspace" && entry.rootId === "other-root" && entry.frameId === "frame-a"));
  assert.ok(entries.some((entry) => entry.frameId === "frame-b"));
  assert.equal(fileEditTimelineFromMessages([message]).length, 4);
  const conflictingForeign = assistantMessage({ tool_logs: [{ file_edit_receipt:
    committedEditReceipt({ profile_id: "other" }),
  }] });
  assert.equal(fileEditTimelineFromMessages([message, conflictingForeign], "profile-a").length, 2);
});
