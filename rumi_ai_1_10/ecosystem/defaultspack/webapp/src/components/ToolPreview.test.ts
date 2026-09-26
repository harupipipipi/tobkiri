import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  artifactDialogItemFromToolPreview,
  buildCanvasTabPickerItems,
  buildToolPreviewDisplayItems,
  buildToolPreviewTimelineItems,
  hasCanvasItems,
  isCanvasPreviewItemRenderable,
  MEMO_PREVIEW_ID,
  selectCanvasTab,
  ToolPreviewPanel,
  WEB_PREVIEW_IFRAME_SANDBOX,
  type ToolPreviewItem,
} from "./ToolPreview";

test("empty Canvas exposes memo creation before any artifact exists", () => {
  const props = {
    previews: [],
    isVisible: true,
    onClose: () => undefined,
    mode: "manual" as const,
    onModeChange: () => undefined,
    memo: "",
  };
  const html = renderToStaticMarkup(createElement(ToolPreviewPanel, {
    ...props,
    onMemoChange: () => undefined,
  }));
  assert.match(html, /aria-label="Canvas タブを追加"/);
  assert.equal(renderToStaticMarkup(createElement(ToolPreviewPanel, props)), "");
});

const previews: ToolPreviewItem[] = [
  {
    id: "first",
    toolStepId: "tool-a",
    timestamp: 2,
    data: { type: "file", filename: "a.txt", size: "1kb", content: "a" },
  },
  {
    id: "second",
    toolStepId: "tool-b",
    timestamp: 1,
    data: { type: "web", url: "https://example.com", title: "Example" },
  },
];

test("canvas has content only after a preview or memo content exists", () => {
  assert.equal(hasCanvasItems([], ""), false);
  assert.equal(hasCanvasItems([], "   "), false);
  assert.equal(hasCanvasItems(previews, ""), true);
  assert.equal(hasCanvasItems([], "memo"), true);
});

test("tool preview items put the active item first without injecting empty memo", () => {
  const displayItems = buildToolPreviewDisplayItems(previews, "", "tool-b");

  assert.equal(displayItems[0]?.id, "second");
  assert.equal(displayItems.some((item) => item.id === "__memo__"), false);
});

test("canvas filters planned-tool placeholders", () => {
  const placeholder: ToolPreviewItem = {
    id: "planned",
    toolStepId: "coding_file_create",
    timestamp: 3,
    data: {
      type: "code",
      filename: "coding_file_create",
      language: "text",
      content: "Tool planned or referenced: coding_file_create",
    },
  };

  assert.equal(isCanvasPreviewItemRenderable(placeholder), false);
  assert.equal(hasCanvasItems([placeholder], ""), false);
  assert.deepEqual(buildToolPreviewDisplayItems([placeholder], "", null), []);
  assert.deepEqual(buildToolPreviewTimelineItems([placeholder]), []);
});

test("memo is shown first only when it is active or has content", () => {
  assert.equal(buildToolPreviewDisplayItems(previews, "", "__memo__")[0]?.id, "__memo__");
  assert.equal(buildToolPreviewDisplayItems(previews, "draft note", null)[0]?.id, "__memo__");
});

test("canvas picker offers and selects the first empty memo tab", () => {
  const displayItems = buildToolPreviewDisplayItems(previews, "", null);
  const pickerItems = buildCanvasTabPickerItems(displayItems, "", true);
  const memoItem = pickerItems.find((item) => item.id === MEMO_PREVIEW_ID);

  assert.ok(memoItem);
  assert.equal(memoItem.data.type, "file");
  assert.equal(memoItem.data.filename, "memo.md");
  assert.equal(memoItem.data.content, "");

  const selected = selectCanvasTab([], memoItem);
  assert.deepEqual(selected.openPreviewIds, [MEMO_PREVIEW_ID]);
  assert.equal(selected.activeTabId, MEMO_PREVIEW_ID);
  assert.equal(selected.memoTabCreated, true);
});

test("canvas picker omits memo when editing is not supported and never duplicates it", () => {
  const displayItems = buildToolPreviewDisplayItems(previews, "", null);

  assert.equal(
    buildCanvasTabPickerItems(displayItems, "", false).some(
      (item) => item.id === MEMO_PREVIEW_ID,
    ),
    false,
  );

  const withMemo = buildToolPreviewDisplayItems(previews, "draft", null);
  assert.equal(
    buildCanvasTabPickerItems(withMemo, "draft", true).filter(
      (item) => item.id === MEMO_PREVIEW_ID,
    ).length,
    1,
  );
});

test("tool preview timeline is chronological regardless of display ordering", () => {
  const displayItems = buildToolPreviewDisplayItems(previews, "", "tool-a");

  assert.deepEqual(displayItems.map((item) => item.id), ["first", "second"]);
  assert.deepEqual(buildToolPreviewTimelineItems(displayItems).map((item) => item.id), ["second", "first"]);
});

test("web preview iframe sandbox keeps same-origin content isolated", () => {
  assert.match(WEB_PREVIEW_IFRAME_SANDBOX, /allow-scripts/);
  assert.doesNotMatch(WEB_PREVIEW_IFRAME_SANDBOX, /allow-same-origin/);
});

test("tool preview artifacts map to reusable foreground dialog items", () => {
  const image = artifactDialogItemFromToolPreview({
    id: "img",
    toolStepId: "tool-img",
    timestamp: 3,
    data: { type: "image", url: "data:image/png;base64,abc", alt: "screenshot", path: "/tmp/screen.png" },
  });
  const file = artifactDialogItemFromToolPreview(previews[0]);

  assert.equal(image.kind, "image");
  assert.equal(image.imageUrl, "data:image/png;base64,abc");
  assert.equal(image.href, undefined);
  assert.equal(file.kind, "file");
  assert.equal(file.title, "a.txt");
});
