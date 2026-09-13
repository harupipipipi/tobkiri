import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { ErrorCopyAction, ErrorNotice, copyTextWithFallback, errorNoticeCopyText } from "./ErrorNotice";

test("error notices separate their severity icon from one stable copy glyph", () => {
  const markup = renderToStaticMarkup(
    <ErrorNotice
      copyLabel="起動エラーをコピー"
      errorIcon="startup"
      message="ランタイムに接続できませんでした。"
      title="Tobkiriを起動できませんでした"
    />,
  );

  assert.match(markup, /role="alert"/);
  assert.match(markup, /aria-live="assertive"/);
  assert.match(markup, /data-error-notice="startup"/);
  assert.match(markup, /data-error-icon="startup"/);
  assert.match(markup, /aria-label="起動エラーをコピー"/);
  assert.match(markup, /data-copy-action=""/);
  assert.match(markup, /data-copy-icon=""/);
  assert.match(markup, /role="status" aria-live="polite"/);
});

test("copy helper falls back to a selected textarea when Clipboard API rejects", async () => {
  let appended = false;
  let selected = false;
  let removed = false;
  const textarea = {
    focus: () => undefined,
    readOnly: false,
    remove: () => { removed = true; },
    select: () => { selected = true; },
    setAttribute: () => undefined,
    style: { cssText: "" },
    value: "",
  };
  const copied = await copyTextWithFallback("safe error", {
    clipboard: { writeText: async () => { throw new Error("denied"); } },
    document: {
      activeElement: null,
      body: { appendChild: () => { appended = true; } },
      createElement: () => textarea,
      execCommand: (command: string) => command === "copy",
    } as unknown as Document,
  });

  assert.equal(copied, true);
  assert.equal(appended, true);
  assert.equal(selected, true);
  assert.equal(removed, true);
  assert.equal(textarea.value, "safe error");
});

test("copy helper reports failure when neither clipboard path is usable", async () => {
  const copied = await copyTextWithFallback("safe error", {
    clipboard: { writeText: async () => { throw new Error("denied"); } },
    document: null,
  });

  assert.equal(copied, false);
});

test("severity controls the notice live mode and default copy text includes its title", () => {
  const warningMarkup = renderToStaticMarkup(
    <ErrorNotice
      message="再試行できます。"
      severity="warning"
      title="接続が一時的に切れました"
    />,
  );

  assert.match(warningMarkup, /role="status"/);
  assert.match(warningMarkup, /aria-live="polite"/);
  assert.doesNotMatch(warningMarkup, /role="alert"/);
  assert.equal(
    errorNoticeCopyText("接続が一時的に切れました", "再試行できます。"),
    "接続が一時的に切れました\n\n再試行できます。",
  );
  assert.equal(errorNoticeCopyText(undefined, "再試行できます。"), "再試行できます。");
});

test("non-announcing history keeps copy-result feedback available after a click", () => {
  const markup = renderToStaticMarkup(
    <ErrorNotice
      announce={false}
      message="過去の実行は失敗しました。"
      title="履歴エラー"
    />,
  );

  assert.doesNotMatch(markup, /role="alert"|aria-live="assertive"/);
  assert.match(markup, /role="status" aria-live="polite"/);
});

test("copy feedback remains polite for static-history controls", () => {
  const markup = renderToStaticMarkup(
    <ErrorCopyAction copyText="過去のエラー" />,
  );

  assert.match(markup, /role="status" aria-live="polite"/);
});

test("copy feedback keeps the Copy glyph instead of swapping to a status icon", async () => {
  const source = await readFile(new URL("./ErrorNotice.tsx", import.meta.url), "utf8");

  assert.match(source, /<Copy aria-hidden="true" data-copy-icon="" size=\{14\} \/>/);
  assert.match(source, /feedback === "copied" && "border-emerald-500\/60 text-emerald-200"/);
  assert.match(source, /const liveMode = severity === "warning" \? "polite" : "assertive"/);
  assert.match(source, /const noticeRole = severity === "warning" \? "status" : "alert"/);
  assert.match(source, /copyText \?\? errorNoticeCopyText\(title, message\)/);
  assert.doesNotMatch(source, /<Check\b|<X\b/);
});
