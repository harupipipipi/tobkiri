import { taskPetHideAcknowledgment, taskPetHideActionOutcome } from "../test-support/taskPetHideAcknowledgment";
import { taskPetPreferenceKey } from "../src/lib/taskPet";
import { expect, test, type Page } from "@playwright/test";

async function openPet(page: Page) {
  const child = page.waitForEvent("popup");
  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("/pe");
  await page.getByRole("option", { name: "/pet", exact: true }).click();
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
  const preferenceKey = taskPetPreferenceKey("default", "enabled");
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), preferenceKey)).toBe("true");
  // Observe the actual trusted click and its exact source WindowProxy. These
  // test-only probes do not dispatch events, delay closing or alter application state.
  await page.evaluate(() => {
    const owner = window as Window & { __petHideProbe?: { source: Window | null; targetIsHide: boolean; trustedPrimaryClick: boolean; matchingHideMessage: boolean } };
    owner.__petHideProbe = { source: null, targetIsHide: false, trustedPrimaryClick: false, matchingHideMessage: false };
    const observe = (event: MessageEvent) => {
      const probe = owner.__petHideProbe;
      if (probe?.source && event.source === probe.source && event.origin === location.origin
        && event.data?.type === "task-pet-hidden" && event.data.profileId === "default") {
        probe.matchingHideMessage = true;
        window.removeEventListener("message", observe);
      }
    };
    window.addEventListener("message", observe);
  });
  await hideButton.evaluate((element) => {
    element.addEventListener("click", (event) => {
      const owner = window.opener as (Window & { __petHideProbe?: { source: Window | null; targetIsHide: boolean; trustedPrimaryClick: boolean; matchingHideMessage: boolean } }) | null;
      if (!owner?.__petHideProbe) return;
      owner.__petHideProbe.source = window;
      owner.__petHideProbe.targetIsHide = event.currentTarget === element
        && element.matches("button.task-pet-window-hide") && element.textContent?.trim() === "非表示";
      owner.__petHideProbe.trustedPrimaryClick = event.isTrusted && event instanceof MouseEvent && event.button === 0;
    }, { capture: true, once: true });
  });
  const childClose = popup.waitForEvent("close");
  const [clickOutcome, closeOutcome] = await Promise.allSettled([hideButton.click(), childClose]);
  taskPetHideActionOutcome(clickOutcome, closeOutcome);
  await expect.poll(() => page.evaluate(() => Boolean((window as Window & {
    __petHideProbe?: { matchingHideMessage: boolean };
  }).__petHideProbe?.matchingHideMessage))).toBe(true);
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), preferenceKey)).toBe("false");
  const clickEvidence = await page.evaluate(() => {
    const probe = (window as Window & { __petHideProbe?: { targetIsHide: boolean; trustedPrimaryClick: boolean; matchingHideMessage: boolean } }).__petHideProbe;
    return { targetIsHide: probe?.targetIsHide === true, trustedPrimaryClick: probe?.trustedPrimaryClick === true,
      matchingHideMessage: probe?.matchingHideMessage === true };
  });
  const evidence = { ...clickEvidence, childClosed: popup.isClosed(), parentOpen: !page.isClosed(),
    persistedHidden: await page.evaluate((key) => localStorage.getItem(key) === "false", preferenceKey) };
  const acknowledgment = taskPetHideAcknowledgment(clickOutcome, closeOutcome, evidence);
  await testInfo.attach("task-pet-hide-acknowledgment", {
    body: Buffer.from(JSON.stringify({ acknowledgment, evidence })), contentType: "application/json",
  });
  expect(page.url()).toBe(originalUrl);
  expect(popup.isClosed()).toBe(true);
  await expect(page.locator(".task-pet-launcher")).toHaveCount(0);
  await page.getByRole("button", { name: "親画面を操作" }).click();
  await expect(page.getByRole("status").filter({ hasText: /^親画面の操作回数:/ })).toHaveText("親画面の操作回数: 1");
  const restored = await openPet(page);
  expect(restored).not.toBe(popup);
  await expect(restored).toHaveURL(/\/p\/default\/chat\?surface=task-pet/);
  await expect(restored.locator(".task-pet-window-card")).toHaveAttribute("data-state", "completed");
  await expect(restored.getByText("タスクが完了しました")).toBeVisible();
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), preferenceKey)).toBe("true");
  await restored.close();
  await expect(page.locator(".task-pet-launcher")).toHaveCount(0);
  expect(page.isClosed()).toBe(false);
  expect(apiRequests).toEqual([]);
  await expect(page.getByTestId("chat-dispatches")).toHaveText("0");
  await expect(page.getByTestId("steer-dispatches")).toHaveText("0");
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

