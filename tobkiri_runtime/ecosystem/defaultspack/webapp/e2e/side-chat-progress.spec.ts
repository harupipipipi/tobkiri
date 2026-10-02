import { expect, test, type Page } from "@playwright/test";
import type { mountProgressFixture } from "./side-chat-progress.fixture";
declare global { interface Window { threadProgressFixture: Awaited<ReturnType<typeof mountProgressFixture>> } }
async function mount(page: Page, captureTtl?: number) {
  await page.route("**/__side-chat-progress-fixture", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><html><body></body></html>" }));
  await page.route("**/*", async (route) => {
    if (new URL(route.request().url()).hostname !== "127.0.0.1") await route.abort(); else await route.fallback();
  });
  await page.goto("/__side-chat-progress-fixture");
  await page.evaluate(async () => {
    const refreshPath = "/@react-refresh"; const { default: refresh } = await import(/* @vite-ignore */ refreshPath);
    refresh.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (value) => value;
    window.__vite_plugin_react_preamble_installed__ = true;
    const path = "/e2e/side-chat-progress.fixture.tsx"; const fixture = await import(/* @vite-ignore */ path);
    window.threadProgressFixture = await fixture.mountProgressFixture();
  });
  if (captureTtl !== undefined) await page.evaluate((ttl) => window.threadProgressFixture.captureExpiry(ttl), captureTtl);
  await page.locator("textarea").fill("  retained draft\n");
  await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("paragraph").filter({ hasText: "retained draft" })).toBeVisible();
}

test("actual resource pages display before send settles; finish keeps draft/ticket until canonical history", async ({ page }) => {
  await mount(page);
  await page.evaluate(() => { window.threadProgressFixture.append({ type: "text_delta", delta: "First real chunk" }); window.threadProgressFixture.pulse(); });
  await expect(page.locator("[data-thread-progress-text]")).toHaveText("First real chunk");
  await page.evaluate(() => {
    window.threadProgressFixture.append({ type: "thinking_delta", delta: "Raw private reasoning must remain hidden" });
    window.threadProgressFixture.append({ type: "text_delta", delta: " <script>untrusted text</script>" });
    window.threadProgressFixture.append({ type: "finish", finish_reason: "stop" }); window.threadProgressFixture.pulse();
  });
  await expect(page.locator("[data-thread-progress-text]")).toHaveText("First real chunk <script>untrusted text</script>");
  await expect(page.getByText("Raw private reasoning must remain hidden")).toHaveCount(0);
  await expect(page.locator("[data-thread-progress] script")).toHaveCount(0);
  await expect(page.locator("textarea")).toHaveValue("  retained draft\n");
  expect(await page.evaluate(() => window.threadProgressFixture.canLeave())).toBe(false);
  expect(await page.evaluate(() => window.threadProgressFixture.requests().filter((request) => request.payload.operation === "send").length)).toBe(1);
  const reads = await page.evaluate(() => window.threadProgressFixture.reads().filter((request) => request.contractId === "tobkiri.resource.turn.progress.v1"));
  expect(reads.length).toBeGreaterThan(0);
  for (const read of reads) expect(Object.keys(read.payload).sort()).toEqual(["conversation_id", "cursor", "turn_id"]);
  await page.evaluate(() => { window.threadProgressFixture.failSend(); window.threadProgressFixture.savedHistory(); });
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByText("Canonical saved answer", { exact: true })).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue("");
  await expect(page.locator("[data-thread-progress]")).toHaveCount(0);
});

test("changed binding or missing read target hides provisional text and never retries AI", async ({ page }) => {
  await mount(page);
  await page.evaluate(() => { window.threadProgressFixture.append({ type: "text_delta", delta: "Confirmed chunk" }); window.threadProgressFixture.pulse(); });
  await expect(page.locator("[data-thread-progress-text]")).toHaveText("Confirmed chunk");
  await page.evaluate(() => { window.threadProgressFixture.tamper({ binding: {
    turn_id: "foreign-turn", conversation_id: "child", parent_id: "user-1", conversation_revision: 4,
    request_id: "other.request", input_digest: `sha256:${"a".repeat(64)}`, ai_input_digest: `sha256:${"b".repeat(64)}`,
  } }); window.threadProgressFixture.pulse(); });
  await expect(page.locator("[data-thread-progress-text]")).toHaveCount(0);
  await expect(page.locator("textarea")).toHaveValue("  retained draft\n");
  await page.evaluate(async () => { await window.threadProgressFixture.disableProgress(); window.threadProgressFixture.pulse(); });
  await expect(page.getByText("ライブ応答は現在利用できません。保存された実行記録の照合を続けます。")).toBeVisible();
  expect(await page.evaluate(() => window.threadProgressFixture.requests().filter((request) => request.payload.operation === "send").length)).toBe(1);
});

test("a delayed page from an old capture cannot render after activation changes", async ({ page }) => {
  await mount(page);
  await page.evaluate(() => { window.threadProgressFixture.hold(); window.threadProgressFixture.append({ type: "text_delta", delta: "Stale delayed text" }); window.threadProgressFixture.pulse(); });
  await expect.poll(() => page.evaluate(() => window.threadProgressFixture.hasHeldRead())).toBe(true);
  await page.evaluate(async () => { await window.threadProgressFixture.changeCapture(); window.threadProgressFixture.release(); });
  await expect(page.locator("[data-thread-progress-text]")).toHaveCount(0);
  await expect(page.getByText("Stale delayed text")).toHaveCount(0);
  expect(await page.evaluate(() => window.threadProgressFixture.requests().filter((request) => request.payload.operation === "send").length)).toBe(1);
});

test("catalog refresh cannot extend the original retained resource deadline", async ({ page }) => {
  await mount(page, 1500);
  await page.evaluate(() => { window.threadProgressFixture.append({ type: "text_delta", delta: "Finite captured text" }); window.threadProgressFixture.pulse(); });
  await expect(page.locator("[data-thread-progress-text]")).toHaveText("Finite captured text");
  await page.evaluate(async () => { await window.threadProgressFixture.captureExpiry(60000); window.threadProgressFixture.pulse(); });
  await expect(page.locator("[data-thread-progress-text]")).toHaveCount(0);
  await expect(page.getByText("ライブ応答は現在利用できません。保存された実行記録の照合を続けます。")).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue("  retained draft\n");
  expect(await page.evaluate(() => window.threadProgressFixture.requests().filter((request) => request.payload.operation === "send").length)).toBe(1);
});
