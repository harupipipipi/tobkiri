import { expect, test, type Page } from "@playwright/test";

async function openPet(page: Page) {
  const child = page.waitForEvent("popup");
  await page.getByRole("button", { name: /ペットを(開く|表示)/ }).click();
  const popup = await child;
  await expect(popup.getByRole("main", { name: "Tobkiri ペット" })).toBeVisible();
  return popup;
}

test("independent pet receives saved task updates, hides and restores without closing its parent", async ({ page, context }, testInfo) => {
  const apiRequests: string[] = [];
  context.on("request", (request) => {
    if (/\/api\//.test(request.url())) apiRequests.push(request.url());
  });
  await page.goto("/e2e/fixtures/task-pet.html");
  const originalUrl = page.url();
  const popup = await openPet(page);
  await expect(popup).toHaveURL(/\/p\/default\/chat\?surface=task-pet/);
  await expect(popup.locator(".task-pet-window-card")).toHaveAttribute("data-state", "thinking");
  await expect(popup.getByText("独立ウィンドウでタスクを見守る")).toBeVisible();
  expect(page.url()).toBe(originalUrl);
  await expect(page.getByRole("main", { name: "Tobkiri ペット" })).toHaveCount(0);
  await page.getByRole("button", { name: "タスクを完了" }).click();
  await expect(popup.locator(".task-pet-window-card")).toHaveAttribute("data-state", "completed");
  await expect(popup.getByText("タスクが完了しました")).toBeVisible();
  await popup.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
  await expect.poll(() => popup.locator(".task-pet-character").evaluate((image) => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  await popup.screenshot({ path: testInfo.outputPath("task-pet-window-preview.png") });
  await page.getByRole("button", { name: "長いタスクを表示" }).click();
  await expect(popup.locator(".task-pet-window-detail")).toContainText("日本語の長いタスク");
  const hideButton = popup.getByRole("button", { name: "非表示" });
  await expect(hideButton).toBeInViewport();
  expect(await popup.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await hideButton.click();
  await expect.poll(() => popup.isClosed()).toBe(true);
  await expect(page.getByRole("button", { name: "ペットを表示", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "親画面を操作" }).click();
  await expect(page.getByRole("status")).toHaveText("親画面の操作回数: 1");
  const restored = await openPet(page);
  await expect(restored.getByText("タスクが完了しました")).toBeVisible();
  await restored.close();
  await expect(page.getByRole("button", { name: "ペットを表示", exact: true })).toBeVisible();
  expect(page.isClosed()).toBe(false);
  expect(apiRequests).toEqual([]);
});

test("pet rejects foreign sources, origins and malformed state; drag requests move only its own window", async ({ page, context }) => {
  await context.addInitScript(() => {
    const moves: number[][] = [];
    Object.assign(window, { __petMoves: moves });
    window.moveTo = (x, y) => { moves.push([x, y]); };
  });
  await page.goto("/e2e/fixtures/task-pet.html");
  const popup = await openPet(page);
  await expect(popup.locator(".task-pet-window-card")).toHaveAttribute("data-state", "thinking");
  await popup.evaluate(() => {
    const presentation = { profileId: "default", enabled: true, view: { key: null, revision: 99, mood: "completed", label: "FORGED", title: "FORGED", detail: "FORGED" } };
    window.dispatchEvent(new MessageEvent("message", { origin: "https://foreign.invalid", source: window.opener, data: { type: "task-pet-state", presentation } }));
    window.dispatchEvent(new MessageEvent("message", { origin: location.origin, source: window, data: { type: "task-pet-state", presentation } }));
    window.dispatchEvent(new MessageEvent("message", { origin: location.origin, source: window.opener, data: { type: "task-pet-state", presentation: { ...presentation, view: null } } }));
  });
  await expect(popup.locator(".task-pet-window-card")).toHaveAttribute("data-state", "thinking");
  await expect(popup.getByText("FORGED")).toHaveCount(0);
  const handle = popup.getByRole("button", { name: "ペットをドラッグして移動" });
  const bounds = await handle.boundingBox();
  expect(bounds).not.toBeNull();
  await popup.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + bounds!.height / 2);
  await popup.mouse.down();
  await popup.mouse.move(bounds!.x + bounds!.width / 2 + 20, bounds!.y + bounds!.height / 2 + 15, { steps: 3 });
  await popup.mouse.up();
  await expect.poll(() => popup.evaluate(() => (window as unknown as { __petMoves: number[][] }).__petMoves.length)).toBeGreaterThan(0);
  expect(await page.evaluate(() => (window as unknown as { __petMoves: number[][] }).__petMoves)).toEqual([]);
});

type NativeCall = { command: string; args: Record<string, unknown> };
type NativeDouble = {
  calls: NativeCall[];
  emit: (event: string, payload: unknown) => void;
  resolveContext: (payload: unknown) => void;
};
async function installNativeDouble(page: Page) {
  await page.addInitScript(() => {
    const callbacks = new Map<number, (payload: unknown) => void>();
    const listeners = new Map<string, Map<number, number>>();
    const calls: { command: string; args: Record<string, unknown> }[] = [];
    let nextId = 0;
    let resolveContext: (payload: unknown) => void = () => undefined;
    Object.assign(window, {
      __nativePet: {
        calls,
        emit: (event: string, payload: unknown) => {
          for (const [id, callback] of listeners.get(event) ?? []) callbacks.get(callback)?.({ event, id, payload });
        },
        resolveContext: (payload: unknown) => resolveContext(payload),
      },
      __TAURI_EVENT_PLUGIN_INTERNALS__: {
        unregisterListener: (event: string, id: number) => listeners.get(event)?.delete(id),
      },
      __TAURI_INTERNALS__: {
        transformCallback: (callback: (payload: unknown) => void) => {
          const id = ++nextId;
          callbacks.set(id, callback);
          return id;
        },
        invoke: async (command: string, args: Record<string, unknown> = {}) => {
          calls.push({ command, args });
          if (command === "plugin:event|listen") {
            const id = ++nextId;
            const event = args.event as string;
            if (!listeners.has(event)) listeners.set(event, new Map());
            listeners.get(event)!.set(id, args.handler as number);
            return id;
          }
          if (command === "task_pet_context") return new Promise((resolve) => { resolveContext = resolve; });
          return null;
        },
      },
    });
  });
}
const nativePet = (page: Page) => page.evaluate(() => (window as unknown as { __nativePet: NativeDouble }).__nativePet.calls);

test("native parent listens before opening and does not reopen a hidden pet on completion", async ({ page }) => {
  await installNativeDouble(page);
  await page.goto("/e2e/fixtures/task-pet.html");
  await expect.poll(async () => (await nativePet(page)).filter((call) => call.command === "sync_task_pet").length).toBe(1);
  const initialCalls = await nativePet(page);
  expect(initialCalls.findIndex((call) => call.command === "plugin:event|listen")).toBeLessThan(initialCalls.findIndex((call) => call.command === "sync_task_pet"));
  expect(initialCalls.find((call) => call.command === "sync_task_pet")?.args.open).toBe(true);
  await page.getByRole("button", { name: "長いタスクを表示" }).click();
  await expect.poll(async () => (await nativePet(page)).filter((call) => call.command === "sync_task_pet").length).toBe(2);
  expect((await nativePet(page)).at(-1)?.args.open).toBe(false);
  await page.evaluate(() => (window as unknown as { __nativePet: NativeDouble }).__nativePet.emit("task-pet-hidden", { profileId: "default" }));
  await expect(page.getByRole("button", { name: "ペットを表示", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "タスクを完了" }).click();
  await expect.poll(async () => (await nativePet(page)).filter((call) => call.command === "sync_task_pet" && (call.args.presentation as { view: { mood: string } }).view.mood === "completed").length).toBeGreaterThan(0);
  expect((await nativePet(page)).filter((call) => call.command === "sync_task_pet").slice(1).every((call) => call.args.open === false)).toBe(true);
  const opensBeforeRestore = (await nativePet(page)).filter((call) => call.command === "sync_task_pet" && call.args.open === true).length;
  await page.getByRole("button", { name: "ペットを表示", exact: true }).click();
  await expect.poll(async () => (await nativePet(page)).filter((call) => call.command === "sync_task_pet" && call.args.open === true).length).toBe(opensBeforeRestore + 1);
  await expect(page.getByRole("button", { name: "ペットを開く", exact: true })).toBeVisible();
  expect((await nativePet(page)).filter((call) => call.command === "sync_task_pet" && call.args.open === true).length).toBe(opensBeforeRestore + 1);
});

test("native child keeps a newer event over delayed context and delegates drag and hide", async ({ page }) => {
  const apiRequests: string[] = [];
  page.on("request", (request) => { if (/\/api\//.test(request.url())) apiRequests.push(request.url()); });
  await installNativeDouble(page);
  await page.goto("/p/default/chat?surface=task-pet");
  await expect.poll(async () => (await nativePet(page)).some((call) => call.command === "task_pet_context")).toBe(true);
  await page.evaluate(() => {
    const bridge = (window as unknown as { __nativePet: NativeDouble }).__nativePet;
    const presentation = { profileId: "default", enabled: true, view: { key: null, revision: 2, mood: "completed", label: "完了", title: "新しい状態", detail: "確定した状態" } };
    bridge.emit("task-pet-state", presentation);
    bridge.resolveContext({ ...presentation, view: { ...presentation.view, revision: 1, mood: "thinking", title: "古い状態" } });
  });
  await expect(page.getByText("新しい状態")).toBeVisible();
  await page.evaluate(() => (window as unknown as { __nativePet: NativeDouble }).__nativePet.emit("task-pet-state", { profileId: "default", view: null }));
  await expect(page.getByText("新しい状態")).toBeVisible();
  await page.getByRole("button", { name: "ペットをドラッグして移動" }).click();
  await expect.poll(async () => (await nativePet(page)).some((call) => call.command === "drag_task_pet")).toBe(true);
  await page.getByRole("button", { name: "非表示" }).click();
  await expect.poll(async () => (await nativePet(page)).some((call) => call.command === "hide_task_pet")).toBe(true);
  expect(apiRequests).toEqual([]);
});
