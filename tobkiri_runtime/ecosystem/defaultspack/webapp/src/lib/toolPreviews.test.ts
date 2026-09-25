import test from "node:test";
import assert from "node:assert/strict";

import type { ChatMessage } from "./api";
import { isHumanOperatorCanvasPreview, toolPreviewsFromMessages } from "./toolPreviews";

const PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgo=";

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

test("tool previews include activity detail for completed calculator events without artifacts", () => {
  const message = assistantMessage({
    events: [
      {
        type: "tool_call_completed",
        phase: "tool_call_completed",
        tool_name: "calculator",
        tool_call_id: "call_calc",
        arguments: { expression: "2 + 2" },
        display_text: "2 + 2 = 4",
        result: { status: "ok", data: { result: 4 } },
        timestamp: 2_000,
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]);
  const detail = previews.find((preview) => preview.toolStepId === "call_calc");

  assert.equal(previews.length, 1);
  assert.equal(detail?.data.type, "file");
  if (detail?.data.type === "file") {
    assert.equal(detail.data.filename, "calculator.activity.md");
    assert.match(detail.data.content ?? "", /Tool activity detail/);
    assert.match(detail.data.content ?? "", /2 \+ 2/);
    assert.match(detail.data.content ?? "", /"result": 4/);
  }
});

test("activity detail previews are bounded and use safe filenames", () => {
  const message = assistantMessage({
    events: [
      {
        type: "tool_call_completed",
        tool_name: "../../text-only tool",
        tool_call_id: "call_large",
        result: { status: "ok", data: { output: "x".repeat(10_000) } },
      },
    ],
  });

  const [detail] = toolPreviewsFromMessages([message]);

  assert.equal(detail?.data.type, "file");
  if (detail?.data.type === "file") {
    assert.equal(detail.data.filename.startsWith("."), false);
    assert.equal(detail.data.filename.endsWith(".activity.md"), true);
    assert.ok((detail.data.content?.length ?? 0) < 3_000);
    assert.match(detail.data.content ?? "", /\.\.\./);
  }
});

test("artifact previews take precedence over generated activity details", () => {
  const message = assistantMessage({
    events: [
      {
        type: "tool_call_completed",
        tool_name: "report_writer",
        tool_call_id: "call_report",
        display_text: "Report ready",
        result: { status: "ok", path: "reports/final.txt" },
      },
    ],
  });

  const previews = toolPreviewsFromMessages([message]);

  assert.equal(previews.length, 1);
  assert.equal(previews[0]?.data.type, "file");
  if (previews[0]?.data.type === "file") {
    assert.equal(previews[0].data.filename, "final.txt");
    assert.equal(previews[0].data.content, undefined);
  }
});

test("discarded provider attempts never create activity detail previews", () => {
  const message = assistantMessage({
    events: [
      {
        type: "tool_call_completed",
        tool_name: "calculator",
        tool_call_id: "call_discarded",
        provider_attempt_generation: 1,
        provider_attempt_discarded: true,
        display_text: "stale result",
        result: { status: "ok", data: { result: 99 } },
      },
    ],
  });

  assert.deepEqual(toolPreviewsFromMessages([message]), []);
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
