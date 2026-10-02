import { expect, test, type Page } from "@playwright/test";

async function mockBrowserHost(page: Page, options: { denyStart?: boolean } = {}) {
  const hash = `sha256:${"1".repeat(64)}`;
  const calls: Array<{ operation: string; arguments: Record<string, unknown> }> = [];
  let running = false;
  let tabs: Array<{ tab_id: string; title: string; url: string }> = [];
  const extensions: Array<Record<string, unknown>> = [];
  const contribution = {
    contribution_id: "defaultspack.frontend.browser", kind: "route", mode: "application_builtin",
    label: "Browser", priority: 0, owner_pack_id: "defaultspack", owner_pack_hash: hash,
    build_identity: "fixture", resolved_profile_id: "defaults", resolved_profile_revision: hash,
    resolved_activation_id: "activation:browser-fixture", resolved_plan_hash: hash, descriptor_hash: hash,
    route: "/browser", implementation: "defaultspack.browser", localization: {}, accessibility: { name: "Browser", keyboard: true },
  };
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const target = decodeURIComponent(new URL(route.request().url()).pathname.split("/api/contracts/defaultspack/")[1]);
    let data: unknown;
    if (target === "GET /api/ui/catalog") {
      data = { dynamic_host: { version: "rumi.ui.contribution.v1", profile_id: "defaults", profile_revision: hash, activation_id: "activation:browser-fixture", plan_hash: hash, selected_entry_route: "/browser", contributions: [contribution], quarantined_pack_ids: [], diagnostics: [], catalog_hash: hash } };
    } else if (target === "POST /api/browser/observe" || target === "POST /api/browser/control") {
      const request = route.request().postDataJSON() as typeof calls[number];
      calls.push(request);
      for (const key of ["approved", "approval_token", "authority_token", "viewer_host_approved", "yolo_mode"]) {
        expect(request.arguments).not.toHaveProperty(key);
      }
      const args = request.arguments;
      switch (request.operation) {
        case "browser.runtime.status": data = { running, managed: true, profile_id: "managed", executable_available: true, websocket_available: true }; break;
        case "browser.profiles.list": data = { active_profile_id: "managed", profiles: [{ profile_id: "managed", label: "AI research" }] }; break;
        case "browser.extensions.list": data = { extensions }; break;
        case "browser.tabs.list": data = { tabs }; break;
        case "browser.runtime.start":
          if (options.denyStart) { await route.fulfill({ status: 403, json: { success: false, error: "Browser control permission denied" } }); return; }
          running = true; tabs = [{ tab_id: "tab-1", title: "New tab", url: "about:blank" }]; data = { running, profile_id: "managed" }; break;
        case "browser.runtime.stop": running = false; data = { running }; break;
        case "browser.navigate": {
          const tabId = typeof args.tab_id === "string" ? args.tab_id : `tab-${tabs.length + 1}`;
          const existing = tabs.find((tab) => tab.tab_id === tabId);
          const tab = { tab_id: tabId, title: "Example page", url: String(args.url) };
          if (existing) Object.assign(existing, tab); else tabs.push(tab);
          data = { tab_id: tabId, url: args.url }; break;
        }
        case "browser.tab.select": data = { tab_id: args.tab_id }; break;
        case "browser.extensions.install": {
          const manifest = JSON.parse((args.files as Record<string, string>)["manifest.json"]);
          extensions.push({ extension_id: "page-helper", name: manifest.name, version: manifest.version, permissions: manifest.permissions, loaded: true });
          data = { installed: true }; break;
        }
        case "browser.cookies.import": data = { imported_count: 1, applied_to_browser: true }; break;
        case "browser.devtools.inspect": data = { tab_id: args.tab_id, title: "Example page", dom: { root: { name: "HTML" } } }; break;
        case "browser.devtools.evaluate": data = { value: "Example page" }; break;
        case "browser.network.capture": data = { requests: [{ request_id: "request-1", method: "GET", status: 200, url: "https://example.test/data", type: "Fetch" }], console: [], duration_ms: args.duration_ms }; break;
        default: throw new Error(`Unexpected browser operation ${request.operation}`);
      }
    } else throw new Error(`Unexpected route ${target}`);
    await route.fulfill({ json: { success: true, data } });
  });
  return calls;
}

