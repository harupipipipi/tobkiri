import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { ErrorCopyAction, ErrorNotice, copyTextWithFallback, errorNoticeCopyText } from "./ErrorNotice";
import { ChatNotifications, chatErrorPresentation } from "./ChatNotifications";

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
  assert.match(source, /const copyAttempt = useRef\(0\)/);
  assert.match(source, /if \(attempt === copyAttempt\.current\)/);
  assert.match(
    source,
    /useLayoutEffect\(\(\) => \{\s+copyAttempt\.current \+= 1;\s+setFeedback\("idle"\);[\s\S]*?\}, \[copyText\]\);/,
  );
  assert.doesNotMatch(
    source,
    /useEffect\(\(\) => \{\s+copyAttempt\.current \+= 1;\s+setFeedback\("idle"\);[\s\S]*?\}, \[copyText\]\);/,
  );
  assert.doesNotMatch(source, /<Check\b|<X\b/);
});

test("the top-level unavailable route uses the shared error surface", async () => {
  const source = await readFile(new URL("../App.tsx", import.meta.url), "utf8");

  assert.match(source, /errorIcon="screen-unavailable"/);
  assert.match(source, /copyLabel="Copy unavailable screen error"/);
  assert.match(source, /message="This screen is not available in Tobkiri\."/);
  assert.doesNotMatch(source, /<main role="alert">This screen is not available in Tobkiri\.<\/main>/);
});

test("new chat owns one shared notification surface without an inline error banner", async () => {
  const source = await readFile(new URL("../App.tsx", import.meta.url), "utf8");
  const start = source.indexOf(") : showNewConversationStage && !isLoading ? (");
  assert.notEqual(start, -1);
  const home = source.slice(start, source.indexOf("<Renderers.chatMessages", start));

  assert.equal((home.match(/<ChatNotifications\b/g) ?? []).length, 1);
  assert.ok(home.indexOf("<ChatNotifications") < home.indexOf("<h1"));
  assert.match(home, /error=\{error\}/);
  assert.match(home, /onDismissError=\{error \? dismissChatError : undefined\}/);
  assert.match(home, /onDismissCompletionNotice=\{\(\) => setSavedTurnCompletionNotice\(null\)\}/);
  assert.match(home, /onRetry=\{retryIsEligible\(\) \? handleRetryLastFailedSubmission : undefined\}/);
  assert.match(home, /completionNotice=\{savedTurnCompletionNotice\?\.conversationId === activeConversationId/);
  assert.doesNotMatch(home, /role="alert"|border-red-500\/30/);
  assert.equal((source.match(/<ChatNotifications\b/g) ?? []).length, 1);
});

test("chat error promotes its short first line without duplicate body or branding", () => {
  assert.deepEqual(chatErrorPresentation("Load failed"), {
    title: "Load failed", message: "", copyText: "Load failed",
  });
  const markup = renderToStaticMarkup(<ChatNotifications error="Load failed" />);
  assert.match(markup, />Load failed - Tobkiri<\/p>/);
  assert.equal((markup.match(/Load failed/g) ?? []).length, 1);
  assert.equal((markup.match(/Tobkiri/g) ?? []).length, 1);
  assert.doesNotMatch(markup, /rumi-chat-notification-message|処理を完了できませんでした/);
});

test("chat error keeps multiline detail and exact original clipboard text", async () => {
  const original = "  Load failed  \r\n\r\n  HTTP 503: local server unavailable\r\n  retry after checking connection";
  const notice = chatErrorPresentation(original);
  assert.equal(notice.title, "Load failed");
  assert.equal(notice.message, "HTTP 503: local server unavailable\n  retry after checking connection");
  let clipboard = "";
  assert.equal(await copyTextWithFallback(notice.copyText, {
    clipboard: { writeText: async (text) => { clipboard = text; } },
  }), true);
  assert.equal(clipboard, original);
});

test("long or blank-first-line chat errors retain their complete scrollable details", () => {
  for (const original of ["診断".repeat(100), "\nLoad failed\n  detail remains"]) {
    const notice = chatErrorPresentation(original);
    assert.equal(notice.title, "処理を完了できませんでした");
    assert.equal(notice.message, original);
    assert.equal(notice.copyText, original);
    const markup = renderToStaticMarkup(<ChatNotifications error={original} />);
    assert.match(markup, /rumi-chat-notification-message/);
  }
});

test("completion notices use one branded title and preserve distinct details", () => {
  const markup = renderToStaticMarkup(<ChatNotifications error={null} completionNotice={{
    tone: "success", title: "送信を確認しました", message: "返答は会話に保存されています。",
  }} />);
  assert.match(markup, />送信を確認しました - Tobkiri<\/p>/);
  assert.match(markup, /返答は会話に保存されています。/);
  assert.equal((markup.match(/Tobkiri/g) ?? []).length, 1);
  const repeated = renderToStaticMarkup(<ChatNotifications error={null} completionNotice={{
    tone: "warning", title: "接続を確認してください", message: "接続を確認してください",
  }} />);
  assert.doesNotMatch(repeated, /rumi-chat-notification-message/);
});
