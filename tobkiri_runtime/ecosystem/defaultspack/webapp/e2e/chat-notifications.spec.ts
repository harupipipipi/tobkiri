import { expect, test } from "@playwright/test";

test("chat notification floats, folds, reopens and retains explicit retry and dismiss", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  const notification = page.getByRole("alert");
  await expect(notification).toContainText("Load failed");
  await expect(page.getByText("Hello! How can I help you today?", { exact: true })).toBeVisible();
  const geometry = await notification.evaluate((element) => ({
    position: getComputedStyle(element.parentElement!).position,
    width: element.getBoundingClientRect().width,
    rounded: getComputedStyle(element).borderRadius,
  }));
  expect(geometry.position).toBe("fixed");
  expect(geometry.width).toBeLessThanOrEqual(440);
  expect(geometry.rounded).toBe("24px");
  await page.screenshot({ path: test.info().outputPath("notification-card.png") });
  await page.clock.fastForward(8_100);
  await expect(notification).toHaveCount(0);
  await page.getByRole("button", { name: "通知を表示 (1件)" }).click();
  await expect(notification).toContainText("Load failed");
  await page.getByRole("button", { name: "再試行", exact: true }).click();
  await expect(page.getByText("再試行が押されました", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "エラーを閉じる", exact: true }).click();
  await expect(notification).toHaveCount(0);
  await page.getByRole("button", { name: "エラー通知", exact: true }).click();
  await expect(notification).toContainText("Load failed");
});

test("notification pauses while being read and stays within a narrow viewport", async ({ page }) => {
  await page.clock.install();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/e2e/fixtures/chat-notifications.html");
  await page.getByRole("button", { name: "長い通知", exact: true }).click();
  const notification = page.getByRole("alert");
  await notification.hover();
  await page.clock.fastForward(9_000);
  await expect(notification).toBeVisible();
  const geometry = await notification.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const message = element.querySelector(".rumi-chat-notification-message")!;
    return {
      left: box.left, right: box.right,
      scrollable: message.scrollHeight > message.clientHeight,
      animation: getComputedStyle(element).animationName,
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(12);
  expect(geometry.right).toBeLessThanOrEqual(378);
  expect(geometry.scrollable).toBe(true);
  expect(geometry.animation).toBe("none");
  await page.getByRole("button", { name: "チャットエラーをコピー", exact: true }).focus();
  await page.mouse.move(0, 0);
  await page.clock.fastForward(9_000);
  await expect(notification).toBeVisible();
  await page.getByRole("button", { name: "エラーを解消", exact: true }).click();
  await expect(notification).toHaveCount(0);
  await page.getByRole("button", { name: "完了通知", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("送信を確認しました");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("pin keeps only its own notification visible and unpin restores folding", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  const notification = page.getByRole("alert");
  await notification.getByRole("button", { name: "通知を固定", exact: true }).click();
  await expect(notification.getByRole("button", { name: "通知の固定を解除", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "完了通知", exact: true }).click();
  await page.mouse.move(0, 0);
  await page.clock.fastForward(9_000);
  await expect(notification).toBeVisible();
  await expect(page.getByRole("button", { name: "通知を表示 (1件)" })).toBeVisible();
  await expect(page.locator("[data-chat-completion-notice]")).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("notification-pinned.png") });
  await notification.getByRole("button", { name: "通知の固定を解除", exact: true }).click();
  await expect(notification.getByRole("button", { name: "通知を固定", exact: true })).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("button", { name: "長い通知", exact: true }).focus();
  await page.mouse.move(0, 0);
  await page.clock.runFor(100);
  await page.clock.fastForward(9_000);
  await expect(notification).toHaveCount(0);
  await expect(page.getByRole("button", { name: "通知を表示 (2件)" })).toBeVisible();
});
