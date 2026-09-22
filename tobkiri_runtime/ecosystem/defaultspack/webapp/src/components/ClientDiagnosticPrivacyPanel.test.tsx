import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ClientDiagnosticPrivacyPanel } from "./ClientDiagnosticPrivacyPanel";

test("diagnostic privacy panel defaults to local-only and exposes the safe schema preview", () => {
  const html = renderToStaticMarkup(<ClientDiagnosticPrivacyPanel locale="en" />);

  assert.match(html, /Client diagnostic privacy/);
  assert.match(html, /Remote reporting is opt-in/);
  assert.match(html, /checked="" value="local_only"/);
  assert.match(html, /Remote reporting: off/);
  assert.match(html, /rumi\.client_diagnostic\.v2/);
  assert.match(html, /Redacted diagnostic preview/);
  assert.doesNotMatch(html, /access_token|Authorization|private prompt|tool output|Users\//i);
});

test("diagnostic privacy panel localizes user-facing controls", () => {
  const html = renderToStaticMarkup(<ClientDiagnosticPrivacyPanel locale="ja" />);

  assert.match(html, /クライアント診断のプライバシー/);
  assert.match(html, /リモート送信は明示的な選択が必要です/);
  assert.match(html, /安全なプレビューをコピー/);
});
