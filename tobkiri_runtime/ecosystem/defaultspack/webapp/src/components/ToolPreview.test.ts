import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  artifactDialogItemFromToolPreview,
  buildCanvasTabPickerItems,
  buildToolPreviewDisplayItems,
  buildToolPreviewTimelineItems,
  CANVAS_CLOSE_LABEL,
  CanvasCloseButton,
  hasCanvasItems,
  hardenedHtmlPreviewDocument,
  isCanvasPreviewItemRenderable,
  MAX_INLINE_PREVIEW_IMAGE_URL_LENGTH,
  remotePreviewText,
  RemotePreviewBoundary,
  safePreviewImageUrl,
  MEMO_PREVIEW_ID,
  selectCanvasTab,
  WEB_PREVIEW_IFRAME_SANDBOX,
  type ToolPreviewItem,
} from "./ToolPreview";
import { modalStackTransition } from "./ModalFoundation";

test("modal layer stack closes only the requested layer and preserves nesting", () => {
  const parent = modalStackTransition([], "open", "settings");
  const nested = modalStackTransition(parent, "open", "artifact");
  assert.deepEqual(nested, ["settings", "artifact"]);
  assert.deepEqual(modalStackTransition(nested, "close", "artifact"), ["settings"]);
  assert.deepEqual(modalStackTransition(nested, "close", "settings"), ["artifact"]);
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

test("canvas close button has a stable accessible name and closes the panel", () => {
  let closeCalls = 0;
  const button = CanvasCloseButton({
    onClose: () => {
      closeCalls += 1;
    },
  });

  assert.equal(button.type, "button");
  assert.equal(button.props.type, "button");
  assert.equal(button.props["aria-label"], CANVAS_CLOSE_LABEL);
  assert.equal(button.props.title, CANVAS_CLOSE_LABEL);

  button.props.onClick();
  assert.equal(closeCalls, 1);
});

test("canvas stays hidden until a preview or memo content exists", () => {
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

test("web preview iframe sandbox disables active capabilities", () => {
  assert.equal(WEB_PREVIEW_IFRAME_SANDBOX, "");
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
  assert.equal(image.untrustedSourceUrl, undefined);
  assert.equal(file.kind, "file");
  assert.equal(file.title, "a.txt");
});


test("tool preview image policy permits raster data only and rejects SVG", () => {
  assert.equal(safePreviewImageUrl("data:image/png;base64,abc", "https://rumi.example/"), "data:image/png;base64,abc");
  assert.equal(safePreviewImageUrl("data:image/svg+xml;base64,PHN2Zz4=", "https://rumi.example/"), undefined);
  assert.equal(safePreviewImageUrl("/artifact/1", "https://rumi.example/"), undefined);
  assert.equal(safePreviewImageUrl("https://attacker.example/pixel.gif", "https://rumi.example/"), undefined);
  assert.equal(
    safePreviewImageUrl(`data:image/png;base64,${"a".repeat(MAX_INLINE_PREVIEW_IMAGE_URL_LENGTH)}`),
    undefined,
  );
});

test("remote file URLs stay inert until the host provides verified artifact content", () => {
  const remote = remotePreviewText({
    type: "file",
    filename: "report.html",
    size: "tool artifact",
    url: "https://tool.example/report.html",
  });
  const inline = remotePreviewText({
    type: "file",
    filename: "report.html",
    size: "inline artifact",
    url: "https://attacker.example/report.html",
    content: "<p>inline content</p>",
  });

  assert.equal(remote.isLoading, false);
  assert.match(remote.error ?? "", /unverified tool URLs are not fetched/);
  assert.equal(remote.text, "");
  assert.equal(inline.error, null);
  assert.equal(inline.text, "<p>inline content</p>");
});

test("blocked remote previews expose source copy without a network-bearing element", () => {
  const html = renderToStaticMarkup(createElement(RemotePreviewBoundary, {
    url: "https://attacker.example/side-effect",
    title: "Untrusted preview",
  }));

  assert.match(html, /Remote preview blocked/);
  assert.match(html, /URL をコピー/);
  assert.doesNotMatch(html, /\s(?:src|href)=/);
  assert.doesNotMatch(html, /<(?:iframe|img|a)\b/);
});

test("HTML preview document has a fail-closed CSP and no injected base URL", () => {
  const document = hardenedHtmlPreviewDocument("<head><base href='https://attacker.example'><meta content='0;url=https://attacker.example' http-equiv='refresh'></head><body><a href='https://attacker.example' ping='https://attacker.example/ping'>leave</a><area href='https://attacker.example/map'><script>fetch('https://attacker.example')</script><form action='https://attacker.example'><button>go</button></form></body>");
  assert.match(document, /Content-Security-Policy/);
  assert.match(document, /default-src 'none'/);
  assert.match(document, /connect-src 'none'/);
  assert.match(document, /form-action 'none'/);
  assert.match(document, /base-uri 'none'/);
  assert.match(document, /navigate-to 'none'/);
  assert.doesNotMatch(document, /<base\s/i);
  assert.doesNotMatch(document, /http-equiv=['"]refresh/i);
  assert.doesNotMatch(document, /<(?:a|area)\b/i);
  assert.match(document, /<span>leave<\/span>/);
});
