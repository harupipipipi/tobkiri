import { expect, test, type Page } from "@playwright/test";

test.use({ viewport: { width: 1280, height: 800 } });

type HarnessInput = {
  action: string;
  seat_id: string;
  key?: string;
  text?: string;
};

async function openHarness(page: Page) {
  await page.route("**/api/contracts/defaultspack/**", (route) => route.fulfill({ status: 204 }));
  await page.goto("/e2e/desktop-keyboard-capture-harness.html");
  await expect(page.getByTestId("desktop-capture-harness")).toBeVisible();
}

async function inputs(page: Page): Promise<HarnessInput[]> {
  return page.evaluate(() => window.desktopCaptureHarness?.getInputs() ?? []);
}

test("keyboard capture is explicit, accessible, and reserves a guaranteed exit", async ({ page }) => {
  await openHarness(page);

  const tile = page.getByTestId("desktop-tile-seat-1");
  const frame = tile.getByRole("group", { name: "Primary desktop live snapshot" });
  const start = tile.getByRole("button", { name: "Start keyboard control" });
  const keyboardControl = tile.locator('button[aria-pressed]');
  const helpId = await frame.getAttribute("aria-describedby");

  expect(helpId).toBe("desktop-keyboard-help-seat-1");
  await expect(tile.locator(`#${helpId}`)).toContainText("Keyboard control is off for Primary desktop.");
  await frame.focus();
  await page.keyboard.type("x");
  expect(await inputs(page)).toEqual([]);

  await start.focus();
  await page.keyboard.press("Enter");
  const capturedFrame = tile.getByRole("application", { name: /Primary desktop live snapshot, keyboard control active/ });
  await expect(capturedFrame).toBeFocused();
  await expect(keyboardControl).toHaveAttribute("aria-pressed", "true");
  await expect(capturedFrame).toHaveAttribute("aria-keyshortcuts", "Escape Control+Alt+Shift+Escape");
  await expect(tile).toContainText("Tab, Shift+Tab, and supported in-page shortcuts are sent remotely.");
  await expect(tile).toContainText("Physical key-up and held-key state are unavailable");

  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  const shortcutDefaultPrevented = await capturedFrame.evaluate((element) => !element.dispatchEvent(new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key: "l",
    ctrlKey: true,
  })));
  expect(shortcutDefaultPrevented).toBe(true);
  await expect.poll(() => inputs(page)).toEqual([
    { action: "key", key: "Tab", seat_id: "seat-1" },
    { action: "key", key: "shift+Tab", seat_id: "seat-1" },
    { action: "key", key: "ctrl+l", seat_id: "seat-1" },
  ]);
  await expect(capturedFrame).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(start).toBeFocused();
  await expect(keyboardControl).toHaveAttribute("aria-pressed", "false");
  await expect(tile.getByRole("group", { name: "Primary desktop live snapshot" })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(start).toBeFocused();
  await page.keyboard.press("Enter");
  await tile.getByRole("application", { name: /keyboard control active/ }).dispatchEvent("keydown", {
    key: "Escape",
    ctrlKey: true,
    altKey: true,
    shiftKey: true,
  });
  await expect(keyboardControl).toBeFocused();
  await expect(keyboardControl).toHaveAccessibleName("Start keyboard control");

  await page.keyboard.press("Space");
  await expect(tile.getByRole("application", { name: /keyboard control active/ })).toBeFocused();
  await tile.getByRole("button", { name: "Release keyboard control" }).focus();
  await page.keyboard.press("Space");
  await expect(start).toBeFocused();
});

test("capture handles international input and blocks unsupported local keys", async ({ page }) => {
  await openHarness(page);
  const tile = page.getByTestId("desktop-tile-seat-1");
  await tile.getByRole("button", { name: "Start keyboard control" }).click();
  const frame = tile.getByRole("application", { name: /keyboard control active/ });

  await frame.evaluate((element) => {
    element.dispatchEvent(new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      key: "😀",
    }));
    const clipboard = new DataTransfer();
    clipboard.setData("text/plain", "pasted 日本語");
    element.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: clipboard,
    }));
    element.dispatchEvent(new CompositionEvent("compositionstart", {
      bubbles: true,
      cancelable: true,
      data: "",
    }));
    element.dispatchEvent(new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      key: "Process",
      isComposing: true,
    }));
    element.dispatchEvent(new CompositionEvent("compositionend", {
      bubbles: true,
      cancelable: true,
      data: "入力",
    }));
  });

  await expect.poll(() => inputs(page)).toEqual([
    { action: "type_text", text: "😀", seat_id: "seat-1" },
    { action: "type_text", text: "pasted 日本語", seat_id: "seat-1" },
    { action: "type_text", text: "入力", seat_id: "seat-1" },
  ]);

  const defaultWasPrevented = await frame.evaluate((element) => !element.dispatchEvent(new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    key: "F1",
  })));
  expect(defaultWasPrevented).toBe(true);
  await expect(tile.getByText("F1 is not supported by remote keyboard control and was not sent.", { exact: true })).toBeVisible();
  expect(await inputs(page)).toHaveLength(3);
  await expect(frame).toBeFocused();
});

test("lease loss, seat stop, desktop switch, and remote errors release and restore focus", async ({ page }) => {
  await openHarness(page);
  const harness = page.getByTestId("desktop-capture-harness");
  const primary = page.getByTestId("desktop-tile-seat-1");
  const primaryStart = primary.getByRole("button", { name: "Start keyboard control" });

  await primaryStart.click();
  await page.evaluate(() => window.desktopCaptureHarness?.setLeaseSeat(null));
  await expect(harness).toHaveAttribute("data-lease-seat", "none");
  await expect(primaryStart).toBeFocused();
  await expect(primaryStart).toHaveAttribute("aria-disabled", "true");
  await expect(primary).toContainText("because the control lease ended");

  await page.evaluate(() => window.desktopCaptureHarness?.setLeaseSeat("seat-1"));
  await expect(primaryStart).toHaveAttribute("aria-disabled", "false");
  await primaryStart.click();
  await page.evaluate(() => window.desktopCaptureHarness?.setStatus("seat-1", "stopped"));
  await expect(primaryStart).toBeFocused();
  await expect(primary).toContainText("because the desktop is stopped");

  await page.evaluate(() => {
    window.desktopCaptureHarness?.setStatus("seat-1", "running");
    window.desktopCaptureHarness?.setLeaseSeat("seat-1");
  });
  await expect(primaryStart).toHaveAttribute("aria-disabled", "false");
  await primaryStart.click();
  await page.evaluate(() => window.desktopCaptureHarness?.setLeaseSeat("seat-2"));
  await expect(primaryStart).toBeFocused();
  const secondary = page.getByTestId("desktop-tile-seat-2");
  const secondaryStart = secondary.getByRole("button", { name: "Start keyboard control" });
  await expect(secondaryStart).toHaveAttribute("aria-disabled", "false");

  await secondaryStart.click();
  await page.evaluate(() => window.desktopCaptureHarness?.setRejectInput(true));
  await expect(harness).toHaveAttribute("data-reject-input", "true");
  await page.keyboard.press("Enter");
  await expect(secondaryStart).toBeFocused();
  await expect(secondary).toContainText("because remote input was rejected");
});

test("keyboard controls remain usable without horizontal overflow at a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await openHarness(page);
  const tile = page.getByTestId("desktop-tile-seat-1");

  await expect(tile.getByRole("button", { name: "Start keyboard control" })).toBeVisible();
  await expect(tile).toContainText("Keyboard control is off for Primary desktop.");
  const widths = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(widths.body).toBeLessThanOrEqual(widths.viewport);
});
