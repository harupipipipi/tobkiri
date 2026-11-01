import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ToolFilterLogWidget, ToolManagerWidget } from "./ToolStatusWidgets";

test("blocked vision tool shows reason in ToolManagerWidget", () => {
  const html = renderToStaticMarkup(
    createElement(ToolManagerWidget, {
      tools: [{ id: "vision_tool", label: "Vision Tool", category: "tool" }],
      disabledToolIds: [],
      hiddenToolIds: [],
      filterEntries: [
        {
          tool_name: "vision_tool",
          status: "blocked",
          reason_code: "model_unsupported",
          required: { model_capabilities: ["model.image_input"] },
        },
      ],
    }),
  );

  assert.match(html, /現在のモデルでは使えません/);
  assert.match(html, /Vision対応モデルに切り替えると使えます/);
});

test("ToolFilterLogWidget shows blocked tools", () => {
  const html = renderToStaticMarkup(
    createElement(ToolFilterLogWidget, {
      entries: [
        {
          tool_name: "vision_tool",
          status: "blocked",
          reason_code: "model_unsupported",
          required: { model_capabilities: ["model.image_input"] },
        },
      ],
    }),
  );

  assert.match(html, /vision_tool/);
  assert.match(html, /現在のモデルでは使えません/);
});

test("ToolFilterLogWidget shows hidden tools as hidden", () => {
  const html = renderToStaticMarkup(
    createElement(ToolFilterLogWidget, {
      entries: [
        {
          tool_name: "secret_tool",
          status: "hidden",
        },
      ],
    }),
  );

  assert.match(html, /secret_tool/);
  assert.match(html, /現在は非表示です/);
  assert.match(html, /現在の表示設定では非表示です/);
});

test("status summary keeps overlapping counts and three-digit values readable", () => {
  const html = renderToStaticMarkup(createElement(ToolManagerWidget, {
    tools: Array.from({ length: 149 }, (_, index) => ({
      id: `tool_${index}`, label: `Tool ${index}`, category: "tool" as const,
      tool_info: { setup_state: { status: index === 0 ? "ok" as const : "missing" as const } },
    })),
    disabledToolIds: [], hiddenToolIds: [], filterEntries: [],
  }));
  assert.match(html, /許可中<\/p><p[^>]*>149<\/p>/);
  assert.match(html, /設定が必要<\/p><p[^>]*>148<\/p>/);
  for (const label of ["権限で無効", "実行不可", "承認が必要"]) {
    assert.match(html, new RegExp(`${label}</p><p[^>]*>0</p>`));
  }
  assert.match(html, /style="grid-template-columns:minmax\(0,1fr\) auto"/);
  assert.match(html, /whitespace-nowrap text-right[^\"]*tabular-nums/);
  assert.doesNotMatch(html, /xl:grid-cols-5/);
});
