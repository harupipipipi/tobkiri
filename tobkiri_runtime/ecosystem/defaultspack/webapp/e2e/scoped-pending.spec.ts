import { expect, test } from "@playwright/test";

test("scoped pending hook withholds ownership on every A B A entry and fences old setters", async ({ page }) => {
  await page.goto("/e2e/fixtures/scoped-pending.html");
  await page.evaluate(async () => {
    const { mountScopedPendingFixture } = await import("/e2e/scoped-pending.fixture.tsx");
    (window as any).__pendingFixture = await mountScopedPendingFixture();
  });
  await expect(page.getByTestId("owner")).toHaveText("false");
  await page.evaluate(() => (window as any).__pendingFixture.observe());
  await expect(page.getByTestId("owner")).toHaveText("true");
  await page.evaluate(() => (window as any).__pendingFixture.switchStore("b"));
  await expect(page.getByTestId("pending")).toHaveText("");
  await page.evaluate(() => (window as any).__pendingFixture.lateAUpdate());
  await expect(page.getByTestId("pending")).toHaveText("");
  await page.evaluate(() => (window as any).__pendingFixture.switchStore("a"));
  await expect(page.getByTestId("pending")).toHaveText("chat");
  await expect(page.getByTestId("owner")).toHaveText("false");
  await page.evaluate(() => (window as any).__pendingFixture.lateAUpdate());
  await expect(page.getByTestId("pending")).toHaveText("chat");
  await page.evaluate(() => (window as any).__pendingFixture.externalWriteThenForget());
  await expect(page.getByTestId("pending")).toHaveText("other");
  await page.evaluate(() => (window as any).__pendingFixture.quotaFailure());
  await expect(page.getByTestId("pending")).toHaveText("chat");
  await expect(page.getByTestId("persistence")).toHaveText("true");
});