test("typed pet opens locally and escaped pet remains literal chat input", async ({ page }) => {
  await page.goto("/e2e/fixtures/task-pet.html");
  const child = page.waitForEvent("popup");
  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("/pet");
  await page.getByRole("button", { name: "/pet を実行", exact: true }).click();
  const popup = await child;
  await expect(popup.getByRole("main", { name: "Tobkiri ペット" })).toBeVisible();
  await expect(page.getByTestId("chat-dispatches")).toHaveText("0");
  await expect(page.locator(".task-pet-launcher")).toHaveCount(0);
  await popup.close();
  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("//pet");
  await expect(page.getByRole("option", { name: "/pet", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
  await expect(page.getByTestId("chat-dispatches")).toHaveText("1");
});

test("pet submission during generation bypasses steering and a missing provider key", async ({ page }) => {
  await page.goto("/e2e/fixtures/task-pet.html");
  await page.getByRole("button", { name: "生成中にする", exact: true }).click();
  await page.getByRole("button", { name: "APIキーなしにする", exact: true }).click();
  const child = page.waitForEvent("popup");
  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("/pet");
  await page.getByRole("button", { name: "/pet を実行", exact: true }).click();
  const popup = await child;
  await expect(popup.getByRole("main", { name: "Tobkiri ペット" })).toBeVisible();
  await expect(page.getByTestId("chat-dispatches")).toHaveText("0");
  await expect(page.getByTestId("steer-dispatches")).toHaveText("0");
  await popup.close();
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
  await expect(page.locator(".task-pet-launcher")).toHaveCount(0);
  await page.getByRole("button", { name: "タスクを完了" }).click();
  await expect.poll(async () => (await nativePet(page)).filter((call) => call.command === "sync_task_pet" && (call.args.presentation as { view: { mood: string } }).view.mood === "completed").length).toBeGreaterThan(0);
  expect((await nativePet(page)).filter((call) => call.command === "sync_task_pet").slice(1).every((call) => call.args.open === false)).toBe(true);
  const opensBeforeRestore = (await nativePet(page)).filter((call) => call.command === "sync_task_pet" && call.args.open === true).length;
  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("/pet");
  await page.getByRole("button", { name: "/pet を実行", exact: true }).click();
  await expect.poll(async () => (await nativePet(page)).filter((call) => call.command === "sync_task_pet" && call.args.open === true).length).toBe(opensBeforeRestore + 1);
  await expect(page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" })).toHaveValue("");
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
  const handle = page.getByRole("button", { name: "ペットをドラッグして移動" });
  await handle.evaluate((element) => {
    element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 }));
    window.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, button: 0 }));
  });
  expect((await nativePet(page)).filter((call) => call.command === "drag_task_pet")).toEqual([]);
  await handle.hover();
  await page.mouse.down();
  await expect.poll(async () => (await nativePet(page)).some((call) => call.command === "drag_task_pet")).toBe(true);
  await page.mouse.up();
  await page.getByRole("button", { name: "非表示" }).click();
  await expect.poll(async () => (await nativePet(page)).some((call) => call.command === "hide_task_pet")).toBe(true);
  expect(apiRequests).toEqual([]);
});
