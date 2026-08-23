import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 390, height: 844 } });

test("surface templates stay safe and accessible in a narrow real browser", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/e2e/surface-template.html");
  await expect(page.locator("[data-browser-fixture-ready]")).toBeVisible();

  const surface = page.locator('[data-surface-template="test.browser.content"]');
  await expect(surface).toHaveAttribute("data-surface-pattern", "content");
  await expect(surface).toContainText("<img src=x onerror=");
  await expect(surface.locator("img")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.body.dataset.compromised ?? "safe")).toBe("safe");

  const bounds = await surface.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);

  const action = page.getByRole("button", { name: "Continue safely" });
  await expect(action).toBeVisible();
  expect((await action.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await action.focus();
  await expect(action).toBeFocused();
  await action.press("Enter");
  await expect(page.getByTestId("action-result")).toHaveText('{"choice":"approved"}');
  await expect.poll(() => surface.evaluate((node) => parseFloat(getComputedStyle(node).transitionDuration)))
    .toBeLessThanOrEqual(0.001);

  const untrusted = page.getByTestId("untrusted-surface");
  await expect(untrusted.getByRole("status")).toHaveText("This trusted decision surface is unavailable.");
  await expect(untrusted.getByRole("button", { name: "Delete" })).toHaveCount(0);

  await expect(page.getByTestId("failed-surface").getByRole("status"))
    .toHaveText("This surface could not be rendered safely.");
});
