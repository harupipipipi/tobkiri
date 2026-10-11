import assert from "node:assert/strict";
import test from "node:test";

import { apiKeySetupSaveErrorMessage } from "./apiKeySetupField";

test("API key save errors redact secrets and connection URLs", () => {
  const message = apiKeySetupSaveErrorMessage(new Error(
    "request http://127.0.0.1:1234/v1 failed: Authorization: Bearer local-secret-value api_key=another-secret",
  ));

  assert.match(message, /APIキーを保存できませんでした/);
  assert.doesNotMatch(message, /local-secret-value|another-secret|127\.0\.0\.1/);
  assert.match(message, /\[url\]|\[auth-header\]|\[credential\]/);
});
