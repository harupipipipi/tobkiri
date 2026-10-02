import assert from "node:assert/strict";
import test from "node:test";

import { browserApprovalDetails, browserCookieDomains, browserNavigationUrl, browserPendingApproval, normalizeBrowserResult, readBrowserExtensionFiles } from "./browserModel";

function textFile(path: string, text: string): File {
  const bytes = new TextEncoder().encode(text);
  return {
    name: path.split("/").at(-1)!,
    webkitRelativePath: path,
    size: bytes.length,
    async arrayBuffer() { return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); },
  } as File;
}
const manifest = JSON.stringify({ manifest_version: 3, name: "AI page helper", version: "1.0.0", permissions: ["storage"], host_permissions: ["https://example.test/*"] });

test("extension folder selection preserves nested files and previews manifest access", async () => {
  const draft = await readBrowserExtensionFiles([
    textFile("page-helper/manifest.json", manifest),
    textFile("page-helper/scripts/content.js", "document.title = 'updated';"),
  ]);
  assert.deepEqual(Object.keys(draft.files), ["manifest.json", "scripts/content.js"]);
  assert.deepEqual(draft.permissions, ["storage"]);
  assert.deepEqual(draft.hostPermissions, ["https://example.test/*"]);
  assert.equal(draft.name, "AI page helper");
});

test("extension drafts reject escaping paths, duplicates and incompatible manifests", async () => {
  await assert.rejects(readBrowserExtensionFiles([textFile("helper/manifest.json", manifest), textFile("helper/../escape.js", "alert(1)")]), /相対パス/);
  await assert.rejects(readBrowserExtensionFiles([textFile("manifest.json", manifest), textFile("manifest.json", manifest)]), /重複/);
  await assert.rejects(readBrowserExtensionFiles([textFile("manifest.json", '{"manifest_version":2,"name":"old","version":"1"}')]), /Manifest V3/);
  await assert.rejects(readBrowserExtensionFiles([textFile("content.js", "let enabled = true;")]), /manifest.json/);
});

test("extension review includes optional privileges and content-script site access", async () => {
  const completeManifest = JSON.stringify({ manifest_version: 3, name: "Site helper", version: "1", optional_permissions: ["cookies"], optional_host_permissions: ["https://optional.test/*"], content_scripts: [{ matches: ["<all_urls>"], js: ["content.js"] }] });
  const draft = await readBrowserExtensionFiles([textFile("manifest.json", completeManifest), textFile("content.js", "console.log('ready')")]);
  assert.deepEqual(draft.permissions, ["cookies (任意)"]);
  assert.deepEqual(draft.hostPermissions, ["https://optional.test/* (任意)", "<all_urls> (Content Scripts)"]);
  assert.deepEqual(browserApprovalDetails("browser.extensions.install", { files: draft.files }).content_script_matches, ["<all_urls>"]);
});

test("extension drafts reject binary data and enforce the managed runtime byte limits", async () => {
  await assert.rejects(readBrowserExtensionFiles([textFile("manifest.json", manifest), textFile("icon.bin", "\u0000binary")]), /バイナリ/);
  const invalidUtf8 = { ...textFile("script.js", ""), size: 1, async arrayBuffer() { return new Uint8Array([255]).buffer; } } as File;
  await assert.rejects(readBrowserExtensionFiles([textFile("manifest.json", manifest), invalidUtf8]), /UTF-8/);
  await assert.rejects(readBrowserExtensionFiles([textFile("manifest.json", manifest), { ...textFile("large.js", ""), size: 1024 * 1024 + 1 } as File]), /1 MB/);
  await assert.rejects(readBrowserExtensionFiles([{ ...textFile("manifest.json", manifest), size: 64 * 1024 + 1 } as File]), /64 KB/);
});

test("managed navigation requires credential-free web URLs", () => {
  assert.equal(browserNavigationUrl(" https://example.test/path "), "https://example.test/path");
  for (const value of ["javascript:alert(1)", "file:///private", "https://user:password@example.test/"]) {
    assert.throws(() => browserNavigationUrl(value));
  }
});

test("browser host failures remain errors and complete widget data stays accessible", () => {
  assert.deepEqual(normalizeBrowserResult({ widget: { tabs: [{ tab_id: "tab-1" }] }, result: "model context" }), { tabs: [{ tab_id: "tab-1" }] });
  assert.throws(() => normalizeBrowserResult({ success: false, reason: "Browser control was denied" }), /denied/);
  assert.throws(() => normalizeBrowserResult({ is_error: true, widget: { message: "Runtime unavailable" } }), /Runtime unavailable/);
});

test("pending browser requests redact cookie content and extension source in the approval surface", () => {
  const cookies = browserPendingApproval({ approval_required: true, request_id: "request-1" }, "browser.cookies.import", { profile_id: "p-1", content: "secret-session-token", format: "json", domains: ["example.test"] });
  assert.equal(cookies?.requestId, "request-1");
  assert.doesNotMatch(JSON.stringify(cookies), /secret-session-token/);
  assert.equal(cookies?.payload.content_bytes, 20);
  const extension = browserApprovalDetails("browser.extensions.install", { files: { "manifest.json": manifest, "content.js": "const privateCode = true;" } });
  assert.deepEqual(extension.permissions, ["storage"]);
  assert.doesNotMatch(JSON.stringify(extension), /privateCode/);
  assert.throws(() => browserPendingApproval({ approval_required: true, approval_token: "untrusted" }, "browser.runtime.start", {}), /承認リクエスト/);
});

test("cookie domain filters preserve leading dots and reject URL or path inputs", () => {
  assert.deepEqual(browserCookieDomains("example.test, .example.org example.test"), ["example.test", ".example.org"]);
  assert.deepEqual(browserCookieDomains(""), []);
  assert.throws(() => browserCookieDomains("https://example.test/path"), /ドメイン名/);
});
