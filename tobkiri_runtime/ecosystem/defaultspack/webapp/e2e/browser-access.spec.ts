import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { existsSync } from "node:fs";
import { expect, test } from "@playwright/test";

const runtimeRoot = join(process.cwd(), "../../../");
const target = "/p/defaults/chat";

function browserAccessPage(requestAllowed = true): string {
  const script = [
    "from core_runtime.api.browser_access import browser_access_document",
    `print(browser_access_document(${JSON.stringify(target)}, request_allowed=${requestAllowed ? "True" : "False"}).decode())`,
  ].join("; ");
  const localPython = join(runtimeRoot, "../.venv/bin/python");
  const python = existsSync(localPython) ? localPython : (process.env.PYTHON ?? "python3");
  return execFileSync(python, ["-B", "-c", script], {
    cwd: runtimeRoot,
    encoding: "utf8",
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

async function showCeremony(page: import("@playwright/test").Page, allowed = true, url = "/") {
  const html = browserAccessPage(allowed);
  await page.route(/^http:\/\/127\.0\.0\.1:\d+\/(?:\?.*)?$/, (route) =>
    route.fulfill({ contentType: "text/html", body: html }),
  );
  await page.route("**/p/defaults/chat", (route) =>
    route.fulfill({ contentType: "text/html", body: "<p>Authenticated test chat</p>" }),
  );
  await page.goto(url);
}

const jsonResponse = (data: object, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify({ success: status < 400, data }),
});

test("approval claims the session and returns to the exact chat target", async ({ page }) => {
  await page.clock.install();
  await showCeremony(page);
  await page.clock.runFor(300);
  await page.screenshot({ path: test.info().outputPath("browser-access-request.png") });
  await page.route("**/api/panel/browser-access/request", (route) =>
    route.fulfill(jsonResponse({ request_id: "request-1" })),
  );
  let polls = 0;
  await page.route("**/api/panel/browser-access/status", (route) =>
    route.fulfill(jsonResponse({ status: ++polls === 1 ? "pending" : "approved" })),
  );
  await page.route("**/api/panel/browser-access/claim", (route) =>
    route.fulfill(jsonResponse({ csrf_token: "csrf-test", journal_scope: "scope-test" })),
  );

  await page.getByRole("button", { name: "アクセスをリクエスト" }).click();
  await expect(page.getByRole("status")).toHaveText("Launcherで承認を待っています…");
  await page.clock.runFor(1_100);
  await expect(page).toHaveURL(/\/p\/defaults\/chat$/);
  expect(await page.evaluate(() => sessionStorage.getItem("rumi-panel-csrf"))).toBe("csrf-test");
  expect(await page.evaluate(() => sessionStorage.getItem("tobkiri-panel-journal-scope-v1"))).toBe("scope-test");
});

test("denial offers a retry and the next request can be approved", async ({ page }) => {
  await showCeremony(page);
  let requestCount = 0;
  await page.route("**/api/panel/browser-access/request", (route) => {
    requestCount += 1;
    return route.fulfill(jsonResponse({ request_id: `request-${requestCount}` }));
  });
  await page.route("**/api/panel/browser-access/status", (route) =>
    route.fulfill(jsonResponse({ status: requestCount === 1 ? "denied" : "approved" })),
  );
  await page.route("**/api/panel/browser-access/claim", (route) =>
    route.fulfill(jsonResponse({ csrf_token: "csrf", journal_scope: "scope" })),
  );

  await page.getByRole("button", { name: "アクセスをリクエスト" }).click();
  await expect(page.getByRole("status")).toHaveText("アクセスは許可されませんでした。");
  await page.getByRole("button", { name: "もう一度リクエスト" }).click();
  await expect(page).toHaveURL(/\/p\/defaults\/chat$/);
  expect(requestCount).toBe(2);
});

test("expired request and unavailable launcher show recoverable errors", async ({ page }) => {
  await showCeremony(page);
  await page.route("**/api/panel/browser-access/request", (route) =>
    route.fulfill(jsonResponse({ request_id: "expired" })),
  );
  await page.route("**/api/panel/browser-access/status", (route) =>
    route.fulfill(jsonResponse({ code: "invalid_request" }, 400)),
  );
  await page.getByRole("button", { name: "アクセスをリクエスト" }).click();
  await expect(page.getByRole("status")).toHaveText("リクエストの期限が切れました。もう一度リクエストしてください。");

  await page.unroute("**/api/panel/browser-access/request");
  await page.route("**/api/panel/browser-access/request", (route) =>
    route.fulfill(jsonResponse({ code: "launcher_unavailable" }, 503)),
  );
  await page.getByRole("button", { name: "もう一度リクエスト" }).click();
  await expect(page.getByRole("status")).toHaveText("Launcherに接続できません。起動してから再度お試しください。");
});

test("auth code exchange preserves the requested chat target", async ({ page }) => {
  await page.route("**/api/panel/auth/exchange", (route) =>
    route.fulfill(jsonResponse({ csrf_token: "exchange-csrf", journal_scope: "exchange-scope" })),
  );
  await showCeremony(page, true, "/?code=one-time-code&request_id=request-2");
  await expect(page).toHaveURL(/\/p\/defaults\/chat$/);
  expect(await page.evaluate(() => sessionStorage.getItem("rumi-panel-csrf"))).toBe("exchange-csrf");
});

test("request-disabled page stays inert and fits a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await showCeremony(page, false);
  await expect(page.getByRole("button")).toBeHidden();
  const dimensions = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
  }));
  expect(dimensions.bodyWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
});
