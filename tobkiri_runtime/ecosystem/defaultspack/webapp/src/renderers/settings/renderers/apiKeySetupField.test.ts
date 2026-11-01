import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { apiKeySetupSaveErrorMessage, BuiltinApiKeySetupRenderer } from "./apiKeySetupField";

test("pending saved connections do not imply missing keys or open credential entry", () => {
  const html = renderToStaticMarkup(createElement(BuiltinApiKeySetupRenderer, {
    sectionId: "models",
    field: { id: "api_keys", label: "AI APIキー", type: "api_key_setup" },
    value: null,
    onChange: () => undefined,
  }));

  assert.match(html, /接続を確認中…/);
  assert.doesNotMatch(html, /登録済みの接続がありません/);
  assert.doesNotMatch(html.match(/<details\b[^>]*>/)?.[0] ?? "", /\bopen\b/);
});

test("API key save errors redact secrets and connection URLs", () => {
  const message = apiKeySetupSaveErrorMessage(new Error(
    "request http://127.0.0.1:1234/v1 failed: Authorization: Bearer local-secret-value api_key=another-secret",
  ));

  assert.match(message, /APIキーを保存できませんでした/);
  assert.doesNotMatch(message, /local-secret-value|another-secret|127\.0\.0\.1/);
  assert.match(message, /\[url\]|\[auth-header\]|\[credential\]/);
});
