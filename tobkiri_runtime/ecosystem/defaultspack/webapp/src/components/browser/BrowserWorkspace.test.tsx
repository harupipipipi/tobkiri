import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { BrowserCookiesPanel } from "./BrowserCookiesPanel";
import { BrowserDevtoolsPanel } from "./BrowserDevtoolsPanel";
import { BrowserExtensionsPanel } from "./BrowserExtensionsPanel";
import { BrowserWorkspace } from "./BrowserWorkspace";

const onAction = async () => null;

test("Browser workspace exposes profiles and all browser panels without embedding untrusted pages", () => {
  const html = renderToStaticMarkup(<BrowserWorkspace initialSnapshot={{ runtime: { running: true }, activeProfileId: "managed", profiles: [{ profile_id: "managed", label: "AI research" }], tabs: [{ tab_id: "tab-1", title: "Example page", url: "https://example.test" }] }} />);
  for (const label of ["Tobkiri Browser", "ブラウザプロファイル", "AI research", "拡張機能", "Cookie インポート", "開発者ツール", "Example page", "ブラウザの URL"]) assert.ok(html.includes(label), label);
  assert.doesNotMatch(html, /<iframe|dangerouslySetInnerHTML/);
});

test("Browser workspace displays missing runtime readiness and a disabled launch action", () => {
  const html = renderToStaticMarkup(<BrowserWorkspace initialSnapshot={{ runtime: { running: false, executable_available: false, message: "Chromium executable was not found" }, activeProfileId: "managed", profiles: [{ profile_id: "managed" }] }} />);
  assert.match(html, /Chromium executable was not found/);
  assert.match(html, /ブラウザを起動する準備ができていません/);
  assert.match(html, /<button[^>]*disabled=""[^>]*>.*?ブラウザを起動/s);
});

test("browser extension and cookie panels provide file import controls and show scope", () => {
  const extensions = renderToStaticMarkup(<BrowserExtensionsPanel profileId="managed" extensions={[]} disabled={false} receipt="" onAction={onAction} />);
  assert.match(extensions, /拡張機能のフォルダ/);
  assert.match(extensions, /type="file"/);
  assert.match(extensions, /Manifest V3/);
  const cookies = renderToStaticMarkup(<BrowserCookiesPanel profileId="managed" disabled={false} receipt="" onAction={onAction} />);
  assert.match(cookies, /JSON/);
  assert.match(cookies, /Netscape/);
  assert.match(cookies, /Cookie の対象ドメイン/);
  assert.doesNotMatch(cookies, /textarea|localStorage/);
});

test("developer tools escape inspection content and render bounded network capture records", () => {
  const html = renderToStaticMarkup(<BrowserDevtoolsPanel profileId="managed" tabId="tab-1" disabled={false} inspection={{ title: "<script>alert(1)</script>" }} evaluation={null} capture={{ requests: [{ request_id: "req-1", status: 200, method: "GET", url: "https://example.test", type: "Document" }], console: [{ level: "error", url: "https://example.test", argument_count: 1 }], truncated: true }} onAction={onAction} />);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /10 秒/);
  assert.match(html, /表示上限に達しました/);
  assert.match(html, /Console イベント/);
  assert.match(html, /https:\/\/example.test/);
});
