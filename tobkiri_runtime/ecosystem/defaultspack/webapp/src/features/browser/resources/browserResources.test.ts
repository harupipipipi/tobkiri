import assert from "node:assert/strict";
import test from "node:test";

import { managedBrowserRequest } from "../../../lib/api";
import { browserResources } from "./browserResources";

test("managed browser reads use observation contracts and mutation or capture uses control", () => {
  for (const action of ["browser.runtime.status", "browser.profiles.list", "browser.tabs", "browser.extensions.list", "browser.cookies.list", "browser.devtools.inspect"]) {
    assert.equal(managedBrowserRequest(action).route.apiPath, "/api/browser/observe", action);
  }
  for (const action of ["browser.runtime.start", "browser.runtime.stop", "browser.open_url", "browser.extensions.install", "browser.extensions.remove", "browser.cookies.import", "browser.devtools.evaluate", "browser.network.capture"]) {
    assert.equal(managedBrowserRequest(action).route.apiPath, "/api/browser/control", action);
  }
  assert.equal(managedBrowserRequest("browser.tabs").body.operation, "browser.tabs.list");
  assert.equal(managedBrowserRequest("browser.open_url").body.operation, "browser.navigate");
  assert.throws(() => managedBrowserRequest("computer.click"), /Unsupported/);
});

test("managed browser client never transports caller authority material", () => {
  for (const key of ["approved", "approval_token", "authority_token", "viewer_host_approved", "yolo_mode"]) {
    assert.throws(() => managedBrowserRequest("browser.runtime.start", { [key]: true }), /Host/);
  }
});

test("browser resources call the canonical contract with typed operation arguments", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl = "";
  let requestBody: unknown;
  globalThis.fetch = async (input, init) => {
    requestUrl = String(input);
    requestBody = JSON.parse(String(init?.body));
    return new Response(JSON.stringify({ success: true, data: { requests: [], console: [], duration_ms: 2000 } }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const result = await browserResources.run("browser.network.capture", { tab_id: "tab-1", duration_ms: 2000, reload: false });
    assert.equal(decodeURIComponent(requestUrl), "/api/contracts/defaultspack/POST /api/browser/control");
    assert.deepEqual(requestBody, { operation: "browser.network.capture", arguments: { tab_id: "tab-1", duration_ms: 2000, reload: false } });
    assert.deepEqual(result.requests, []);
  } finally { globalThis.fetch = originalFetch; }
});

test("browser resources preserve host denial without an automatic approval retry", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(JSON.stringify({ success: false, error: "Browser control permission denied" }), { status: 403, headers: { "Content-Type": "application/json" } });
  };
  try {
    await assert.rejects(browserResources.run("browser.runtime.start", { profile_id: "managed" }), /permission denied/);
    assert.equal(calls, 1);
  } finally { globalThis.fetch = originalFetch; }
});
