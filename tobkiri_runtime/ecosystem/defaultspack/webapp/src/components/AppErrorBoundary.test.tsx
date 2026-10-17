import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AppErrorBoundary } from "./AppErrorBoundary";

const RAW_SECRET = "sk-never-render-this-secret";
const RAW_PATH = "/Users/private/project/secret.ts";

test("crash state is redacted and exposes factual recoverable actions", () => {
  const derived = AppErrorBoundary.getDerivedStateFromError(new Error(`private prompt ${RAW_SECRET} at ${RAW_PATH}`));
  const boundary = new AppErrorBoundary({ children: createElement("div", null, "healthy") });
  boundary.state = { ...boundary.state, ...derived, diagnosticStatus: "not_recorded", draft: { capturedAt: "now", drafts: { "rumi-input": "private draft content" } }, crashCount: 2 };
  const html = renderToStaticMarkup(boundary.render());
  assert.match(html, /この画面の表示処理が停止しました/);
  assert.match(html, /診断情報は記録されていません/);
  assert.match(html, /この画面を再試行|チャットへ戻る|セーフモード|ページ全体を再読み込み/);
  assert.match(html, /入力をJSONで保存/);
  assert.match(html, /aria-live="assertive"|role="alert"/);
  assert.match(html, /data-error-icon="application-recovery"/);
  assert.match(html, /aria-label="クラッシュ情報をコピー"/);
  assert.match(html, /data-copy-icon=""/);
  assert.match(html, /role="status" aria-live="polite"/);
  assert.doesNotMatch(html, new RegExp(RAW_SECRET));
  assert.doesNotMatch(html, /private prompt|private draft content|Users\/private/);
});

test("diagnostic copy only claims recording after acknowledgement", () => {
  const boundary = new AppErrorBoundary({ children: null });
  boundary.state = { ...boundary.state, failed: true, diagnosticReference: "diag_safe", diagnosticStatus: "sending" };
  assert.match(renderToStaticMarkup(boundary.render()), /記録完了とはまだ確認されていません/);
  boundary.state = { ...boundary.state, diagnosticStatus: "recorded" };
  assert.match(renderToStaticMarkup(boundary.render()), /backendに記録されました.*diag_safe/);
});

test("initial crash mount focuses the recovery heading", () => {
  const boundary = new AppErrorBoundary({ children: null });
  boundary.state = { ...boundary.state, failed: true };
  let focused = false;
  Object.defineProperty((boundary as unknown as { headingRef: { current: unknown } }).headingRef, "current", { value: { focus: () => { focused = true; } } });
  boundary.componentDidMount();
  assert.equal(focused, true);
});
