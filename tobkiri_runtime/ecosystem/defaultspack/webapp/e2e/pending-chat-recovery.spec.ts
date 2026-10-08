import { expect, test } from "@playwright/test";

test("unknown send stays editable and local recovery retains the request without replay or owner commands", async ({ page }) => {
  let mutationCount = 0;
  page.on("request", (request) => {
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) mutationCount += 1;
  });
  await page.goto("/e2e/fixtures/pending-chat-recovery.html");
  const input = page.getByLabel("Tobkiriにメッセージを送信", { exact: true });
  await expect(input).toBeEditable();
  await input.fill("次の下書き");
  await input.press("Enter");
  await expect(page.getByRole("button", { name: "送信結果が未確認", exact: true })).toBeDisabled();
  await expect(page.getByTestId("request-count")).toHaveText("送信 0・実行操作 0");
  await expect(page.getByText("会話を準備しています。", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "記録を残して待機を解除", exact: true }).click();
  await expect(input).toHaveValue("次の下書き");
  await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeEnabled();
  await page.getByText("未確認の送信記録（1件）", { exact: true }).click();
  await expect(page.locator("#composer-pending-recovery")).toContainText("使えるtool教えて");
  await expect(page.getByTestId("request-count")).toHaveText("送信 0・実行操作 0");
  expect(mutationCount).toBe(0);
  await page.screenshot({ path: test.info().outputPath("pending-recovery.png") });
  await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
  await expect(page.getByTestId("request-count")).toHaveText("送信 1・実行操作 0");
});
