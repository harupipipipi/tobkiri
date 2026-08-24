import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { SettingsStatusBar } from "./SettingsStatusBar";

test("offline Settings copy stays save-specific and avoids implementation terms", () => {
  const html = renderToStaticMarkup(
    createElement(SettingsStatusBar, {
      backendState: "offline",
      backendNote: null,
      locale: "ja",
      saveState: {
        status: "error",
        dirtyKeys: ["general.language"],
      },
      loadState: { status: "ready" },
      onRetrySave: () => undefined,
    }),
  );

  assert.match(html, /オフライン/);
  assert.match(html, /サーバーでは未確認/);
  assert.match(html, /再接続後に保存状態を確認/);
  assert.match(html, /保存を再試行/);
  assert.doesNotMatch(html, /backend/i);
  assert.doesNotMatch(html, /送信|下書き/);
});
