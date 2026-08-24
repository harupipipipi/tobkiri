import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { BackendConnectionBanner } from "./BackendConnectionBanner";

test("connection banner announces degraded pending-send state and exposes a real health check", () => {
  const html = renderToStaticMarkup(
    createElement(BackendConnectionBanner, {
      state: "degraded",
      lastHealthyAt: Date.now(),
      pendingOperation: "send",
      locale: "ja",
      onCheckConnection: () => undefined,
    }),
  );

  assert.match(html, /role="status"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /aria-atomic="true"/);
  assert.match(html, /data-backend-connection-state="degraded"/);
  assert.match(html, /再接続中/);
  assert.match(html, /送信結果を確認中/);
  assert.match(html, /<button[^>]*>接続を確認<\/button>/);
});

test("initial online state does not announce a recovery that did not happen", () => {
  const html = renderToStaticMarkup(
    createElement(BackendConnectionBanner, {
      state: "online",
      lastHealthyAt: Date.now(),
      pendingOperation: null,
      locale: "en",
      onCheckConnection: () => undefined,
    }),
  );

  assert.equal(html, "");
});

test("connection banner distinguishes a pending approval from a pending send", () => {
  const html = renderToStaticMarkup(
    createElement(BackendConnectionBanner, {
      state: "offline",
      lastHealthyAt: Date.now(),
      pendingOperation: "approval",
      locale: "en",
      onCheckConnection: () => undefined,
    }),
  );

  assert.match(html, /approved action result cannot be confirmed/);
  assert.match(html, /Do not run the same action again/);
  assert.doesNotMatch(html, /send result/);
});
