import { expect, test } from "@playwright/test";

test("chat notification floats, folds, reopens and retains explicit retry and dismiss", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  const notification = page.getByRole("alert");
  await expect(notification).toContainText("Load failed");
  await expect(notification.locator(".rumi-chat-notification-title")).toHaveText("Load failed - Tobkiri");
  await expect(notification.locator(".rumi-chat-notification-message")).toHaveCount(0);
  await expect(page.getByText("Hello! How can I help you today?", { exact: true })).toBeVisible();
  const geometry = await notification.evaluate((element) => ({
    position: getComputedStyle(element.parentElement!).position,
    width: element.getBoundingClientRect().width,
    rounded: getComputedStyle(element).borderRadius,
    stackOverflow: getComputedStyle(element.parentElement!).overflowY,
  }));
  expect(geometry.position).toBe("fixed");
  expect(geometry.width).toBeLessThanOrEqual(440);
  expect(geometry.rounded).toBe("24px");
  expect(geometry.stackOverflow).toBe("visible");
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

test("keyboard reopening releases focus when the bell is removed", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  await page.mouse.move(0, 0);
  await page.clock.fastForward(8_100);
  const reopen = page.getByRole("button", { name: "通知を表示 (1件)" });
  await reopen.focus();
  await reopen.press("Enter");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(reopen).toHaveCount(0);
  await page.clock.fastForward(8_100);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(reopen).toBeVisible();
});

test("closing a focused card does not pause a surviving notice", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  await page.getByRole("button", { name: "完了通知", exact: true }).click();
  await page.mouse.move(0, 0);
  const close = page.getByRole("button", { name: "エラーを閉じる", exact: true });
  await close.focus();
  await close.press("Enter");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator("[data-chat-completion-notice]")).toBeVisible();
  await page.clock.fastForward(8_100);
  await expect(page.locator("[data-chat-completion-notice]")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "通知を表示 (1件)" })).toBeVisible();
});

test("replacing a focused notice releases pause but retaining focus still pauses", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  await page.mouse.move(0, 0);
  await page.getByRole("button", { name: "チャットエラーをコピー", exact: true }).focus();
  // A prop update outside the notification must not itself move DOM focus.
  await page.getByRole("button", { name: "長い通知", exact: true }).dispatchEvent("click");
  await expect(page.getByRole("alert")).toContainText("詳細な通信エラー");
  await page.clock.fastForward(8_100);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "通知を表示 (1件)" }).press("Enter");
  await page.getByRole("button", { name: "チャットエラーをコピー", exact: true }).focus();
  await page.getByRole("button", { name: "完了通知", exact: true }).dispatchEvent("click");
  await page.clock.fastForward(8_100);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.locator("[data-chat-completion-notice]")).toBeVisible();
});

test("removing the hovered card resumes the remaining notice timer", async ({ page }) => {
  await page.clock.install();
  await page.goto("/e2e/fixtures/chat-notifications.html");
  await page.getByRole("button", { name: "長い完了通知", exact: true }).click();
  const completion = page.locator("[data-chat-completion-notice]");
  const message = completion.locator(".rumi-chat-notification-message");
  const bounds = await message.boundingBox();
  expect(bounds).not.toBeNull();
  await page.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + bounds!.height - 5);
  await page.clock.fastForward(9_000);
  await expect(completion).toBeVisible();
  // External dismissal removes the hovered element without moving the pointer.
  await completion.getByRole("button", { name: "通知を閉じる", exact: true }).dispatchEvent("click");
  await expect(completion).toHaveCount(0);
  await page.clock.fastForward(8_100);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("two long notifications scroll within a short viewport", async ({ page }) => {
  await page.clock.install();
  await page.setViewportSize({ width: 390, height: 400 });
  await page.goto("/e2e/fixtures/chat-notifications.html");
  await page.getByRole("button", { name: "長い通知", exact: true }).click();
  await page.getByRole("button", { name: "長い完了通知", exact: true }).click();
  const stack = page.locator("[data-chat-notifications]");
  const geometry = await stack.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      top: bounds.top, bottom: bounds.bottom,
      overflow: getComputedStyle(element).overflowY,
      scrollable: element.scrollHeight > element.clientHeight,
    };
  });
  expect(geometry.top).toBeGreaterThanOrEqual(0);
  expect(geometry.bottom).toBeLessThanOrEqual(392);
  const cardBounds = await page.getByRole("alert").boundingBox();
  expect(cardBounds!.x).toBeGreaterThanOrEqual(12);
  expect(cardBounds!.x + cardBounds!.width).toBeLessThanOrEqual(378);
  expect(geometry.overflow).toBe("auto");
  expect(geometry.scrollable).toBe(true);
  await stack.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  const completionBounds = await page.locator("[data-chat-completion-notice]").boundingBox();
  expect(completionBounds!.y + completionBounds!.height).toBeLessThanOrEqual(388);
  const close = page.locator("[data-chat-completion-notice]").getByRole("button", { name: "通知を閉じる", exact: true });
  await close.focus();
  const closeBounds = await close.boundingBox();
  expect(closeBounds!.y).toBeGreaterThanOrEqual(geometry.top);
  expect(closeBounds!.y + closeBounds!.height).toBeLessThanOrEqual(388);
  await page.clock.runFor(300);
  await page.screenshot({ path: test.info().outputPath("notification-short-stack.png") });
  await close.press("Enter");
  await expect(page.locator("[data-chat-completion-notice]")).toHaveCount(0);
  await expect(page.getByRole("alert")).toBeVisible();
});

test("promoted error headings copy the exact original error including all details", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "notificationCopiedText", { value: "", writable: true });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text: string) => { Reflect.set(window, "notificationCopiedText", text); } },
    });
  });
  await page.goto("/e2e/fixtures/chat-notifications.html");
  const notification = page.getByRole("alert");
  const copy = notification.getByRole("button", { name: "チャットエラーをコピー", exact: true });
  await copy.click();
  await expect.poll(() => page.evaluate(() => Reflect.get(window, "notificationCopiedText"))).toBe("Load failed");
  await expect(notification.getByRole("status")).toContainText("クリップボードにコピーしました");
  await page.getByRole("button", { name: "長い通知", exact: true }).click();
  await expect(notification.locator(".rumi-chat-notification-title")).toHaveText("詳細な通信エラー - Tobkiri");
  await copy.click();
  await expect.poll(() => page.evaluate(() => Reflect.get(window, "notificationCopiedText"))).toBe(
    "詳細な通信エラー\n" + "保存結果の確認を待っています。".repeat(120),
  );
});