test("Browser uses the admitted route for navigation, extension review, cookie import and developer tools", async ({ page }) => {
  test.setTimeout(60_000);
  const calls = await mockBrowserHost(page);
  await page.goto("/p/defaults/browser");
  await expect(page.getByRole("heading", { name: "Tobkiri Browser" })).toBeVisible();
  await page.getByRole("button", { name: "ブラウザを起動", exact: true }).click();
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeEnabled();
  await page.getByRole("textbox", { name: "ブラウザの URL" }).fill("https://example.test/");
  await page.getByRole("button", { name: "開く", exact: true }).click();
  await expect(page.getByRole("button", { name: /Example page/ })).toBeVisible();
  await page.getByRole("button", { name: "新しいタブ", exact: true }).click();
  await page.getByRole("textbox", { name: "ブラウザの URL" }).fill("https://second.test/");
  await page.getByRole("button", { name: "開く", exact: true }).click();
  await expect(page.getByRole("button", { name: /Example page/ })).toHaveCount(2);
  expect(calls.filter((call) => call.operation === "browser.navigate").at(-1)?.arguments).not.toHaveProperty("tab_id");

  await page.getByRole("button", { name: "拡張機能", exact: true }).click();
  await page.getByLabel("拡張機能のファイル", { exact: true }).setInputFiles([
    { name: "manifest.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ manifest_version: 3, name: "AI page helper", version: "1.0", permissions: ["storage"], host_permissions: ["https://example.test/*"] })) },
    { name: "content.js", mimeType: "text/javascript", buffer: Buffer.from("document.title = 'AI helper';") },
  ]);
  await expect(page.getByText("アクセスするサイト", { exact: true })).toBeVisible();
  await expect(page.getByText("https://example.test/*", { exact: true })).toBeVisible();
  expect(calls.filter((call) => call.operation === "browser.extensions.install")).toHaveLength(0);
  await page.getByRole("button", { name: "権限を確認して追加", exact: true }).click();
  await expect(page.getByRole("button", { name: "AI page helper を削除", exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Cookie インポート", exact: true }).click();
  const cookieContent = JSON.stringify([{ domain: "example.test", path: "/", name: "session", value: "private-session-value" }]);
  await page.getByLabel("Cookie ファイル", { exact: true }).setInputFiles({ name: "cookies.json", mimeType: "application/json", buffer: Buffer.from(cookieContent) });
  await page.getByRole("textbox", { name: "Cookie の対象ドメイン" }).fill("example.test");
  await expect(page.locator("body")).not.toContainText("private-session-value");
  await page.getByRole("button", { name: "確認してインポート", exact: true }).click();
  await expect(page.getByRole("button", { name: "確認してインポート", exact: true })).toBeDisabled();
  expect(calls.find((call) => call.operation === "browser.cookies.import")?.arguments.domains).toEqual(["example.test"]);

  await page.getByRole("button", { name: "開発者ツール", exact: true }).click();
  await page.getByRole("button", { name: "DOM・レイアウトを取得", exact: true }).click();
  await expect(page.getByLabel("DOM とレイアウト")).toContainText("HTML");
  await page.getByRole("button", { name: "記録を開始", exact: true }).click();
  await expect(page.getByRole("table", { name: "ネットワーク通信の記録" })).toContainText("https://example.test/data");
  expect(calls.find((call) => call.operation === "browser.network.capture")?.arguments).toMatchObject({ profile_id: "managed", tab_id: "tab-2", duration_ms: 2000, max_entries: 200, reload: false });
  await page.setViewportSize({ width: 720, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("Browser preserves host denial and does not retry a blocked control action", async ({ page }) => {
  const calls = await mockBrowserHost(page, { denyStart: true });
  await page.goto("/p/defaults/browser");
  await page.getByRole("button", { name: "ブラウザを起動", exact: true }).click();
  await expect(page.locator("body")).toContainText("Browser control permission denied");
  await expect(page.getByRole("button", { name: "ブラウザを起動", exact: true })).toBeEnabled();
  expect(calls.filter((call) => call.operation === "browser.runtime.start")).toHaveLength(1);
});
