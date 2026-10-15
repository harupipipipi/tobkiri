import { expect, test, type Page } from "@playwright/test";
import type { mountRecoveryFixture } from "./side-chat-recovery.fixture";

type RecoveryFixture = Awaited<ReturnType<typeof mountRecoveryFixture>>;
declare global {
  interface Window {
    recoveryFixture: RecoveryFixture;
    $RefreshReg$?: () => void;
    $RefreshSig$?: () => (value: unknown) => unknown;
    __vite_plugin_react_preamble_installed__?: boolean;
  }
}

async function mount(page: Page) {
  await page.route("**/__side-chat-recovery-fixture", (route) => route.fulfill({
    contentType: "text/html", body: "<!doctype html><html><body></body></html>",
  }));
  // Forbid all external traffic: this is a component fixture, not live acceptance.
  await page.route("**/*", async (route) => {
    if (new URL(route.request().url()).hostname !== "127.0.0.1") await route.abort();
    else await route.fallback();
  });
  await page.goto("/__side-chat-recovery-fixture");
  await page.evaluate(async () => {
    const refreshPath = "/@react-refresh";
    const { default: refresh } = await import(/* @vite-ignore */ refreshPath);
    refresh.injectIntoGlobalHook(window);
    window.$RefreshReg$ = () => {};
    window.$RefreshSig$ = () => (value) => value;
    window.__vite_plugin_react_preamble_installed__ = true;
    const path = "/e2e/side-chat-recovery.fixture.tsx";
    const fixture = await import(/* @vite-ignore */ path);
    window.recoveryFixture = await fixture.mountRecoveryFixture();
  });
  await expect(page.locator("textarea")).toBeVisible();
}

test("latest-owner recovery uses the exact public action, disables inflight, and retains an unsent draft", async ({ page }) => {
  await mount(page);
  await page.locator("textarea").fill("unsent draft");
  const recovery = page.getByRole("button", { name: "最新の実行を復旧", exact: true });
  await expect(recovery).toBeDisabled();
  await page.evaluate(() => {
    window.recoveryFixture.replaceSource(window.recoveryFixture.withLatestTurn());
    window.recoveryFixture.setMode("pending");
  });
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(recovery).toBeEnabled();
  await recovery.click();
  await expect(page.locator('[data-view-control="reconcile"] button')).toBeDisabled();
  expect(await page.evaluate(() => window.recoveryFixture.requests())).toMatchObject([{
    contractId: "tobkiri.service.side-chat.turn.v1",
    contributionId: "pack.tobkiri_side_chat_pack.tobkiri_side_chat_pack.side-chat-turn",
    ownerPackId: "tobkiri_side_chat_pack", profileId: "profile",
    payload: { operation: "reconcile", conversation_id: "child", turn_id: "newer-turn" },
  }]);
  expect(await page.evaluate(() => Object.keys(window.recoveryFixture.requests()[0].payload).sort()))
    .toEqual(["conversation_id", "operation", "turn_id"]);
  await page.evaluate(() => window.recoveryFixture.resolvePending({ status: "approval_required" }));
  await expect(page.getByText("Waiting for approval in the trusted Tobkiri approval surface.")).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue("unsent draft");
  expect(await page.evaluate(() => window.recoveryFixture.requests().length)).toBe(1);
});

test("missing owner bindings and unavailable selected operation keep recovery disabled", async ({ page }) => {
  await mount(page);
  await page.evaluate(() => {
    const source = window.recoveryFixture.withLatestTurn();
    window.recoveryFixture.replaceSource({ ...source, conversation_id: undefined });
  });
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("button", { name: "最新の実行を復旧", exact: true })).toBeDisabled();
  await page.evaluate(async () => {
    window.recoveryFixture.replaceSource(window.recoveryFixture.withLatestTurn());
    await window.recoveryFixture.setAvailable(false);
  });
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("button", { name: "最新の実行を復旧", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => window.recoveryFixture.requests())).toEqual([]);
});

test("explicit captured-ticket recovery survives newer turns, failure, approval, and requires canonical saved history", async ({ page }) => {
  await mount(page);
  const draft = "  retained draft\n";
  await page.locator("textarea").fill(draft);
  await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
  await expect(page.getByRole("button", { name: "この送信結果を復旧", exact: true })).toBeVisible();
  const capturedTurn = await page.evaluate(() => window.recoveryFixture.requests()[0].payload.turn_id);
  await page.evaluate(() => window.recoveryFixture.replaceSource(window.recoveryFixture.withLatestTurn()));
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByRole("button", { name: "実行記録を再読み込み", exact: true }).click();
  await expect.poll(async () => page.evaluate(() => window.recoveryFixture.reads()
    .filter((request) => request.payload.operation === "events").length)).toBeGreaterThanOrEqual(2);
  expect(await page.evaluate(() => window.recoveryFixture.requests().length)).toBe(1);
  await page.evaluate(() => window.recoveryFixture.setMode("pending"));
  await page.getByRole("button", { name: "この送信結果を復旧", exact: true }).evaluate((button: HTMLButtonElement) => {
    button.click(); button.click();
  });
  await expect(page.getByRole("button", { name: "復旧結果を待っています…", exact: true })).toBeDisabled();
  const requests = await page.evaluate(() => window.recoveryFixture.requests());
  expect(requests).toHaveLength(2);
  expect(requests[1].payload).toEqual({ operation: "reconcile", conversation_id: "child", turn_id: capturedTurn });
  expect(await page.evaluate(() => window.recoveryFixture.canLeave())).toBe(false);
  await page.evaluate(() => window.recoveryFixture.failPending());
  await expect(page.getByText("復旧結果を確認できません。入力内容と送信IDを保持し、自動再送しません。")).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue(draft);
  await page.evaluate(() => window.recoveryFixture.setMode("returned_failure"));
  await page.getByRole("button", { name: "この送信結果を復旧", exact: true }).click();
  await expect(page.getByText("復旧結果を確認できません。入力内容と送信IDを保持し、自動再送しません。")).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue(draft);
  expect(await page.evaluate(() => window.recoveryFixture.canLeave())).toBe(false);
  await page.evaluate(async () => { await window.recoveryFixture.setAvailable(false); });
  await expect(page.getByRole("button", { name: "この送信結果を復旧", exact: true })).toBeDisabled();
  await expect(page.locator("textarea")).toHaveValue(draft);
  await page.evaluate(async () => {
    await window.recoveryFixture.setAvailable(true); window.recoveryFixture.setMode("approval");
  });
  await page.getByRole("button", { name: "この送信結果を復旧", exact: true }).click();
  await expect(page.getByText("Tobkiri の承認画面で確認を待っています。入力内容と送信IDを保持しています。")).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue(draft);
  expect(await page.evaluate(() => window.recoveryFixture.canLeave())).toBe(false);
  await page.evaluate(() => window.recoveryFixture.setMode("completed"));
  await page.getByRole("button", { name: "この送信結果を復旧", exact: true }).click();
  await expect(page.locator("textarea")).toHaveValue(draft);
  await expect(page.getByRole("button", { name: "この送信結果を復旧", exact: true })).toBeVisible();
  await page.evaluate(() => window.recoveryFixture.replaceSource(window.recoveryFixture.withSavedReceipt()));
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.locator("textarea")).toHaveValue("");
  await expect(page.getByRole("button", { name: "この送信結果を復旧", exact: true })).toHaveCount(0);
  await expect(page.getByText("Saved answer", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.recoveryFixture.requests().filter((request) => request.payload.operation === "send").length)).toBe(1);
  expect(await page.evaluate(() => window.recoveryFixture.canLeave())).toBe(true);
});
