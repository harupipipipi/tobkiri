import { createHistoryReferenceFixture, HistoryReferenceFixtureError } from "../test-support/historyReferenceFixture";
import { completedSavedTurnFixtureEvents, completedSavedTurnFixtureList, createConversationFixtureState, FixtureMutationError, settingsFixtureMutation } from "../test-support/uiContractMutationFixture";
import { expect, test, type Page, type Route, type Request, type Response, type Locator, type Frame } from "@playwright/test";
import { frontendFixtureBinding, frontendFixtureRequest, matchesFrontendFixtureBinding } from "../test-support/frontendContractFixture";
import { validSavedToolSelection, type ChatMessage, type ModelProfile, type SavedTurnRequest, type SavedTurnResult } from "../src/lib/api";
import { canonicalRequestQuery } from "./contractRequestMatcher";
import { frontendHostFixtureCatalog as dynamicHostCatalog, frontendHostFixtureScreenPath } from "../test-support/frontendHostFixture";
import { lateSavedProgressConversation, lateSavedProgressFixture, lateSavedProgressText } from "../test-support/lateSavedProgressFixture";

test.use({ viewport: { width: 1440, height: 900 } });

test("actual ChatApp tab loads keep selected identity and Stop scoped through delayed B and A return", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  const base = smokeConversation();
  const settled = {
    ...base, title: "Settled B", conversation_revision: 1,
    conversation_kind: null, tags: [], metadata: {},
    messages: base.messages.map((message, index) => ({
      ...message, content: [{ type: "text", text: index ? "Settled B reply" : "hello B" }],
      raw_text: index ? "Settled B reply" : "hello B", metadata: {},
    })),
  };
  const active = {
    ...settled, id: "c-pending-a", title: "Pending A", messages: [],
  };
  let releaseB!: () => void;
  const delayedB = new Promise<void>((resolve) => { releaseB = resolve; });
  let releaseStart!: () => void;
  const inflightStart = new Promise<void>((resolve) => { releaseStart = resolve; });
  let delayB = false;
  let delayedBReads = 0;
  let operationId: string | null = null;
  const stopped: string[] = [];
  const turn = () => ({
    id: operationId, conversation_id: active.id, status: "running", revision: 2,
  });
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = chatRequestKind(request);
    if (target === "listConversations") {
      return route.fulfill({ json: ok({ conversations: [{ ...settled, messages: [] }], total: 1, store_revision: 1 }) });
    }
    if (target === "createConversation") {
      return route.fulfill({ json: ok(active) });
    }
    if (target === "getConversation" && requestConversationId(request) === settled.id) {
      if (delayB) { delayedBReads += 1; await delayedB; }
      return route.fulfill({ json: ok(settled) });
    }
    if (target === "getConversation" && requestConversationId(request) === active.id) {
      return route.fulfill({ json: ok(active) });
    }
    if (target === "startTurn") {
      operationId = (request.postDataJSON() as { request: { turn_id: string } }).request.turn_id;
      await inflightStart;
      return route.fulfill({ json: ok({ status: "reconciliation_required", turn: turn() }) });
    }
    if (target === "turnEvents") {
      if (!operationId) return route.fulfill({ status: 404, json: { status: "error", error: { message: "not yet registered" } } });
      return route.fulfill({ json: ok({
        turn_id: operationId, operation_id: operationId, conversation_id: active.id,
        request_id: "saved-turn.ui-regression", turn_revision: 2, status: "running",
        turn: turn(), events: [], terminal: null,
      }) });
    }
    if (target === "turns") {
      return route.fulfill({ json: ok({ turns: operationId ? [turn()] : [] }) });
    }
    if (target === "reconcile") {
      return route.fulfill({ json: ok({ status: "reconciliation_required", turn: turn() }) });
    }
    if (target === "stop") {
      const id = (request.postDataJSON() as { turn_id: string }).turn_id;
      stopped.push(id);
      return route.fulfill({ json: ok({ turn_id: id, status: "cancellation_requested", stopped: false }) });
    }
    return route.fallback();
  });
  try {
    await page.goto("/p/defaults/chat");
    await expect(page.getByText("Settled B reply", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "New Chat", exact: true }).click();
    await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("Keep pending A separate");
    await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).press("Enter");
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toBeEnabled();
    await expect(page.getByRole("tab", { name: "Pending A", exact: true })).toBeVisible();
    delayB = true;
    await page.getByRole("tab", { name: "Settled B", exact: true }).click();
    await expect.poll(() => delayedBReads).toBeGreaterThan(0);
    await expect(page).toHaveURL(new RegExp(`chat=${settled.id}`));
    await expect(page.getByText("Settled B reply", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
    await expect(page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" })).toBeEnabled();
    await page.getByRole("tab", { name: "Pending A", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`chat=${active.id}`));
    await expect(page.getByText("Keep pending A separate", { exact: true })).toBeVisible();
    const lateResponse = page.waitForResponse((response) => requestConversationId(response.request()) === settled.id);
    releaseB();
    await lateResponse;
    await expect(page.getByRole("tab", { name: "Pending A", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tab", { name: "Settled B", exact: true })).toHaveAttribute("aria-selected", "false");
    await expect(page.getByText("Settled B reply", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Keep pending A separate", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`chat=${active.id}`));
    await page.getByRole("button", { name: "生成を停止", exact: true }).click();
    await expect.poll(() => stopped).toEqual([operationId]);
  } finally {
    releaseB();
    releaseStart();
  }
});

test("actual ChatApp waits for canonical registration and recovers a lost start reply without resending", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  const conversation = {
    ...smokeConversation(), id: "c-delayed-registration", title: "Delayed registration",
    conversation_revision: 1, conversation_kind: null, tags: [], metadata: {}, messages: [],
  };
  let releaseStart!: () => void;
  const pendingStart = new Promise<void>((resolve) => { releaseStart = resolve; });
  let releaseRegistrationRead!: () => void;
  const registrationRead = new Promise<void>((resolve) => { releaseRegistrationRead = resolve; });
  let registered = false;
  let delayRegistrationRead = true;
  let operationId: string | null = null;
  let starts = 0;
  let emptyListReads = 0;
  let registeredListReads = 0;
  let eventReads = 0;
  const controls: string[] = [];
  const turn = () => ({
    id: operationId, conversation_id: conversation.id, status: "running", revision: 2,
  });
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = chatRequestKind(request);
    if (target === "listConversations") {
      return route.fulfill({ json: ok({ conversations: [], total: 0, store_revision: 1 }) });
    }
    if (target === "createConversation") {
      return route.fulfill({ json: ok(conversation) });
    }
    if (target === "getConversation") {
      return route.fulfill({ json: ok(conversation) });
    }
    if (target === "startTurn") {
      starts += 1;
      operationId = (request.postDataJSON() as { request: { turn_id: string } }).request.turn_id;
      await pendingStart;
      return route.abort("failed");
    }
    if (target === "turns") {
      if (!registered) emptyListReads += 1;
      if (registered) {
        registeredListReads += 1;
        if (delayRegistrationRead) await registrationRead;
      }
      return route.fulfill({ json: ok({ turns: registered ? [turn()] : [] }) });
    }
    if (target === "turnEvents") {
      eventReads += 1;
      return route.fulfill({ json: ok({
        turn_id: operationId, operation_id: operationId, conversation_id: conversation.id,
        request_id: "saved-turn.registration-regression", turn_revision: 2, status: "running",
        turn: turn(), events: [], terminal: null,
      }) });
    }
    if (target === "reconcile") {
      return route.fulfill({ json: ok({ status: "reconciliation_required", turn: turn() }) });
    }
    if (target === "stop" || target === "steer") {
      controls.push(target);
      return route.fulfill({ json: ok({ turn_id: operationId, status: "cancellation_requested", stopped: false }) });
    }
    return route.fallback();
  });
  try {
    await page.goto("/p/defaults/chat");
    await page.getByRole("button", { name: "New Chat", exact: true }).click();
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await composer.fill("Wait for the owner to register this exact root");
    await composer.press("Enter");
    await expect.poll(() => emptyListReads).toBeGreaterThanOrEqual(2);
    expect(starts).toBe(1);
    expect(eventReads).toBe(0);
    expect(controls).toEqual([]);
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Copy backend connection error", exact: true })).toHaveCount(0);
    releaseStart();
    await expect(page.getByRole("alert").getByText("Failed to fetch - Tobkiri", { exact: true })).toBeVisible();
    registered = true;
    await expect.poll(() => registeredListReads).toBeGreaterThan(0);
    await page.getByRole("button", { name: "New Chat", exact: true }).click();
    const lateRegistration = page.waitForResponse((response) => chatRequestKind(response.request()) === "turns");
    delayRegistrationRead = false;
    releaseRegistrationRead();
    await lateRegistration;
    await expect(composer).toBeEnabled();
    expect(eventReads).toBe(0);
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
    await page.getByRole("tab", { name: "Delayed registration", exact: true }).click();
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toBeEnabled();
    expect(eventReads).toBeGreaterThan(0);
    expect(starts).toBe(1);
    await expect(page.getByRole("button", { name: "Copy backend connection error", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "生成を停止", exact: true }).click();
    await expect.poll(() => controls).toEqual(["stop"]);
  } finally {
    releaseStart();
    releaseRegistrationRead();
  }
});

for (const failure of ["transport", "auth", "foreign conversation", "duplicate root", "guidance descendant"] as const) {
  test(`actual ChatApp preserves registration failures for ${failure} without authorizing controls`, async ({ page }) => {
    await installDefaultspackApiMocks(page, { applicationChat: true });
    const conversation = {
      ...smokeConversation(), id: "c-rejected-registration", title: "Rejected registration",
      conversation_revision: 1, conversation_kind: null, tags: [], metadata: {}, messages: [],
    };
    let releaseStart!: () => void;
    const pendingStart = new Promise<void>((resolve) => { releaseStart = resolve; });
    let operationId: string | null = null;
    let starts = 0;
    let eventReads = 0;
    const controls: string[] = [];
    await page.route("**/api/contracts/defaultspack/**", async (route) => {
      const request = route.request();
      const target = chatRequestKind(request);
      if (target === "listConversations") {
        return route.fulfill({ json: ok({ conversations: [], total: 0, store_revision: 1 }) });
      }
      if (target === "createConversation") {
        return route.fulfill({ json: ok(conversation) });
      }
      if (target === "startTurn") {
        starts += 1;
        operationId = (request.postDataJSON() as { request: { turn_id: string } }).request.turn_id;
        await pendingStart;
        return route.abort("failed");
      }
      if (target === "turns") {
        if (failure === "transport") return route.abort("failed");
        if (failure === "auth") return route.fulfill({ status: 403, json: { success: false, error: "Unauthorized" } });
        const root = {
          id: operationId, conversation_id: conversation.id, status: "running", revision: 2,
        };
        const turns = failure === "foreign conversation"
          ? [{ ...root, conversation_id: "foreign-conversation" }]
          : failure === "duplicate root"
            ? [root, root]
            : [{ ...root, guidance_parent_turn_id: "different-root", guidance_source_turn_id: "different-root", guidance_id: "guidance-1" }];
        return route.fulfill({ json: ok({ turns }) });
      }
      if (target === "turnEvents") {
        eventReads += 1;
        return route.abort("failed");
      }
      if (target === "stop" || target === "steer") {
        controls.push(target);
        return route.abort("failed");
      }
      return route.fallback();
    });
    try {
      await page.goto("/p/defaults/chat");
      await page.getByRole("button", { name: "New Chat", exact: true }).click();
      const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
      await composer.fill("Keep this root pending until the exact owner is verified");
      await composer.press("Enter");
      await expect(page.getByRole("button", { name: "Copy backend connection error", exact: true })).toBeVisible();
      expect(starts).toBe(1);
      expect(eventReads).toBe(0);
      expect(controls).toEqual([]);
      await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
      await expect(page.getByText("Keep this root pending until the exact owner is verified", { exact: true })).toBeVisible();
    } finally {
      releaseStart();
    }
  });
}

test("actual ChatApp preserves an uncached selected root and draft while its record loads", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  const a = { ...smokeConversation(), title: "Loaded A", conversation_kind: null, tags: [], metadata: {}, conversation_revision: 1 };
  const b = { ...a, id: "c-uncached-b", title: "Uncached B", messages: [] };
  let releaseB!: () => void;
  const delayedB = new Promise<void>((resolve) => { releaseB = resolve; });
  let bReads = 0;
  const writes: string[] = [];
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = chatRequestKind(request);
    if (target === "listConversations") {
      return route.fulfill({ json: ok({ conversations: [a, b].map((item) => ({ ...item, messages: [] })), total: 2, store_revision: 1 }) });
    }
    if (target === "getConversation" && requestConversationId(request) === a.id) {
      return route.fulfill({ json: ok(a) });
    }
    if (target === "getConversation" && requestConversationId(request) === b.id) {
      bReads += 1;
      await delayedB;
      return route.fulfill({ json: ok(b) });
    }
    if (target === "createConversation" || target === "startTurn") {
      writes.push(target);
      return route.abort();
    }
    return route.fallback();
  });
  try {
    await page.goto(`/p/defaults/chat?chat=${a.id}`);
    await expect(page.getByTestId(`history-chat-card-${b.id}`)).toBeVisible();
    await expect(page.getByRole("tab", { name: "Loaded A", exact: true })).toBeVisible();
    await page.getByTestId(`history-chat-card-${b.id}`).click();
    await expect.poll(() => bReads).toBeGreaterThan(0);
    await expect(page).toHaveURL(new RegExp(`chat=${b.id}`));
    await expect(page.getByRole("tab", { name: "Uncached B", exact: true })).toHaveAttribute("aria-selected", "true");
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await expect(composer).toBeEnabled();
    await composer.fill("Keep this draft for B");
    await composer.press("Enter");
    await expect(page.getByRole("alert").getByText("会話を読み込んでいます。完了してから送信してください。 - Tobkiri", { exact: true })).toBeVisible();
    await expect(composer).toHaveValue("Keep this draft for B");
    expect(writes).toEqual([]);
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(new RegExp(`chat=${b.id}`));
    const response = page.waitForResponse((result) => requestConversationId(result.request()) === b.id);
    releaseB();
    await response;
    await expect(page.getByRole("tab", { name: "Uncached B", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(composer).toHaveValue("Keep this draft for B");
    await expect(page).toHaveURL(new RegExp(`chat=${b.id}`));
  } finally {
    releaseB();
  }
});

// These specs exercise mocked UI contracts only. Live MCP proof is covered by
// the Python integration tests that assert tool_logs and tool_call events.
const now = 1_785_000_000_000;
const approvalDigest = "a".repeat(64);
const historyReferenceDropEvent = "tobkiri:history-reference-drop";

for (const switchToB of [false, true]) {
  test(`actual ChatApp delayed model completion ${switchToB ? "preserves B tab body URL and draft" : "refreshes its owning A normally"}`, async ({ page }) => {
    await installDefaultspackApiMocks(page, { applicationChat: true });
    const conversation = (id: string, title: string, text: string) => ({
      ...smokeConversation(), id, title, conversation_revision: 1,
      conversation_kind: null, tags: [], metadata: {},
      messages: smokeConversation().messages.map((message, index) => ({
        ...message, conversation_id: id, metadata: {}, events: [], tool_logs: [],
        content: [{ type: "text", text: index ? text : `hello ${title}` }],
        raw_text: index ? text : `hello ${title}`,
      })),
    });
    let a = conversation("c-model-a", "Model A", "Original A reply");
    const b = conversation("c-model-b", "Model B", "B stays visible");
    let releaseCommand!: () => void;
    const commandGate = new Promise<void>((resolve) => { releaseCommand = resolve; });
    let releaseMutation!: () => void;
    const mutationGate = new Promise<void>((resolve) => { releaseMutation = resolve; });
    let commandSeen = false;
    const mutations: Array<Record<string, unknown>> = [];
    let completedMutation = false;
    let listsAfterMutation = 0;
    await page.route("**/api/contracts/defaultspack/**", async (route) => {
      const request = route.request();
        const target = chatRequestKind(request);
      if (target === "listConversations") {
        if (completedMutation) listsAfterMutation += 1;
        return route.fulfill({ json: ok({ conversations: [a, b], total: 2, store_revision: 1 }) });
      }
      if (target === "getConversation") {
        return route.fulfill({ json: ok(requestConversationId(request) === a.id ? a : b) });
      }
      if (target === "invokeCommand") {
        expect((request.postDataJSON() as Record<string, unknown>).conversation_id).toBe(a.id);
        commandSeen = true;
        await commandGate;
        return route.fulfill({ json: ok({
          status: "succeeded", operation_id: "command-model-a",
          legacy_result: { executed: true, requires_approval: false, selected_model: googleProfile.profile_id },
        }) });
      }
      if (target === "commandEvents") {
        return fulfillStreamEvents(route, [{ type: "completed", sequence: 1 }]);
      }
      if (target === "updateConversation") {
        const mutation = request.postDataJSON() as Record<string, unknown>;
        mutations.push(mutation);
        await mutationGate;
        a = { ...conversation(a.id, "Model A updated", "Updated A reply"), model: googleProfile.profile_id, conversation_revision: 2 };
        completedMutation = true;
        return route.fulfill({ json: ok(a) });
      }
      return route.fallback();
    });
    try {
      await page.goto(`/p/defaults/chat?chat=${a.id}`);
      await expect(page.getByText("Original A reply", { exact: true })).toBeVisible();
      const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
      await composer.fill(`/model ${googleProfile.profile_id}`);
      await composer.press("Enter");
      await expect.poll(() => commandSeen).toBe(true);
      if (switchToB) {
        await page.getByRole("button", { name: "New Chat", exact: true }).click();
        await page.getByTestId(`history-chat-card-${b.id}`).click();
        await expect(page.getByText("B stays visible", { exact: true })).toBeVisible();
        await composer.fill("B draft survives A completion");
      }
      releaseCommand();
      await expect.poll(() => mutations.length).toBe(1);
      expect(mutations[0]).toMatchObject({ conversation_id: a.id, expected_conversation_revision: 1, updates: { model: googleProfile.profile_id } });
      if (switchToB) await expect(composer).toHaveValue("B draft survives A completion");
      releaseMutation();
      await expect.poll(() => listsAfterMutation).toBeGreaterThan(0);
      if (switchToB) {
        await expect(page).toHaveURL(new RegExp(`chat=${b.id}`));
        await expect(page.getByRole("tab", { name: "Model B", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(page.getByText("B stays visible", { exact: true })).toBeVisible();
        await expect(page.getByText("Updated A reply", { exact: true })).toHaveCount(0);
        await expect(composer).toHaveValue("B draft survives A completion");
        await expect(composer).toBeEnabled();
        await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
      } else {
        await expect(page).toHaveURL(new RegExp(`chat=${a.id}`));
        await expect(page.getByRole("tab", { name: "Model A updated", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(page.getByText("Updated A reply", { exact: true })).toBeVisible();
        await expect(composer).toHaveValue("");
      }
    } finally {
      releaseCommand();
      releaseMutation();
    }
  });
}

function routeKey(path: string): string {
  return `/${path}`;
}

function requestTarget(url: URL): string {
  const marker = "/api/contracts/defaultspack/";
  if (!url.pathname.startsWith(marker)) return url.pathname;
  const operation = decodeURIComponent(url.pathname.slice(marker.length));
  const separator = operation.indexOf(" ");
  const target = separator < 0 ? operation : operation.slice(separator + 1);
  const queryIndex = target.indexOf("?");
  return queryIndex < 0 ? target : target.slice(0, queryIndex);
}

const chatRoutes = {
  listConversations: ["api/chat/conversations", "GET"],
  createConversation: ["api/chat/conversations", "POST"],
  getConversation: ["api/chat/conversation", "GET"],
  updateConversation: ["api/chat/conversation", "PUT"],
  startTurn: ["api/chat/turn", "POST"],
  turnEvents: ["api/chat/turn/events", "GET"],
  turns: ["api/chat/turns", "GET"],
  reconcile: ["api/chat/turn/reconcile", "POST"],
  stop: ["api/chat/turn/stop", "POST"],
  steer: ["api/chat/turn/steer", "POST"],
  invokeCommand: ["api/command-protocol/v1/invoke", "POST"],
  commandEvents: ["api/command-protocol/v1/invocations/command-model-a/events", "GET"],
} as const;

function chatRequestKind(request: Pick<Request, "url" | "method">): string {
  for (const [kind, [path, method]] of Object.entries(chatRoutes)) {
    if (canonicalRequestQuery(request, path, method) !== null) return kind;
  }
  return "unmatched";
}

function requestConversationId(request: Pick<Request, "url" | "method">): string | null {
  const query = canonicalRequestQuery(request, "api/chat/conversation", "GET");
  const ids = query?.getAll("conversation_id") ?? [];
  return ids.length === 1 ? ids[0] : null;
}

test("bootstrap loading state uses the Tobkiri Launcher animation and honors reduced motion", async ({ page }) => {
  let releaseCatalogRequest: (() => void) | undefined;
  const catalogGate = new Promise<void>((resolve) => {
    releaseCatalogRequest = resolve;
  });
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    await catalogGate;
    await route.abort();
  });

  await page.goto(frontendHostFixtureScreenPath("/chat"));

  const loader = page.locator("[data-tobkiri-loading-screen]").first();
  await expect(loader).toBeVisible();
  await expect(loader).toHaveAttribute("role", "status");
  await expect(loader).toHaveAttribute("aria-live", "polite");
  await expect(loader).toHaveAttribute("aria-label", "インターフェース本体を読み込んでいます…");
  await expect(loader).toHaveCSS("background-color", "rgb(9, 9, 11)");

  const animation = loader.locator('img[data-loading-scene="launcher"]');
  await expect(animation).toBeVisible();
  await expect(animation).toHaveAttribute(
    "src",
    /\/assets\/tobkiri-startup-blade-cut\.svg$/,
  );
  await expect.poll(
    () => animation.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0),
  ).toBe(true);

  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(animation).toBeHidden();
  await expect(loader.locator("[data-reduced-motion-wordmark]")).toBeVisible();

  releaseCatalogRequest?.();
});

test("keeps the startup boundary until slash commands and mention sources are ready", async ({ page }) => {
  let releaseCommands: (() => void) | undefined;
  const commandGate = new Promise<void>((resolve) => {
    releaseCommands = resolve;
  });
  await installDefaultspackApiMocks(page, {
    applicationChat: true,
    beforeCommandCatalogResponse: () => commandGate,
  });

  await page.goto(frontendHostFixtureScreenPath("/chat"));

  const loader = page.locator("[data-tobkiri-loading-screen]").first();
  await expect(loader).toBeVisible();
  await expect(loader.locator('[data-startup-step="commands"]')).toHaveAttribute("data-status", "loading");
  await expect(page.locator("textarea.rumi-composer-textarea")).toHaveCount(0);

  releaseCommands?.();

  await expect(loader).toBeHidden();
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await expect(composer).toBeVisible();

  await composer.fill("/");
  await expect(page.getByTestId("composer-slash-command-candidates")).toContainText("/coding");

  await composer.fill("@web");
  await expect(page.getByTestId("composer-at-mention-candidates")).toContainText("@Web Search");
});

test("verified Pack v4 conversation boots from the dynamic-host catalog", async ({ page }) => {
  await installDefaultspackApiMocks(page);

  await page.goto(frontendHostFixtureScreenPath("/chat"));

  await expect(page.locator('[data-rumi-frontend-host][data-plan-hash^="sha256:"]')).toBeVisible();
  await expect(page.locator('[data-conversation-surface="v4"]')).toBeVisible();
  await expect(page.getByRole("heading", { name: "Tobkiri Conversation" })).toBeVisible();
  const composer = page.getByRole("textbox", { name: "Message Tobkiri" });
  await composer.fill("Verify the current Pack v4 binding.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Pack v4 fixture response.", { exact: true })).toBeVisible();
});

type ApiMockOptions = {
  applicationChat?: boolean;
  calendarPreview?: boolean;
  historyReferences?: boolean;
  preserveLocalStorage?: boolean;
  runtimeProfileId?: "defaults" | "approval-other";
  beforeSettingsWriteResponse?: () => Promise<void>;
  settingsWriteResponse?: "conflict" | "unconfirmed";
  onSettingsWriteCommitted?: () => void;
  beforeCommandCatalogResponse?: () => Promise<void> | void;
  beforeWorkspaceFileReadResponse?: (payload: Record<string, unknown>) => Promise<void> | void;
  initialSettingsValues?: Record<string, Record<string, unknown>>;
  initialSelectedToolIds?: string[];
  initialPendingStorage?: Record<string, unknown>;
  onConversationCreate?: (payload: Record<string, unknown>) => void;
  onStreamRequest?: (payload: Record<string, unknown>) => void;
  onSavedTurnRequest?: (payload: { request: SavedTurnRequest }) => void;
  onSettingsWrite?: (payload: Record<string, unknown>) => void;
  beforeSavedTurnResponse?: () => Promise<void> | void;
  modelProfiles?: ModelProfile[];
  streamEvents?: (message: Record<string, unknown>) => Record<string, unknown>[];
  conversationMutator?: (conversation: ReturnType<typeof smokeConversation>) => void;
  onApprovalDecision?: (decision: "approve" | "deny", payload: Record<string, unknown>) => void;
  codingApprovalAfterTerminal?: boolean;
  codingApprovalAfterRestore?: boolean;
  structuredComposer?: boolean;
  interactiveApproval?: InteractiveApprovalFixture;
  onInteractiveApprovalRead?: (readCount: number) => Partial<InteractiveApprovalFixture> | void;
  onInteractiveApprovalDecision?: (decision: "approve" | "deny", payload: Record<string, unknown>) => void;
};

type InteractiveApprovalFixture = {
  request_id: string;
  request_snapshot_digest: string;
  state: string;
  expires_at: number;
  typed_confirmation_required: boolean;
  typed_confirmation_digest: string | null;
  redacted_metadata: Record<string, string>;
};

function ok(data: unknown) {
  // The HostBootstrap reads the canonical PackAPI `success` envelope
  // directly, while legacy resource clients validate the `status` envelope.
  // The runtime emits both projections during the migration, so the fixture
  // must do the same rather than accidentally testing a fallback path.
  return { status: "ok", success: true, data, error: null };
}

function smokeConversation() {
  return {
    id: "c-smoke",
    conversation_revision: 1,
    title: "Preview Calendar Chat",
    created_at: now - 60_000,
    updated_at: now,
    model: "stub/default",
    conversation_kind: "coding",
    tags: ["coding"],
    is_starred: false,
    is_pinned: false,
    is_archived: false,
    messages: [
      {
        id: "m-user",
        role: "user",
        content: [{ type: "text", text: "Show the current @Web Search state." }],
        raw_text: "Show the current @Web Search state.",
        created_at: now - 20_000,
        conversation_id: "c-smoke",
        parent_id: null,
        children_ids: [],
        sequence_number: 1,
        finish_reason: null,
        usage: null,
        widget: null,
        metadata: {
          mentions: [{
            id: "web_search",
            kind: "tool",
            label: "Web Search",
            syntax: "@Web Search",
          }],
        },
      },
      {
        id: "m-assistant",
        role: "assistant",
        content: [{ type: "text", text: "Preview smoke response with tool timeline." }],
        raw_text: "Preview smoke response with tool timeline.",
        created_at: now - 10_000,
        conversation_id: "c-smoke",
        parent_id: "m-user",
        children_ids: [],
        sequence_number: 2,
        finish_reason: "stop",
        usage: { total_tokens: 42 },
        widget: null,
        model: "stub/default",
        metadata: {
          timing: {
            thinking_started_at: now - 15_000,
            completed_at: now - 10_000,
          },
        },
        events: [
          {
            type: "tool_call_started",
            phase: "tool_call_started",
            tool_call_id: "call-files",
            tool_name: "coding_file_list",
            arguments: { path: "src" },
            timestamp: now - 14_000,
          },
          {
            type: "tool_call_completed",
            phase: "tool_call_completed",
            tool_call_id: "call-files",
            tool_name: "coding_file_list",
            arguments: { path: "src" },
            display_text: "Listed 2 files",
            next_step: "Ready for implementation",
            timestamp: now - 11_000,
          },
        ],
        tool_logs: [
          {
            tool_name: "web_search",
            tool_call_id: "call-web",
            arguments: { query: "defaultspack smoke" },
            result: { status: "ok", data: { summary: "1 result" } },
            timestamp: now - 9_000,
          },
        ],
      },
    ],
  };
}


const smokeProfile = {
  profile_id: "stub/default",
  qualified_model_id: "stub/default",
  provider_id: "stub",
  provider_display_name: "Stub",
  model_id: "default",
  display_name: "Stub Default",
  max_context: -1,
  max_context_tokens: -1,
  supports_thinking: false,
  supports_tool_calling: true,
  supports_vision: false,
  local: true,
  route_configured: true,
  availability: { local: true, configured: true },
};

const googleProfile = {
  profile_id: "google/gemini-2.5-flash",
  qualified_model_id: "google/gemini-2.5-flash",
  provider_id: "google",
  provider_display_name: "Google",
  model_id: "gemini-2.5-flash",
  display_name: "Gemini 2.5 Flash",
  max_context: 1_000_000,
  max_context_tokens: 1_000_000,
  supports_thinking: true,
  supports_tool_calling: true,
  supports_vision: true,
  local: false,
  availability: { configured: true },
};

const embeddingProfile = {
  profile_id: "google/text-embedding-004",
  qualified_model_id: "google/text-embedding-004",
  provider_id: "google",
  provider_display_name: "Google",
  model_id: "text-embedding-004",
  display_name: "Text Embedding 004",
  type: "embedding",
  max_context: 2048,
  max_context_tokens: 2048,
  supports_thinking: false,
  supports_tool_calling: false,
  supports_vision: false,
  local: false,
  configured: true,
  requires_api_key: false,
  capability_tags: ["embedding"],
  recommended_roles: ["tool_embedding"],
  availability: { configured: true },
};

const opencodeProfile = {
  profile_id: "opencode-go/qwen3.5-plus",
  qualified_model_id: "opencode-go/qwen3.5-plus",
  provider_id: "opencode-go",
  provider_display_name: "OpenCode Go",
  model_id: "qwen3.5-plus",
  display_name: "Qwen3.5 Plus via OpenCode Go",
  max_context: 128_000,
  max_context_tokens: 128_000,
  supports_thinking: false,
  supports_tool_calling: true,
  supports_vision: false,
  local: false,
  availability: { configured: true },
};

const opencodeZenProfile = {
  profile_id: "opencode-zen/minimax-m3-free",
  qualified_model_id: "opencode-zen/minimax-m3-free",
  provider_id: "opencode-zen",
  provider_display_name: "OpenCode Zen",
  model_id: "minimax-m3-free",
  display_name: "MiniMax M3 Free via OpenCode Zen",
  max_context: 200_000,
  max_context_tokens: 200_000,
  supports_thinking: true,
  supports_tool_calling: true,
  supports_vision: true,
  local: false,
  availability: { configured: false, status: "requires_api_key" },
};

const sidebarItems = [
  {
    id: "web_search",
    label: "Web Search",
    category: "tool",
    description: "Search the web from the composer.",
    tags: ["research"],
    risk: "medium",
    ui: {
      group_id: "research",
      group_label: "Research",
      group_icon: "search",
      item_icon: "web_search",
      widget_kind: "tool_toggle",
      drop_capabilities: ["composer.toggle_chip"],
      composer_label: "Web Search",
      composer_description: "Search the web.",
    },
    panel: {
      kind: "tool",
      title: "Web Search",
      notes: ["Mocked for Playwright smoke coverage."],
    },
  },
  {
    id: "github_issue_search",
    label: "GitHub Issues",
    category: "tool",
    description: "Search GitHub issues and pull requests.",
    tags: ["github", "issues"],
    risk: "medium",
    ui: {
      group_id: "github",
      group_label: "GitHub",
      item_icon: "git",
      service_id: "github",
      widget_kind: "tool_toggle",
      drop_capabilities: ["composer.toggle_chip"],
      composer_label: "GitHub Issues",
      composer_description: "Search GitHub issues.",
    },
    panel: {
      kind: "tool",
      title: "GitHub Issues",
      notes: ["Mocked for service-level selection coverage."],
    },
  },
  {
    id: "unicode_tool",
    label: "𐐀tool",
    category: "tool",
    description: "Supplementary-plane Unicode tool.",
    tags: ["unicode"],
    risk: "low",
    ui: {
      group_id: "research",
      group_label: "Research",
      widget_kind: "tool_toggle",
      drop_capabilities: ["composer.toggle_chip"],
      composer_label: "𐐀tool",
    },
  },
  {
    id: "scheduler",
    label: "Scheduler",
    category: "tool",
    description: "Schedule and trigger controls.",
    ui: { item_icon: "calendar" },
    panel: {
      kind: "actions",
      title: "Scheduler",
      notes: ["Calendar and trigger smoke surface."],
      actions: [
        {
          id: "schedules.list",
          label: "Calendar",
          icon: "schedules",
          method: "GET",
        },
      ],
    },
  },
];

const catalogSkills = [
  {
    id: "feedback/live-review",
    label: "Live Review",
    description: "Require evidence-backed verification.",
    triggers: ["PR97_LIVE_REALITY_REVIEW"],
    applies_to_tools: ["web_search"],
    aliases: ["reality", "live-review"],
  },
];

const settingsValues = {
  general: {
    language: "en",
    composer_placeholder: "Message Rumi...",
    keyboard_button_navigation: true,
    show_activity_in_messages: true,
  },
  models: {
    preferred_model: "stub/default",
    favorite_profiles: ["stub/default"],
  },
  preview: {
    max_items: 12,
    auto_open: false,
    default_mode: "auto",
  },
  calendar: {
    agent_current_chat: false,
    agent_model: "",
    agent_task_default: false,
    default_time: "09:00",
    quick_add_enabled: true,
    default_item_type: "task",
    week_start: "sunday",
    show_outside_days: true,
    show_time_picker: true,
    dim_weekends: true,
    task_color: "blue",
    time_slot_minutes: 15,
    event_color: "green",
    max_items_per_day: 3,
  },
  chat_rendering: {
    unknown_block_strategy: "hidden",
    show_widgets: true,
  },
  sidebar: {
    pinned_item_ids: ["web_search", "scheduler"],
    starred_item_ids: [],
    custom_tool_tags: {},
  },
  tools: {
    default_mode: "auto",
    selection_strategy: "hybrid",
    semantic_candidate_limit: 24,
    final_tool_limit: 8,
    catalog_ai_direct_limit: 80,
    selector_trace: "summary",
    standard_permissions: {
      read: "auto",
      search: "auto",
      create: "confirm",
      update: "confirm",
      send: "confirm",
      execute: "confirm",
      computer: "confirm",
      delete: "confirm",
    },
    service_permission_overrides: {},
    embedding_model: "",
  },
  commands: {},
};

const settingsSections = [
  {
    id: "general",
    label: "General",
    description: "App behavior.",
    fields: [{
      id: "manual_runtime_mode_selection",
      label: "Manual Runtime Mode Selection",
      type: "toggle",
      default: false,
      advanced: true,
      control_center_section: "advanced",
    }],
  },
  {
    id: "tools",
    label: "機能と接続",
    description: "機能の選定、接続、実行時権限を管理します。",
    fields: [],
  },
  {
    id: "calendar",
    label: "カレンダー",
    description: "Calendar behavior.",
    fields: Array.from({ length: 14 }, (_, index) => ({
      id: `calendar_field_${index + 1}`,
      label: `Calendar Field ${index + 1}`,
      type: "text",
      default: "",
    })),
  },
];

const toolCatalogServices = [
  {
    service_id: "web",
    label: "Web検索",
    summary: "Web、検索、オンライン情報を扱います",
    connection_status: "connected",
    tool_count: 1,
    action_classes: ["search"],
  },
  {
    service_id: "github",
    label: "GitHub",
    summary: "リポジトリ、Issue、Pull Requestを扱います",
    connection_status: "connected",
    tool_count: 1,
    action_classes: ["search"],
  },
  {
    service_id: "calendar",
    label: "Calendar",
    summary: "予定やカレンダーを扱います",
    connection_status: "connected",
    tool_count: 1,
    action_classes: ["read"],
  },
];

const toolCatalogTools = [
  {
    tool_id: "web_search",
    service_id: "web",
    service_label: "Web検索",
    name: "Web Search",
    summary: "Search the web.",
    action_class: "search",
    risk: "medium",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["research"],
  },
  {
    tool_id: "github_issue_search",
    service_id: "github",
    service_label: "GitHub",
    name: "GitHub Issues",
    summary: "Search GitHub issues and pull requests.",
    action_class: "search",
    risk: "medium",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["github"],
  },
  {
    tool_id: "unicode_tool",
    service_id: "unicode",
    service_label: "Unicode",
    name: "𐐀tool",
    summary: "Supplementary-plane Unicode tool.",
    action_class: "read",
    risk: "low",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["unicode"],
  },
  {
    tool_id: "scheduler",
    service_id: "calendar",
    service_label: "Calendar",
    name: "Scheduler",
    summary: "Schedule and trigger controls.",
    action_class: "read",
    risk: "low",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["calendar"],
  },
];

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(ok(data)),
  });
}

async function fulfillStream(route: Route, message: Record<string, unknown>) {
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: [
      `data: ${JSON.stringify({ type: "message", message })}`,
      "",
      `data: ${JSON.stringify({ type: "done", message })}`,
      "",
    ].join("\n"),
  });
}

async function fulfillStreamEvents(route: Route, events: Record<string, unknown>[]) {
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
  });
}

async function installDefaultspackApiMocks(page: Page, options: ApiMockOptions = {}) {
  if (options.applicationChat) {
    await page.route("**/health", (route) => fulfill(route, { status: "ok", saved_turn_store_id: `sha256:${"a".repeat(64)}` }));
  }
  await page.addInitScript(({ selectedToolIds, pendingStorage, runtimeProfileId, preserveLocalStorage }: { selectedToolIds: string[]; pendingStorage?: Record<string, unknown>; runtimeProfileId: string; preserveLocalStorage: boolean }) => {
    if (!preserveLocalStorage) localStorage.clear();
    sessionStorage.clear();
    // The floating pet is covered by component tests. Keep contract-test
    // pointer targets deterministic while exercising chat controls.
    localStorage.setItem(`tobkiri.task-pet.enabled.v2:${runtimeProfileId}`, "false");
    if (pendingStorage) localStorage.setItem(`rumi-pending-chat-v2:${runtimeProfileId}`, JSON.stringify(pendingStorage));
    if (selectedToolIds.length) {
      localStorage.setItem("rumi-selected-tool-ids", JSON.stringify(selectedToolIds));
    }
  }, { selectedToolIds: options.initialSelectedToolIds ?? [], pendingStorage: options.initialPendingStorage, runtimeProfileId: options.runtimeProfileId ?? "defaults", preserveLocalStorage: options.preserveLocalStorage === true });
  await page.addInitScript(() => {
    const fixtureWindow = window as Window & {
      __approvalRendererFixture?: {
        tauriBridgeCalls: Array<{ command: string; args?: Record<string, unknown> }>;
      };
    };
    fixtureWindow.__approvalRendererFixture = { tauriBridgeCalls: [] };
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: {
        core: {
          invoke: async (command: string, args?: Record<string, unknown>) => {
            fixtureWindow.__approvalRendererFixture?.tauriBridgeCalls.push({ command, args });
            if (command === "get_desktop_system_info") {
              return {
                source: "viewer_tauri",
                reliable: true,
                app_name: "Tobkiri",
                display_version: "ui-contract",
                viewer_version: "ui-contract",
                build_channel: "test",
                platform: "linux",
                platform_release: "ui-contract",
                permission_subject: "Tobkiri Launcher",
                permissions: [],
              };
            }
            if (command === "authority_approval_context") {
              const requestId = String(args?.requestId ?? "");
              const interactive = args?.decision === "approve" || args?.decision === "deny";
              return {
                request_id: requestId,
                ui_operator: {
                  version: interactive ? 3 : 1,
                  kind: "ui_operator",
                  origin: "tauri://tobkiri-launcher",
                  window_label: "authority-approval",
                  request_id: requestId,
                  ...(interactive ? {
                    decision: args?.decision,
                    request_snapshot_digest: args?.requestSnapshotDigest,
                    typed_confirmation_digest: args?.typedConfirmationDigest,
                  } : {}),
                  issued_at: Math.floor(Date.now() / 1000),
                  expires_at: Math.floor(Date.now() / 1000) + 30,
                  nonce: "e2e-ui-operator-nonce",
                  signature: "e2e-ui-operator-signature",
                },
              };
            }
            if (command === "close_current_window") {
              return undefined;
            }
            if (command === "coding_approval_operator") {
              return {
                request_id: args?.requestId,
                expected_digest: args?.expectedDigest,
                decision: args?.decision,
                operator: "ui-contract-fixture",
              };
            }
            throw new Error(`Unexpected Tauri bridge command: ${command}`);
          },
        },
      },
    });
  });

  let currentSettingsValues: Record<string, Record<string, unknown>> = JSON.parse(JSON.stringify({
    ...settingsValues,
    ...(options.initialSettingsValues ?? {}),
    general: {
      ...settingsValues.general,
      ...(options.initialSettingsValues?.general ?? {}),
    },
  }));
  let settingsRevision = 1;
  const historyReferenceFixture = options.historyReferences ? createHistoryReferenceFixture() : null;
  const initialConversation = { ...smokeConversation(), metadata: {} as Record<string, unknown> };
  options.conversationMutator?.(initialConversation);
  const conversationState = createConversationFixtureState(initialConversation.conversation_revision, initialConversation.metadata);
  let completedSavedTurn: SavedTurnRequest | null = null;
  let completedSavedTurnResult: SavedTurnResult | null = null;
  let codingApprovalRequest: Record<string, unknown> | null = null;
  let interactiveApprovalRequest: InteractiveApprovalFixture | null = options.interactiveApproval
    ? {
      ...options.interactiveApproval,
      redacted_metadata: { ...options.interactiveApproval.redacted_metadata },
    }
    : null;
  let interactiveApprovalReadCount = 0;
  const settledApprovalRequestIds = new Set<string>();
  const codingCheckpoints: Record<string, unknown>[] = options.codingApprovalAfterRestore
    ? [{ snapshot_id: "checkpoint-1", path: "/repo/.rumi/checkpoints/checkpoint-1" }]
    : [];
  const mcpServers = [
    { server_id: "filesystem", name: "Filesystem MCP", transport: "stdio", connected: true, permissions: { approved: true }, tools: ["mcp_fs_read_file"] },
  ];

  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = requestTarget(url);
    const method = request.method();
    const conversation = { ...structuredClone(initialConversation), ...conversationState.snapshot() };
    if (completedSavedTurn) {
      const request = completedSavedTurn;
      const userText = typeof request.content === "string" ? request.content : request.content[0].text;
      const savedMessages: ChatMessage[] = [
        {
          id: "m-saved-user",
          role: "user",
          content: [{ type: "text", text: userText }],
          raw_text: userText,
          created_at: now + 1_000,
          conversation_id: request.conversation_id,
          sequence_number: 3,
          metadata: { turn_id: request.turn_id },
        },
        {
          id: "m-saved-assistant",
          role: "assistant",
          content: [{ type: "text", text: "Saved response accepted." }],
          raw_text: "Saved response accepted.",
          created_at: now + 2_000,
          conversation_id: request.conversation_id,
          sequence_number: 4,
          finish_reason: "stop",
          metadata: { turn_id: request.turn_id },
        },
      ];
      (conversation.messages as ChatMessage[]).push(...savedMessages);
    }
    const conversationMessages = conversation.messages as Array<{ events?: Record<string, unknown>[] }>;
    for (const message of conversationMessages) {
      if (!message.events) continue;
      message.events = message.events.filter((event) => (
        !settledApprovalRequestIds.has(String(event.approval_request_id ?? "").trim())
      ));
    }
    const approvalEvent = conversationMessages
      .flatMap((message) => message.events ?? [])
      .find((event) => String(event.approval_request_id ?? "").trim());
    const approvalEventId = String(approvalEvent?.approval_request_id ?? "").trim();
    if (!codingApprovalRequest && approvalEventId) {
      codingApprovalRequest = {
        request_id: approvalEventId,
        operation: String(approvalEvent?.action ?? "browser.open_url"),
        risk_level: String(approvalEvent?.risk_level ?? "medium"),
        status: "pending",
        display_summary: String(approvalEvent?.display_summary ?? "Browser action requires approval."),
        created_at: now,
        args_hash: approvalDigest,
      };
    }

    if (path === routeKey("api/health")) {
      return fulfill(route, { status: "ok", pack: "defaultspack", ts: "2026-05-20T00:00:00Z" });
    }

    if (path === routeKey("api/ui/catalog") || path === routeKey("api/ui/full-catalog")) {
      return fulfill(route, {
        dynamic_host: dynamicHostCatalog(options.applicationChat, options.runtimeProfileId),
        app: { id: "defaultspack", name: "Rumi", account: { display_name: "Smoke User", plan_label: "Local" } },
        agent_service: { profiles: [], capabilities: [], presets: [] },
        sidebar: {
          filters: [
            { id: "all", label: "All" },
            { id: "tool", label: "Tools" },
            { id: "system", label: "System" },
          ],
          items: sidebarItems,
        },
        settings: { sections: settingsSections, values: currentSettingsValues },
        chat_rendering: { renderers: [] },
        composer_inputs: options.structuredComposer ? [{
          id: "contract_composer",
          label: "入力オプション",
          description: "送信時の補助情報を設定します。",
          modes: ["chat", "coding", "agent"],
          enabled: true,
          fields: [
            { id: "intent", type: "select", label: "目的", default: "review", options: [{ value: "review", label: "レビュー" }] },
            { id: "detail", type: "select", label: "詳細度", default: "rich", options: [{ value: "rich", label: "リッチ" }] },
            { id: "tone", type: "select", label: "文体", default: "natural", options: [{ value: "natural", label: "自然" }] },
            { id: "note", type: "text", label: "補足", placeholder: "任意の補足" },
          ],
        }] : [],
        skills: catalogSkills,
        extension_points: [],
      });
    }

    if (path === routeKey("api/ui/capability/invoke") && method === "POST") {
      return fulfill(route, {
        content: [{ type: "text", text: "Pack v4 fixture response." }],
      });
    }

    if (path === routeKey("api/ui/settings") && method === "PUT") {
      const payload: unknown = request.postDataJSON();
      options.onSettingsWrite?.(payload as Record<string, unknown>);
      try {
        settingsFixtureMutation(payload, settingsRevision);
        await options.beforeSettingsWriteResponse?.();
        if (options.settingsWriteResponse === "conflict") {
          return route.fulfill({ status: 409, json: { status: "error", success: false, error: "Settings revision conflict" } });
        }
        if (options.settingsWriteResponse === "unconfirmed") {
          // An HTTP success without a canonical receipt is not an acknowledged write.
          return route.fulfill({ status: 202, json: { status: "pending", success: true, data: { pending: true }, error: null } });
        }
        // Recheck CAS after a delayed response gate, before mutating fixture state.
        const receipt = settingsFixtureMutation(payload, settingsRevision);
        for (const [section, values] of Object.entries(receipt.values)) {
          currentSettingsValues[section] = { ...currentSettingsValues[section], ...values };
        }
        settingsRevision = receipt.document_revision;
        // This observes fixture state commit, not receipt delivery to the client.
        options.onSettingsWriteCommitted?.();
        await fulfill(route, receipt);
        return;
      } catch (error) {
        if (!(error instanceof FixtureMutationError)) throw error;
        return route.fulfill({ status: error.status, json: { status: "error", success: false, error: error.message } });
      }
    }

    if (path === routeKey("api/ui/settings")) {
      return fulfill(route, {
        sections: settingsSections, values: currentSettingsValues,
        document_revision: settingsRevision,
      });
    }

    if (options.applicationChat && path === routeKey("api/ai/strategies")) {
      return fulfill(route, {
        api_version: "strategy.catalog.v1", strategies: [], count: 0,
        catalog_revision: "fixture-r1", diagnostics: [], quarantined_pack_ids: [],
      });
    }

    if (options.applicationChat && path === routeKey("api/projects")) {
      return fulfill(route, { namespace: "defaultspack.projects.v1", revision: 0, projects: [] });
    }

    if (options.applicationChat && /^\/api\/ui\/conversations\/[^/]+\/preview$/.test(path)
      && !(options.calendarPreview && path === routeKey("api/ui/conversations/c-smoke/preview"))) {
      return fulfill(route, { conversation_id: path.split("/")[4], previews: [], summary: {} });
    }

    if (path === routeKey("api/command-protocol/v1/catalog")) {
      await options.beforeCommandCatalogResponse?.();
      const protocolCommand = (
        id: string,
        label: string,
        risk: "low" | "medium",
        operationRef: string,
      ) => ({
        canonical_id: `defaultspack:${id}`,
        pack_id: "defaultspack",
        pack_generation: 1,
        command_version: "1.0.0",
        identity: { id, name: id, aliases: [] },
        presentation: {
          label: { fallback: label },
          description: { fallback: `Toggle ${label}.` },
          category: "mode",
          visibility: "default",
          input: { kind: "action" },
          mounts: [],
        },
        execution: { kind: "host_operation", operation_ref: `host:${operationRef}` },
        authorization: {
          risk,
          permissions: [],
          approval_required: false,
          approval_policy: "never",
          executor_policy_ref: "defaultspack.e2e",
        },
        constraints: { modes: ["chat", "coding", "agent"] },
        availability: { status: "available" },
      });
      return fulfill(route, {
        api_version: "tobkiri.commands/v1",
        kind: "ResolvedCommandCatalog",
        catalog_revision: "e2e-revision-1",
        commands: [
          protocolCommand("coding", "Coding Mode", "low", "set_mode_coding"),
          protocolCommand("yolo", "Full Access (YOLO)", "medium", "toggle_ultra_yolo"),
          ...(options.applicationChat ? [{
            ...protocolCommand("model", "Model", "low", "open_model_picker"),
            presentation: {
              ...protocolCommand("model", "Model", "low", "open_model_picker").presentation,
              input: { kind: "search_select", datasource_ref: "tobkiri:model_catalog", argument: "query" },
            },
          }] : []),
        ],
        state_snapshots: [],
        diagnostics: [],
      });
    }

    if (path === routeKey("api/ui/commands")) {
      return fulfill(route, {
        commands: [
          {
            id: "coding",
            name: "coding",
            label: "Coding Mode",
            description: "Toggle coding mode.",
            category: "mode",
            visibility: "default",
            risk: "low",
            modes: ["chat", "coding", "agent"],
            execution: { type: "frontend", action: "set_mode_coding" },
          },
          {
            id: "yolo",
            name: "yolo",
            label: "Full Access (YOLO)",
            description: "Toggle Full Access and Ask approval.",
            category: "mode",
            visibility: "default",
            risk: "medium",
            modes: ["chat", "coding", "agent"],
            execution: { type: "frontend", action: "toggle_ultra_yolo" },
          },
        ],
      });
    }

    if (path === routeKey("api/ui/commands/execute") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      return fulfill(route, {
        executed: true,
        action: payload.command === "coding"
          ? "set_mode_coding"
          : payload.command === "yolo"
            ? "toggle_ultra_yolo"
            : "",
      });
    }

    if (path === routeKey("api/ai/profiles")) {
      const profiles = options.modelProfiles ?? [smokeProfile, googleProfile, opencodeProfile, opencodeZenProfile];
      return fulfill(route, { profiles, count: profiles.length, registry_revision: 1 });
    }

    if (path === routeKey("api/ai/strategies")) {
      return fulfill(route, {
        api_version: "tobkiri.strategies/v1",
        strategies: [],
        count: 0,
        catalog_revision: "e2e-strategy-revision-1",
        diagnostics: [],
        quarantined_pack_ids: [],
      });
    }

    if (path === routeKey("api/ai/models/search") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const types = Array.isArray(payload.type)
        ? payload.type.map((item) => String(item).trim())
        : [String(payload.type ?? "").trim()];
      const models = types.includes("embedding")
        ? [embeddingProfile]
        : [smokeProfile, googleProfile, opencodeProfile, opencodeZenProfile];
      return fulfill(route, { models, count: models.length });
    }

    if (path === routeKey("api/tools/catalog")) {
      return fulfill(route, {
        services: toolCatalogServices,
        tools: toolCatalogTools,
        count: toolCatalogTools.length,
      });
    }

    if (path === routeKey("api/tools/selection/preview") && method === "POST") {
      return fulfill(route, {
        preview_id: "preview-tool-selection",
        expires_at: "2026-05-20T00:05:00Z",
        decision: {
          selected_tools: ["web_search", "github_issue_search"],
          selected_services: toolCatalogServices.slice(0, 2),
          recommendations: [
            { tool_id: "web_search", confidence: 0.8, reason: "web search requested" },
            { tool_id: "github_issue_search", confidence: 0.7, reason: "GitHub context requested" },
          ],
          permission_summary: { auto: 2, confirm: 0, block: 0 },
          metadata: {},
        },
      });
    }

    if (historyReferenceFixture) {
      const binding = frontendFixtureRequest(request.url(), method);
      const list = frontendFixtureBinding("chatReferencesList");
      const resolve = frontendFixtureBinding("chatReferencesResolve");
      const listQuery = canonicalRequestQuery(request, "api/chat/references", "GET");
      const resolveQuery = canonicalRequestQuery(request, "api/chat/references/resolve", "POST");
      if (listQuery !== null || resolveQuery !== null) {
        try {
          if (listQuery !== null && matchesFrontendFixtureBinding(binding, list)) {
            return fulfill(route, historyReferenceFixture.list(listQuery));
          }
          if (resolveQuery !== null && matchesFrontendFixtureBinding(binding, resolve) && [...resolveQuery].length === 0) {
            return fulfill(route, historyReferenceFixture.resolve(request.postDataJSON()));
          }
          throw new HistoryReferenceFixtureError(400, "History reference fixture binding does not match");
        } catch (error) {
          if (!(error instanceof HistoryReferenceFixtureError)) throw error;
          return route.fulfill({ status: error.status, json: { status: "error", success: false, error: error.message } });
        }
      }
    }

    if (path === routeKey("api/chat/conversations") && method === "GET") {
      return fulfill(route, { conversations: [{ ...conversation, messages: [] }], total: 1, store_revision: 1 });
    }

    if (path === routeKey("api/chat/conversations") && method === "POST") {
      options.onConversationCreate?.(request.postDataJSON() as Record<string, unknown>);
      return fulfill(route, conversation);
    }

    if (path === routeKey("api/chat/conversation") && method === "PUT") {
      try {
        const mutation = conversationState.mutate(request.postDataJSON());
        return fulfill(route, { ...conversation, ...mutation });
      } catch (error) {
        if (!(error instanceof FixtureMutationError)) throw error;
        return route.fulfill({ status: error.status, json: { status: "error", success: false, error: error.message } });
      }
    }

    if (path === routeKey("api/chat/conversation") && method === "GET") {
      return fulfill(route, conversation);
    }

    if (path === routeKey("api/chat/turn") && method === "POST") {
      const payload = request.postDataJSON() as { request: SavedTurnRequest };
      options.onSavedTurnRequest?.(payload);
      await options.beforeSavedTurnResponse?.();
      let committedRevision: number;
      try {
        committedRevision = conversationState.completeTurn(payload.request.conversation_id, payload.request.conversation_revision).conversation_revision;
      } catch (error) {
        if (!(error instanceof FixtureMutationError)) throw error;
        return route.fulfill({ status: error.status, json: { status: "error", success: false, error: error.message } });
      }
      completedSavedTurn = payload.request;
      const result: SavedTurnResult = {
        status: "completed",
        turn: {
          id: payload.request.turn_id,
          conversation_id: payload.request.conversation_id,
          status: "completed",
          revision: 3,
          request_id: `saved-turn.${payload.request.turn_id}`,
          events: [],
          result_reference: {
            conversation_id: payload.request.conversation_id,
            conversation_revision: committedRevision,
            user_message_id: "m-saved-user",
            assistant_message_id: "m-saved-assistant",
            outcome_digest: `sha256:${"f".repeat(64)}`,
          },
        },
      };
      completedSavedTurnResult = result;
      return fulfill(route, result);
    }

    const turnListQuery = canonicalRequestQuery(request, "api/chat/turns", "GET");
    if (turnListQuery !== null) {
      try {
        return fulfill(route, completedSavedTurnFixtureList(turnListQuery, completedSavedTurnResult?.turn ?? null));
      } catch (error) {
        if (!(error instanceof FixtureMutationError)) throw error;
        return route.fulfill({ status: error.status, json: { status: "error", success: false, error: error.message } });
      }
    }

    const turnEventsQuery = canonicalRequestQuery(request, "api/chat/turn/events", "GET");
    if (turnEventsQuery !== null) {
      try {
        return fulfill(route, completedSavedTurnFixtureEvents(turnEventsQuery, completedSavedTurnResult?.turn ?? null));
      } catch (error) {
        if (!(error instanceof FixtureMutationError)) throw error;
        return route.fulfill({ status: error.status, json: { status: "error", success: false, error: error.message } });
      }
    }

    if (path === routeKey("api/command-protocol/v1/invocations/events/query") && method === "POST") {
      return fulfill(route, {
        api_version: "command-protocol/v1",
        pending_approvals: [],
      });
    }

    // The application restores any pending high-risk command during bootstrap.
    // Keep this fixture aligned with the V4 interactive-approval adapter: an
    // empty invocation list is an explicit successful response, not an absent
    // legacy `pending_approvals` field.
    if (path === routeKey("api/command-protocol/v1/high-risk") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      if (payload.phase === "list_pending") {
        return fulfill(route, { invocations: [] });
      }
      return fulfill(route, {});
    }

    if (path === routeKey("api/interactive-approval/v1/list")) {
      return fulfill(route, {
        approvals: interactiveApprovalRequest ? [interactiveApprovalRequest] : [],
      });
    }

    if (path === routeKey("api/interactive-approval/v1/get") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      if (!interactiveApprovalRequest) return fulfill(route, {});
      interactiveApprovalReadCount += 1;
      interactiveApprovalRequest = {
        ...interactiveApprovalRequest,
        ...(options.onInteractiveApprovalRead?.(interactiveApprovalReadCount) ?? {}),
      };
      if (payload.request_id !== interactiveApprovalRequest.request_id) {
        return fulfill(route, { ...interactiveApprovalRequest, request_id: String(payload.request_id ?? "") });
      }
      return fulfill(route, interactiveApprovalRequest);
    }

    if (path === routeKey("api/interactive-approval/v1/approve") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onInteractiveApprovalDecision?.("approve", payload);
      if (interactiveApprovalRequest && payload.request_id === interactiveApprovalRequest.request_id) {
        interactiveApprovalRequest = { ...interactiveApprovalRequest, state: "approved" };
      }
      return fulfill(route, interactiveApprovalRequest ?? {});
    }

    if (path === routeKey("api/interactive-approval/v1/deny") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onInteractiveApprovalDecision?.("deny", payload);
      if (interactiveApprovalRequest && payload.request_id === interactiveApprovalRequest.request_id) {
        interactiveApprovalRequest = { ...interactiveApprovalRequest, state: "denied" };
      }
      return fulfill(route, interactiveApprovalRequest ?? {});
    }

    if (path === routeKey("api/chat/conversations/c-smoke/stream") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onStreamRequest?.(payload);
      const message = {
        id: "m-assistant-streamed",
        role: "assistant",
        content: [{ type: "text", text: "Structured response accepted." }],
        raw_text: "Structured response accepted.",
        created_at: now + 1_000,
        conversation_id: "c-smoke",
        parent_id: "m-user-sent",
        children_ids: [],
        sequence_number: 4,
        finish_reason: "stop",
        usage: null,
        widget: null,
        model: "stub/default",
        metadata: {},
        events: [],
        tool_logs: [],
      };
      if (options.streamEvents) {
        return fulfillStreamEvents(route, options.streamEvents(message));
      }
      return fulfillStream(route, message);
    }

    if (path === routeKey("api/chat/conversations/c-smoke")) {
      return fulfill(route, conversation);
    }

    if (path === routeKey("api/ui/conversations/c-smoke/preview")) {
      if (!options.calendarPreview) return fulfill(route, { conversation_id: "c-smoke", previews: [], summary: {} });
      return fulfill(route, {
        conversation_id: "c-smoke",
        previews: [
          {
            id: "preview-calendar",
            toolStepId: "call-files",
            timestamp: now - 8_000,
            data: {
              type: "file",
              filename: "calendar-smoke.json",
              size: "tool artifact",
              content: '{ "job": "nightly-review", "status": "ready" }',
            },
          },
        ],
        summary: { file: 1 },
      });
    }

    if (path === routeKey("api/chat/steer")) {
      return fulfill(route, { items: [] });
    }

    if (path === routeKey("api/agent/schedules")) {
      return fulfill(route, {
        schedules: [
          { id: "nightly-review", name: "nightly-review", schedule: "every 1h", next_run_at: "2026-05-20T12:00:00Z" },
        ],
      });
    }

    if (path === routeKey("api/coding/workspaces")) {
      return fulfill(route, {
        workspaces: [{ workspace_id: "ws-main", label: "Main Repo", root_path: "/repo", trusted: true }],
        selected_workspace_id: "ws-main",
      });
    }

    if (path === routeKey("api/coding/context")) {
      return fulfill(route, {
        branch: "main",
        root_folder: "/repo",
        workspace_id: "ws-main",
        workspace_root: "/repo",
        directory: ".",
        files: ["src/App.tsx", "README.md"],
        entries: [
          { name: "src", path: "src", is_dir: true, size: 0 },
          { name: "README.md", path: "README.md", is_dir: false, size: 200 },
        ],
        git: { branch: "main", clean: false, modified: ["src/App.tsx"], untracked: [], staged: [] },
      });
    }

    if (path === routeKey("api/coding/files/read") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      await options.beforeWorkspaceFileReadResponse?.(payload);
      return fulfill(route, {
        path: String(payload.path ?? "README.md"),
        content: "# Fixture\n",
        size: 10,
        encoding: "utf-8",
        workspace_id: "ws-main",
        workspace_root: "/repo",
      });
    }

    if (path === routeKey("api/coding/git/branch")) {
      return fulfill(route, { branch: "main", branches: ["main", "codex/pr97"], workspace_id: "ws-main" });
    }

    if (path === routeKey("api/coding/git/status")) {
      return fulfill(route, { branch: "main", clean: false, modified: ["src/App.tsx"], untracked: [], staged: [] });
    }

    if (path === routeKey("api/coding/git/diff")) {
      return fulfill(route, { diff: "-old\n+new", files_changed: 1, files: ["src/App.tsx"], workspace_id: "ws-main" });
    }

    if (path === routeKey("api/coding/approvals/approve") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onApprovalDecision?.("approve", payload);
      const requestId = String(payload.approval_request_id ?? "").trim();
      if (requestId) settledApprovalRequestIds.add(requestId);
      if (codingApprovalRequest?.request_id === payload.approval_request_id) {
        codingApprovalRequest = { ...codingApprovalRequest, status: "approved" };
      }
      return fulfill(route, {
        request_id: payload.approval_request_id,
        approved: true,
        token: "approved-mcp-token",
      });
    }

    if (path === routeKey("api/coding/approvals/deny") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onApprovalDecision?.("deny", payload);
      const requestId = String(payload.approval_request_id ?? "").trim();
      if (requestId) settledApprovalRequestIds.add(requestId);
      if (codingApprovalRequest?.request_id === requestId) {
        codingApprovalRequest = { ...codingApprovalRequest, status: "denied" };
      }
      return fulfill(route, { request_id: payload.approval_request_id, approved: false, status: "denied" });
    }

    if (path === routeKey("api/coding/terminal/exec") && method === "POST" && options.codingApprovalAfterTerminal) {
      const payload = request.postDataJSON() as Record<string, unknown>;
      codingApprovalRequest = {
        request_id: "apr-terminal-write",
        operation: "terminal.exec",
        risk_level: "high",
        status: "pending",
        display_summary: "terminal.exec: write qa-file.txt",
        created_at: now,
        args_hash: approvalDigest,
      };
      return fulfill(route, {
        command: String(payload.command ?? ""),
        classification: "high",
        risk_reasons: ["write"],
        approval_required: true,
        approval_request_id: "apr-terminal-write",
        exit_code: null,
        stdout: "",
        stderr: "",
      });
    }

    if (path === routeKey("api/coding/files/restore") && method === "POST" && options.codingApprovalAfterRestore) {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const snapshotId = String(payload.snapshot_id ?? "checkpoint-1");
      if (payload.approval_token) {
        return fulfill(route, { restored: true, snapshot_id: snapshotId });
      }
      codingApprovalRequest = {
        request_id: `apr-${snapshotId}-restore`,
        operation: "file.restore",
        risk_level: "high",
        status: "pending",
        display_summary: `file.restore: ${snapshotId}`,
        created_at: now,
        args_hash: approvalDigest,
      };
      return fulfill(route, {
        approval_required: true,
        approval_request: codingApprovalRequest,
      });
    }

    if (path === routeKey("api/coding/approvals")) {
      const requests = codingApprovalRequest ? [codingApprovalRequest] : [];
      return fulfill(route, { requests, pending: requests, count: requests.length });
    }

    if (path === routeKey("api/coding/checkpoints")) {
      if (method === "POST") {
        const checkpoint = {
          snapshot_id: "checkpoint-2",
          path: "/repo/.rumi/checkpoints/checkpoint-2",
        };
        codingCheckpoints.unshift(checkpoint);
        return fulfill(route, { checkpoint, workspace_id: "ws-main", workspace_root: "/repo" });
      }
      return fulfill(route, {
        checkpoints: codingCheckpoints,
        workspace_id: "ws-main",
        workspace_root: "/repo",
      });
    }

    if (path === routeKey("api/coding/rumi-log")) {
      return fulfill(route, {
        rumi_dir: "/repo/.rumi",
        events_path: "/repo/.rumi/events.jsonl",
        events: [],
        summary: {
          total: 0,
          by_kind: {},
          by_status: {},
          agent_ids: [],
          commit_count: 0,
          push_count: 0,
          plan_count: 0,
          task_count: 0,
          conversation_count: 0,
          mention_count: 0,
          last_event_at: null,
          last_commit_hash: null,
        },
        workspace_id: "ws-main",
        workspace_root: "/repo",
        created: false,
      });
    }

    if (path === routeKey("api/browser/artifacts")) {
      return fulfill(route, {
        artifacts: [{ artifact_id: "browser-1", session_id: "s1", action: "browser.session", created_at: "2026-05-20T00:00:00Z", url: "https://example.com" }],
        count: 1,
      });
    }

    if (path === routeKey("api/tools/mcp") && method === "POST") {
      const payload = request.postDataJSON() as { server?: Record<string, unknown> };
      const server = {
        server_id: String(payload.server?.server_id ?? "contract_digest"),
        name: String(payload.server?.name ?? payload.server?.server_id ?? "contract_digest"),
        transport: "stdio",
        connected: false,
        permissions: { approved: false },
        tools: [],
      };
      mcpServers.push(server);
      return fulfill(route, { server });
    }

    if (path === routeKey("api/tools/mcp/connect") && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const serverId = String(payload.server_id ?? payload.server_name ?? "contract_digest");
      if (!payload.approval_token) {
        codingApprovalRequest = {
          request_id: "apr-mcp-contract",
          operation: "tool.mcp_connect",
          risk_level: "high",
          status: "pending",
          display_summary: `Connect MCP server ${serverId}`,
          created_at: now,
          args_hash: approvalDigest,
          details: {
            mcp_review: {
              executable: String(payload.command ?? "python"),
              transport: "stdio",
              args: Array.isArray(payload.args) ? payload.args : [],
              cwd: "/repo",
              redacted_env: [],
              server_source: "Pack v4 UI contract fixture",
            },
          },
        };
        return fulfill(route, {
          approval_required: true,
          approval_request_id: "apr-mcp-contract",
          server_id: serverId,
        });
      }
      const server = mcpServers.find((item) => item.server_id === serverId);
      if (server) {
        server.connected = true;
        server.permissions = { approved: true };
        server.tools = [`mcp__${serverId}__digest`];
      }
      return fulfill(route, {
        server_id: serverId,
        server_name: serverId,
        status: "connected",
        tools: [`mcp__${serverId}__digest`],
        permission: { approved: true, source: "approval" },
      });
    }

    if (path === routeKey("api/tools/mcp")) {
      return fulfill(route, {
        servers: mcpServers,
        count: mcpServers.length,
      });
    }

    return fulfill(route, {});
  });
}

async function openDefaultspack(page: Page, path = "/chat", options: ApiMockOptions = {}) {
  await installDefaultspackApiMocks(page, { ...options, applicationChat: true });
  // Exercise ChatApp only through its captured Application binding and Profile URL.
  await page.goto(frontendHostFixtureScreenPath(path));
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();
}

async function openCodingWidget(page: Page, options: ApiMockOptions = {}) {
  await openDefaultspack(page, "/chat", options);
  await page.locator("textarea.rumi-composer-textarea").fill("/coding");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  const codingWidgetButton = page.getByRole("button", { name: "Coding widget", exact: true });
  await expect(codingWidgetButton).toBeVisible();
  await codingWidgetButton.click();
  await expect(page.locator(".coding-cockpit")).toBeVisible();
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
}

// These browser tests cover the approval-window renderer through a mocked
// Tauri bridge. WebviewWindowBuilder creation, focus, and always-on-top need
// dedicated desktop E2E coverage; they are not established by this fixture.
test("approval window renderer contract binds typed approval to the current request and closes after settlement", async ({ page }) => {
  const decisions: Array<{ decision: "approve" | "deny"; payload: Record<string, unknown> }> = [];
  const requestId = "apr-renderer-typed-contract";
  const confirmationPhrase = "APPROVE RELEASE";
  await installDefaultspackApiMocks(page, {
    interactiveApproval: {
      request_id: requestId,
      request_snapshot_digest: "1".repeat(64),
      state: "pending",
      expires_at: Math.floor(Date.now() / 1_000) + 300,
      typed_confirmation_required: true,
      typed_confirmation_digest: "2".repeat(64),
      redacted_metadata: {
        action: "Release the prepared update",
        confirmation_phrase: confirmationPhrase,
      },
    },
    onInteractiveApprovalDecision: (decision, payload) => decisions.push({ decision, payload }),
  });

  await page.goto(`/approval?request_id=${requestId}`);

  await expect(page.getByRole("heading", { name: "この操作を許可しますか？" })).toBeVisible();
  await expect(page.getByText("Release the prepared update")).toBeVisible();
  const confirmation = page.getByPlaceholder("確認文を入力");
  const approve = page.getByRole("button", { name: "承認", exact: true });
  await expect(confirmation).toBeVisible();
  await expect(approve).toBeDisabled();

  await confirmation.fill("APPROVE RELEASE later");
  await expect(approve).toBeDisabled();
  expect(decisions).toEqual([]);

  await confirmation.fill(confirmationPhrase);
  await expect(approve).toBeEnabled();
  await approve.click();

  const approvedStatus = page.getByText("承認済み", { exact: true });
  await expect(approvedStatus).toHaveCount(2);
  await expect(approvedStatus.nth(1)).toBeVisible();
  expect(decisions).toHaveLength(1);
  expect(decisions[0]).toMatchObject({
    decision: "approve",
    payload: {
      request_id: requestId,
      confirmation_text: confirmationPhrase,
      ui_operator: {
        version: 3,
        kind: "ui_operator",
        request_id: requestId,
        window_label: "authority-approval",
        decision: "approve",
        request_snapshot_digest: "1".repeat(64),
        typed_confirmation_digest: "2".repeat(64),
      },
    },
  });
  await expect.poll(() => page.evaluate(() => {
    const fixtureWindow = window as Window & {
      __approvalRendererFixture?: {
        tauriBridgeCalls: Array<{ command: string }>;
      };
    };
    return fixtureWindow.__approvalRendererFixture?.tauriBridgeCalls
      .filter((call) => call.command === "close_current_window")
      .length ?? 0;
  })).toBe(1);
});

test("approval window renderer contract denies once and renders its settled state", async ({ page }) => {
  const decisions: Array<{ decision: "approve" | "deny"; payload: Record<string, unknown> }> = [];
  const requestId = "apr-renderer-deny-contract";
  await installDefaultspackApiMocks(page, {
    interactiveApproval: {
      request_id: requestId,
      request_snapshot_digest: "3".repeat(64),
      state: "pending",
      expires_at: Math.floor(Date.now() / 1_000) + 300,
      typed_confirmation_required: false,
      typed_confirmation_digest: null,
      redacted_metadata: { action: "Discard the prepared update" },
    },
    onInteractiveApprovalDecision: (decision, payload) => decisions.push({ decision, payload }),
  });

  await page.goto(`/approval?request_id=${requestId}`);
  await page.getByRole("button", { name: "拒否", exact: true }).click();

  const deniedStatus = page.getByText("拒否済み", { exact: true });
  await expect(deniedStatus).toHaveCount(2);
  await expect(deniedStatus.nth(1)).toBeVisible();
  expect(decisions).toHaveLength(1);
  expect(decisions[0]).toMatchObject({
    decision: "deny",
    payload: {
      request_id: requestId,
      ui_operator: {
        version: 3,
        kind: "ui_operator",
        request_id: requestId,
        decision: "deny",
        request_snapshot_digest: "3".repeat(64),
        typed_confirmation_digest: null,
      },
    },
  });
  await expect.poll(() => page.evaluate(() => {
    const fixtureWindow = window as Window & {
      __approvalRendererFixture?: {
        tauriBridgeCalls: Array<{ command: string }>;
      };
    };
    return fixtureWindow.__approvalRendererFixture?.tauriBridgeCalls
      .filter((call) => call.command === "close_current_window")
      .length ?? 0;
  })).toBe(1);
});

test("approval window renderer contract fails closed when typed confirmation metadata is absent", async ({ page }) => {
  const decisions: Array<{ decision: "approve" | "deny"; payload: Record<string, unknown> }> = [];
  await installDefaultspackApiMocks(page, {
    interactiveApproval: {
      request_id: "apr-renderer-missing-confirmation",
      request_snapshot_digest: "4".repeat(64),
      state: "pending",
      expires_at: Math.floor(Date.now() / 1_000) + 300,
      typed_confirmation_required: true,
      typed_confirmation_digest: "5".repeat(64),
      redacted_metadata: { action: "Apply the protected change" },
    },
    onInteractiveApprovalDecision: (decision, payload) => decisions.push({ decision, payload }),
  });

  await page.goto("/approval?request_id=apr-renderer-missing-confirmation");

  await expect(page.getByText("この承認に必要な確認情報を取得できませんでした。安全のため操作できません。")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("この承認に必要な確認情報を取得できませんでした。安全のため操作できません。");
  await expect(page.getByRole("button", { name: "エラーをコピー" })).toBeVisible();
  await expect(page.getByPlaceholder("確認文を入力")).toBeDisabled();
  await expect(page.getByRole("button", { name: "承認", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "拒否", exact: true })).toHaveCount(0);
  expect(decisions).toEqual([]);
});

test("approval window disables actions when the displayed request expires", async ({ page }) => {
  const decisions: string[] = [];
  const requestId = "apr-renderer-near-expiry";
  await installDefaultspackApiMocks(page, {
    interactiveApproval: {
      request_id: requestId,
      request_snapshot_digest: "6".repeat(64),
      state: "pending",
      expires_at: Math.ceil(Date.now() / 1_000) + 2,
      typed_confirmation_required: false,
      typed_confirmation_digest: null,
      redacted_metadata: { action: "Time-sensitive operation" },
    },
    onInteractiveApprovalDecision: (decision) => decisions.push(decision),
  });

  await page.goto(`/approval?request_id=${requestId}`);
  await expect(page.getByRole("button", { name: "承認", exact: true })).toBeEnabled();
  await expect(page.getByText("期限切れ", { exact: true })).toHaveCount(2, { timeout: 5_000 });
  await expect(page.getByRole("button", { name: "承認", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "拒否", exact: true })).toHaveCount(0);
  expect(decisions).toEqual([]);
});

test("approval window reports an authoritative stale request without an ID mismatch", async ({ page }) => {
  const decisions: string[] = [];
  const requestId = "apr-renderer-stale";
  let isStale = false;
  await installDefaultspackApiMocks(page, {
    interactiveApproval: {
      request_id: requestId,
      request_snapshot_digest: "7".repeat(64),
      state: "pending",
      expires_at: Math.ceil(Date.now() / 1_000) + 300,
      typed_confirmation_required: false,
      typed_confirmation_digest: null,
      redacted_metadata: { action: "Changed operation" },
    },
    onInteractiveApprovalRead: () => isStale ? { state: "stale" } : {},
    onInteractiveApprovalDecision: (decision) => decisions.push(decision),
  });

  await page.goto(`/approval?request_id=${requestId}`);
  await expect(page.getByRole("button", { name: "承認", exact: true })).toBeEnabled();
  isStale = true;
  await page.getByRole("button", { name: "承認", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("このリクエストは古くなりました");
  await expect(page.getByRole("button", { name: "承認", exact: true })).toHaveCount(0);
  await expect(page.getByRole("alert")).not.toContainText("一致しません");
  expect(decisions).toEqual([]);
});

test("approval window reports expiry discovered by its decision preflight", async ({ page }) => {
  const decisions: string[] = [];
  const requestId = "apr-renderer-expired-during-decision";
  let expired = false;
  await installDefaultspackApiMocks(page, {
    interactiveApproval: {
      request_id: requestId,
      request_snapshot_digest: "8".repeat(64),
      state: "pending",
      expires_at: Math.ceil(Date.now() / 1_000) + 300,
      typed_confirmation_required: false,
      typed_confirmation_digest: null,
      redacted_metadata: { action: "Expiring operation" },
    },
    onInteractiveApprovalRead: () => expired ? { state: "expired" } : {},
    onInteractiveApprovalDecision: (decision) => decisions.push(decision),
  });

  await page.goto(`/approval?request_id=${requestId}`);
  await expect(page.getByRole("button", { name: "承認", exact: true })).toBeEnabled();
  expired = true;
  await page.getByRole("button", { name: "承認", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("このリクエストは期限切れです");
  await expect(page.getByRole("alert")).not.toContainText("一致しません");
  await expect(page.getByRole("button", { name: "承認", exact: true })).toHaveCount(0);
  expect(decisions).toEqual([]);
});

test("manual runtime mode control is hidden by default and available after explicit opt-in", async ({ page }) => {
  const writes: Record<string, unknown>[] = [];
  const settingsIdentity = {
    GET: { contributionId: "defaults.ui.settings.read", contractId: "tobkiri.resource.ui.settings.v1",
      operationId: "tobkiri_ui_settings_pack.settings-read", method: "GET", path: routeKey("api/ui/settings") },
    PUT: { contributionId: "defaults.ui.preferences.write", contractId: "tobkiri.action.ui.preferences.v1",
      operationId: "tobkiri_ui_settings_pack.preferences-write", method: "PUT", path: routeKey("api/ui/settings") },
  };
  const isSettingsRequest = (request: Request, method: "GET" | "PUT") => {
    const query = canonicalRequestQuery(request, "api/ui/settings", method);
    return query !== null && (method !== "PUT" || query.size === 0)
      && matchesFrontendFixtureBinding(frontendFixtureRequest(request.url(), request.method()), settingsIdentity[method]);
  };
  await openDefaultspack(page, "/chat", { onSettingsWrite: (payload) => writes.push(payload) });
  await expect(page.getByRole("status", { name: "現在の実行オプション" })).toHaveCount(0);

  const openAdvancedSettings = async () => {
    await page.getByTitle("Settings").last().click();
    await page.getByRole("button", { name: "Change settings display mode" }).click();
    await page.getByRole("button", { name: "Advanced Settings", exact: true }).click();
    await page.locator("main#settings-content details summary").click();
  };
  await openAdvancedSettings();
  const optIn = page.getByRole("button", { name: "Manual Runtime Mode Selection", exact: true });
  await expect(optIn).toHaveAttribute("aria-pressed", "false");
  const saved = page.waitForResponse((response) => isSettingsRequest(response.request(), "PUT"));
  await optIn.click();
  const receipt = await saved;
  expect(receipt.status()).toBe(200);
  expect(frontendFixtureRequest(receipt.url(), receipt.request().method())).toEqual(settingsIdentity.PUT);
  expect(receipt.request().postDataJSON()).toEqual({
    changes: { general: { manual_runtime_mode_selection: true } }, expected_revision: 1,
  });
  expect(await receipt.json()).toEqual(ok({
    values: { general: { manual_runtime_mode_selection: true } }, document_revision: 2,
  }));
  await expect(optIn).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(page.getByRole("dialog", { name: "Settings", exact: true })).toBeHidden();
  await expect(page.getByRole("status", { name: "現在の実行オプション" })).toBeVisible();

  // A fresh document must obtain the persisted value, rather than an optimistic toggle.
  let newDocumentCommitted = false;
  const freshSettingsRequests = new Set<Request>();
  const onNavigated = (frame: Frame) => { if (frame === page.mainFrame()) newDocumentCommitted = true; };
  const onRequest = (request: Request) => {
    if (newDocumentCommitted && request.frame() === page.mainFrame() && isSettingsRequest(request, "GET")) {
      freshSettingsRequests.add(request);
    }
  };
  page.on("framenavigated", onNavigated);
  page.on("request", onRequest);
  try {
    const restored = page.waitForResponse((response) => freshSettingsRequests.has(response.request())
      && isSettingsRequest(response.request(), "GET"));
    await page.goto(frontendHostFixtureScreenPath("/chat"));
    const persisted = await restored;
    expect(persisted.status()).toBe(200);
    expect(frontendFixtureRequest(persisted.url(), persisted.request().method())).toEqual(settingsIdentity.GET);
    expect(await persisted.json()).toMatchObject({ data: {
      values: { general: { manual_runtime_mode_selection: true } }, document_revision: 2,
    } });
  } finally {
    page.off("framenavigated", onNavigated);
    page.off("request", onRequest);
  }
  await expect(page.getByRole("status", { name: "現在の実行オプション" })).toBeVisible();
  await openAdvancedSettings();
  await expect(optIn).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(page.getByRole("dialog", { name: "Settings", exact: true })).toBeHidden();
  expect(writes).toHaveLength(1);
});

test("manual runtime mode control opens the mode selector when enabled", async ({ page }) => {
  await openDefaultspack(page, "/chat", {
    initialSettingsValues: {
      general: { manual_runtime_mode_selection: true },
    },
  });

  const runtimeOptions = page.getByRole("status", { name: "現在の実行オプション" });
  await expect(runtimeOptions).toBeVisible();
  await runtimeOptions.getByRole("button", { name: "実行モード: 自律エージェント" }).click();
  await expect(page.getByText("モード選択")).toBeVisible();
  await expect(page.getByRole("button", { name: "Coding コード編集・Git操作", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Chat 通常チャット", exact: true }).click();
  await expect(runtimeOptions.getByRole("button", { name: "実行モード: 通常チャット" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-app-mode"))).toBe('"chat"');
});

test("projects replace New Group and are searchable from the composer", async ({ page }) => {
  const conversationCreates: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onConversationCreate: (payload) => conversationCreates.push(payload),
  });

  await expect(page.getByText("Projects", { exact: true })).toBeVisible();
  await expect(page.getByText("New Group", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  const projectButton = page.getByRole("button", { name: "Project: None" });
  await expect(projectButton).toHaveCSS("min-height", "44px");
  await projectButton.click();
  await expect(page.getByRole("textbox", { name: "Search projects" })).toBeVisible();
  await page.getByRole("button", { name: "New Project" }).last().click();
  await page.getByPlaceholder("Project name").fill("E2E Project");
  await page.getByRole("button", { name: "Create Project", exact: true }).click();

  await expect(page.getByRole("button", { name: "Project: E2E Project" })).toBeVisible();
  const persistedProject = await page.evaluate(() => {
    const projects = JSON.parse(localStorage.getItem("rumi-history-custom-groups") || "[]") as Array<Record<string, unknown>>;
    return projects.find((project) => project.title === "E2E Project") ?? null;
  });
  expect(persistedProject).toMatchObject({ title: "E2E Project" });
  expect(String(persistedProject?.id ?? "")).toMatch(/^group-\d+$/);

  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("Project scoped message");
  await page.locator(".rumi-send-button").click();
  await expect.poll(() => conversationCreates.length).toBe(1);
  expect(conversationCreates[0].group_id).toBe(persistedProject?.id);
  expect((conversationCreates[0].metadata as Record<string, unknown>).group_id).toBe(persistedProject?.id);
});

test("document scroll fallback survives small and keyboard-like viewports", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 520 });
  await openDefaultspack(page, "/chat");

  await expect(page.locator(".rumi-app-shell")).toBeVisible();
  await expect(page.locator(".rumi-workspace-main")).toHaveCSS("min-height", "0px");

  for (const viewport of [
    { width: 390, height: 520 },
    { width: 320, height: 620 },
    { width: 390, height: 340 },
  ]) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => {
      document.querySelector("[data-qa-scroll-fallback]")?.remove();
      const fallbackProbe = document.createElement("div");
      fallbackProbe.dataset.qaScrollFallback = "true";
      fallbackProbe.style.height = "80vh";
      fallbackProbe.style.pointerEvents = "none";
      document.body.appendChild(fallbackProbe);
      window.scrollTo(0, 0);
    });

    await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).overflowY)).not.toBe("hidden");
    await page.mouse.wheel(0, viewport.height);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  }
});

test("tool hub search suggestions close on outside click while keeping filtered actions usable", async ({ page }) => {
  await openDefaultspack(page);

  await page.locator('button[title="機能"]').click();
  const search = page.getByPlaceholder("機能を検索");
  await search.fill("web");
  await expect(page.getByTestId("tool-manager-candidates")).toBeVisible();
  await expect(page.getByTestId("tool-manager-candidates")).toContainText("Web Search");

  await page.getByRole("heading", { name: "機能" }).click();
  await expect(page.getByTestId("tool-manager-candidates")).toBeHidden();
  await expect(search).toHaveValue("web");

  await page.getByRole("button", { name: "表示中を今回使う" }).click();
  await expect(page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" })).toHaveValue("@web_search ");
});

test("composer approval menu opens action permissions independently of tool selection modes", async ({ page }) => {
  const settingsWrites: Record<string, unknown>[] = [];
  const mutationRequests: string[] = [];
  await openDefaultspack(page, "/chat", { onSettingsWrite: (payload) => settingsWrites.push(payload) });
  page.on("request", (request) => {
    const binding = frontendFixtureRequest(request.url(), request.method());
    const path = requestTarget(new URL(request.url()));
    // Only an exact canonical POST bound to a shipped resource contract is a read.
    // Unknown POSTs and every non-read service/action request remain effects.
    const resourceRead = request.method() === "POST" && binding?.method === "POST"
      && binding.contractId.startsWith("tobkiri.resource.")
      && canonicalRequestQuery(request, binding.path.slice(1), "POST") !== null;
    if ((!["GET", "HEAD", "OPTIONS"].includes(request.method()) && !resourceRead)
      || binding?.contractId.startsWith("tobkiri.action.")
      || binding?.contractId === "tobkiri.service.interactive-effect.v1"
      || path === routeKey("api/command-protocol/v1/invoke")) {
      mutationRequests.push(`${request.method()} ${path}`);
    }
  });
  const approval = page.getByRole("button", { name: "アクションの承認方法" });
  await approval.click();
  const approvalMenu = page.getByRole("menu", { name: "アクションの承認方法" });
  await expect(approvalMenu).toHaveAccessibleName("アクションの承認方法");
  await expect(approvalMenu.getByRole("menuitemradio", { name: /^人が承認/ })).toBeEnabled();
  await expect(approvalMenu.getByRole("menuitemradio", { name: /^人が承認/ })).toHaveAttribute("aria-checked", "true");
  await expect(approvalMenu.getByRole("menuitemradio", { name: /^別のAIが承認/ })).toBeDisabled();
  await expect(approvalMenu.getByRole("menuitemradio", { name: /^追加承認なし/ })).toBeDisabled();
  await expect(approvalMenu).not.toContainText("カスタム（設定）");
  await expect(approvalMenu).not.toContainText("自動で選ぶ");

  await approvalMenu.getByRole("button", { name: "詳細はこちら" }).click();
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await expect(dialog).toBeVisible();
  const categories = dialog.getByRole("navigation", { name: "Settings categories" });
  await categories.getByRole("button", { name: "Tools", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Tools", exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /^Tobkiriが自動で選ぶ/ })).toBeEnabled();
  await expect(dialog.getByRole("checkbox", { name: "入力欄に承認モードを表示" })).toBeChecked();
  const fixedApproval = dialog.getByRole("combobox", { name: "非表示時の固定モード（必須）", exact: true });
  await expect(fixedApproval).toHaveValue("ask");
  // Playwright 1.60's disabled matcher follows the wrapping label to its enabled
  // select. Check each native option itself, then exercise normal keyboard input.
  for (const name of [/^別のAIが承認/, /^追加承認なし/]) {
    const unavailable = fixedApproval.getByRole("option", { name });
    await expect(unavailable).toHaveAttribute("disabled", "");
    await expect(unavailable).toHaveJSProperty("disabled", true);
  }
  await expect(fixedApproval).toBeEnabled();
  await fixedApproval.focus();
  await expect(fixedApproval).toBeFocused();
  for (const key of ["ArrowDown", "End", "ArrowUp", "Home"]) {
    await fixedApproval.press(key);
    await expect(fixedApproval).toHaveValue("ask");
  }
  await fixedApproval.press("Tab");
  await expect(fixedApproval).toHaveValue("ask");
  expect(settingsWrites).toEqual([]);
  expect(mutationRequests).toEqual([]);
  await dialog.getByRole("button", { name: "権限", exact: true }).click();
  await expect(dialog.getByText("機能の選定と、実行時の許可は別です。ここで「確認する」にした操作は、実行前にランタイムの承認UIへ渡されます。", { exact: true })).toBeVisible();
  const sendPermissions = dialog.locator("div.rounded-lg").filter({ has: page.getByRole("heading", { name: "送信する", exact: true }) });
  await expect(sendPermissions.getByRole("button", { name: "確認する", exact: true })).toBeEnabled();
  await expect(sendPermissions.getByRole("button", { name: "使わない", exact: true })).toBeEnabled();

  // Technical tool-source/account separation is in the Advanced Connections tab.
  await dialog.getByRole("button", { name: "Change settings display mode" }).click();
  await categories.getByRole("button", { name: "Tools", exact: true }).click();
  await dialog.getByRole("button", { name: "接続", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "ツールとログインは別に管理されます", exact: true })).toBeVisible();
  await expect(dialog.getByText("MCP servers and tool sources define callable actions. Account login, OAuth tokens, and access tokens remain in Accounts & Connections.", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Safety rules", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Tool source → Tools & MCP", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Off Disable Codex App Server integration.", exact: true })).toBeVisible();
  await categories.getByRole("button", { name: "Connections", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Connections", exact: true })).toBeVisible();
  await expect(dialog.getByLabel("Connection summary", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Official secrets are not bundled. The official app can provide hosted sign-in, while self-hosted installations can import credentials or configure their own OAuth client.", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("checkbox", { name: "入力欄に承認モードを表示" })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Off Disable Codex App Server integration.", exact: true })).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close settings" }).click();
  await expect(dialog).toBeHidden();
  await expect(approval).toContainText("承認");
  expect(settingsWrites).toEqual([]);
  expect(mutationRequests).toEqual([]);
});

test("composer tool mode menu offers supported modes and opens manual tool settings", async ({ page }) => {
  await openDefaultspack(page, "/chat", {
    initialSettingsValues: { tools: { show_tool_selection_control: true } },
  });

  const mode = page.getByRole("button", { name: "機能の使い方", exact: true });
  await expect(mode).toBeVisible();
  await expect(mode).toContainText("機能 自動");
  await mode.click();
  const menu = page.getByRole("menu", { name: "機能の使い方" });
  await expect(menu.getByRole("menuitemradio")).toHaveCount(3);
  await expect(menu.getByRole("menuitemradio", { name: /自動で選ぶ/ })).toHaveAttribute("aria-checked", "true");
  await expect(menu.getByRole("menuitemradio", { name: /自分で選ぶ/ })).toBeVisible();
  await expect(menu.getByRole("menuitemradio", { name: /機能を使わない/ })).toBeVisible();
  await expect(menu.getByRole("menuitemradio", { name: /使う前に確認/ })).toHaveCount(0);

  await menu.getByRole("menuitemradio", { name: /自分で選ぶ/ }).click();
  await expect(menu).toBeHidden();
  await expect(page.getByRole("dialog", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Tools", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(mode).toContainText("機能 手動");

  await mode.click();
  await menu.getByRole("menuitemradio", { name: /自動で選ぶ/ }).click();
  await expect(mode).toContainText("機能 自動");
  await expect(page.getByRole("button", { name: "アクションの承認方法" })).toContainText("承認");
});

test("composer per-turn no-tools mode reaches the request and locks during generation without settings writes", async ({ page }) => {
  const savedTurnRequests: Array<{ request: SavedTurnRequest }> = [];
  const settingsWrites: Array<Record<string, unknown>> = [];
  const operationMismatches: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && message.text().includes("Saved turn events do not match the pending operation")) {
      operationMismatches.push(message.text());
    }
  });
  let releaseSavedTurn: (() => void) | undefined;
  const savedTurnGate = new Promise<void>((resolve) => { releaseSavedTurn = resolve; });
  await installDefaultspackApiMocks(page, {
    modelProfiles: [smokeProfile],
    applicationChat: true,
    initialSettingsValues: { tools: { show_tool_selection_control: true } },
    conversationMutator: (conversation) => {
      conversation.conversation_kind = "chat";
      conversation.tags = [];
    },
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
    onSettingsWrite: (payload) => settingsWrites.push(payload),
    beforeSavedTurnResponse: () => savedTurnGate,
  });
  // Select the production ChatApp through its Host-admitted builtin binding.
  // The separate declarative conversation_v4 view has different controls.
  await page.goto("/p/defaults/chat?chat=c-smoke");
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();

  const mode = page.getByRole("button", { name: "機能の使い方", exact: true });
  await mode.click();
  await page.getByRole("menuitemradio", { name: /機能を使わない/ }).click();
  await expect(mode).toContainText("機能 なし");
  await page.locator("textarea.rumi-composer-textarea").fill("Answer using no external tools.");
  await expect(mode).toContainText("機能 なし");
  await page.getByRole("button", { name: "メッセージを送信" }).click();

  let savedResponses: Promise<[Response, Response]>;
  try {
    await expect.poll(() => savedTurnRequests.length).toBe(1);
    expect(savedTurnRequests[0].request).toMatchObject({
      conversation_id: "c-smoke",
      conversation_revision: 1,
      content: "Answer using no external tools.",
    });
    expect(savedTurnRequests[0].request.tool_selection).toMatchObject({
      mode: "none",
      include: [],
      must_use: false,
    });
    await expect(mode).toBeDisabled();
    expect(settingsWrites).toEqual([]);
    // Register both reads under the closed gate only after busy-state assertions
    // succeed, so a failed assertion cannot leave dangling response promises.
    savedResponses = Promise.all([
      page.waitForResponse((response) => chatRequestKind(response.request()) === "startTurn"),
      page.waitForResponse((response) => {
        const query = canonicalRequestQuery(response.request(), "api/chat/turn/events", "GET");
        return query !== null && query.get("conversation_id") === "c-smoke"
          && query.get("turn_id") === savedTurnRequests[0].request.turn_id;
      }),
    ]);
  } finally {
    releaseSavedTurn?.();
  }

  const [startResponse, eventReceipt] = await savedResponses;
  expect(startResponse.status()).toBe(200);
  const startReceipt = await startResponse.json();
  expect(startReceipt.data.status).toBe("completed");
  expect(eventReceipt.status()).toBe(200);
  const eventsBinding = frontendFixtureBinding("savedTurnEvents");
  expect(eventsBinding).toMatchObject({
    contributionId: "defaults.conversations.turn.events", contractId: "tobkiri.event.turn.v1",
    operationId: "rumi_turn_runtime_pack.turn-events", method: "GET",
  });
  expect(frontendFixtureRequest(eventReceipt.request().url(), eventReceipt.request().method())).toEqual(eventsBinding);
  expect([...canonicalRequestQuery(eventReceipt.request(), "api/chat/turn/events", "GET")!].sort()).toEqual(
    Object.entries({ turn_id: savedTurnRequests[0].request.turn_id, conversation_id: "c-smoke" }).sort(),
  );
  const eventSnapshot = (await eventReceipt.json()).data;
  expect(eventSnapshot.turn).toEqual(startReceipt.data.turn);
  const identity = {
    turn_id: savedTurnRequests[0].request.turn_id, conversation_id: "c-smoke",
    operation_id: savedTurnRequests[0].request.turn_id, request_id: startReceipt.data.turn.request_id,
    turn_revision: startReceipt.data.turn.revision,
  };
  expect(eventSnapshot).toMatchObject({ ...identity, status: "completed", events: [] });
  expect(eventSnapshot.terminal).toEqual({
    ...identity, status: "completed", result_reference: startReceipt.data.turn.result_reference, error: null,
  });
  await expect(mode).toBeEnabled();
  await expect(mode).toContainText("機能 自動");
  await expect(page.getByText("Saved response accepted.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "アクションの承認方法" })).toContainText("承認");
  await expect(page.locator("textarea.rumi-composer-textarea")).toHaveValue("");
  await expect(page.locator("textarea.rumi-composer-textarea")).toBeEditable();
  expect(savedTurnRequests).toHaveLength(1);
  expect(settingsWrites).toEqual([]);
  expect(operationMismatches).toEqual([]);
});

test("new chat resets draft tool mode and confirmed tool authority without duplicating migrated text", async ({ page }) => {
  const settingsWrites: Array<Record<string, unknown>> = [];
  await installDefaultspackApiMocks(page, {
    applicationChat: true,
    initialSelectedToolIds: ["web_search"],
    initialSettingsValues: { tools: { show_tool_selection_control: true } },
    onSettingsWrite: (payload) => settingsWrites.push(payload),
  });
  await page.goto(frontendHostFixtureScreenPath("/chat"));
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();

  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  const mentions = page.locator('[data-composer-inline-mentions] .rumi-composer-inline-mention');
  await expect(composer).toHaveValue("@web_search ");
  await expect(mentions).toHaveCount(1);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe('["web_search"]');
  const mode = page.getByRole("button", { name: "機能の使い方", exact: true });
  await mode.click();
  await page.getByRole("menuitemradio", { name: /機能を使わない/ }).click();
  await expect(mode).toContainText("機能 なし");
  await page.getByRole("button", { name: "New Chat", exact: true }).click();

  await expect(mode).toContainText("機能 自動");
  // Text remains a draft; only confirmed widgets carry tool-selection authority.
  await expect(composer).toHaveValue("@web_search ");
  await expect(mentions).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  expect(settingsWrites).toEqual([]);

  // A second new draft must not replay the consumed legacy selected-tool snapshot.
  await page.getByRole("button", { name: "New Chat", exact: true }).click();
  await expect(mode).toContainText("機能 自動");
  await expect(composer).toHaveValue("@web_search ");
  await expect(mentions).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  expect(settingsWrites).toEqual([]);
});

test("tool selection controller resets transient draft state while preserving persistent preferences", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  await page.goto(frontendHostFixtureScreenPath("/chat"));
  const snapshots = await page.evaluate(async () => {
    const fixturePath = "/e2e/tool-selection-controller.fixture.tsx";
    const fixture = await import(/* @vite-ignore */ fixturePath);
    return fixture.exerciseToolSelectionControllerReset();
  });

  expect(snapshots.beforeReset).toMatchObject({
    effectiveMode: "review",
    turnModeOverride: "review",
    turnExclude: [{ kind: "service", id: "github" }],
    pendingReview: { previewId: "controller-reset-preview" },
    latestDecision: { selected_tools: ["web_search"] },
  });
  expect(snapshots.afterReset).toMatchObject({
    effectiveMode: "manual",
    turnModeOverride: null,
    turnExclude: [],
    pendingReview: null,
    latestDecision: null,
    selectedToolIds: ["web_search"],
    request: {
      mode: "manual",
      include: [{ kind: "service", id: "github" }, { kind: "tool", id: "web_search" }],
      exclude: [{ kind: "tool", id: "computer_control" }],
      must_use: true,
    },
  });
  expect(snapshots.afterNewConversation).toMatchObject({
    effectiveMode: "auto",
    turnModeOverride: null,
    turnExclude: [],
    pendingReview: null,
    latestDecision: null,
    selectedToolIds: ["web_search"],
    request: { mode: "manual", include: [{ kind: "tool", id: "web_search" }], exclude: [], must_use: true },
  });
  expect(snapshots.settingsValues).toEqual({ tools: { default_mode: "auto" } });
});

test("slash yolo cannot enable unavailable approval and retains its command draft", async ({ page }) => {
  const settingsWrites: Record<string, unknown>[] = [];
  const savedTurnRequests: Array<{ request: SavedTurnRequest }> = [];
  await openDefaultspack(page, "/chat", {
    onSettingsWrite: (payload) => settingsWrites.push(payload),
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  const approval = page.getByRole("button", { name: "アクションの承認方法" });
  await composer.fill("/yolo");
  await composer.press("Enter");
  await expect(page.getByRole("alert").getByText("選択した承認方式は現在利用できません。承認方式は変更していません。 - Tobkiri", { exact: true })).toBeVisible();
  await expect(approval).toContainText("承認");
  await expect(composer).toHaveValue("/yolo");
  expect(settingsWrites).toEqual([]);
  expect(savedTurnRequests).toEqual([]);
  expect(await page.evaluate(() => localStorage.getItem("rumi-ultra-yolo-mode"))).not.toBe("true");
});

test("slash yolo can return to human approval after elevated support is unavailable", async ({ page }) => {
  const savedTurnRequests: Array<{ request: SavedTurnRequest }> = [];
  await openDefaultspack(page, "/chat", {
    initialSettingsValues: { tools: { action_approval_mode: "full" } },
    conversationMutator: (conversation) => { conversation.conversation_kind = "chat"; conversation.tags = []; },
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  const approval = page.getByRole("button", { name: "アクションの承認方法" });
  // An unknown command remains a normal message and must not bypass turn approval.
  await composer.fill("/not-a-registered-command");
  await composer.press("Enter");
  await expect(page.getByRole("alert").getByText("選択した承認方式は現在利用できません。設定で「人が承認」を選んでください。下書きは保持しています。 - Tobkiri", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("/not-a-registered-command");
  expect(savedTurnRequests).toEqual([]);
  await composer.fill("/yolo off");
  await composer.press("Enter");
  await expect(approval).toContainText("承認");
  await expect(composer).toHaveValue("");
  await composer.fill("Keep this exact text");
  await composer.press("Enter");
  await expect.poll(() => savedTurnRequests.length).toBe(1);
  expect(savedTurnRequests[0].request).toMatchObject({
    action_approval_mode: "ask", content: "Keep this exact text",
  });
});


for (const change of ["none", "newer edit", "edit and restore", "new chat"] as const) {
  test(`approval preference delayed acknowledgment preserves draft ownership: ${change}`, async ({ page }) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const writes: Record<string, unknown>[] = [];
    const turns: Array<{ request: SavedTurnRequest }> = [];
    await openDefaultspack(page, "/chat", {
      initialSettingsValues: { tools: { action_approval_mode: "full" } },
      onSettingsWrite: (payload) => writes.push(payload),
      beforeSettingsWriteResponse: () => gate,
      onSavedTurnRequest: (payload) => turns.push(payload),
    });
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    const approval = page.getByRole("button", { name: "アクションの承認方法" });
    try {
      await composer.fill("/yolo off");
      await composer.press("Enter");
      await expect.poll(() => writes.length).toBe(1);
      expect(writes[0]).toEqual({ changes: { tools: { action_approval_mode: "ask" } }, expected_revision: 1 });
      await expect(approval).toContainText("フル");
      await expect(composer).toHaveValue("/yolo off");
      expect(turns).toEqual([]);
      let expectedDraft = "";
      if (change === "newer edit") {
        expectedDraft = "This newer draft must survive";
        await composer.fill(expectedDraft);
      } else if (change === "edit and restore") {
        await composer.fill("Temporary different draft");
        expectedDraft = "/yolo off";
        await composer.fill(expectedDraft);
      } else if (change === "new chat") {
        await page.getByRole("button", { name: "New Chat", exact: true }).click();
        await expect(page.getByRole("tab", { name: "New Conversation", exact: true })).toHaveAttribute("aria-selected", "true");
        expectedDraft = "New conversation draft must survive";
        await composer.fill(expectedDraft);
      }
      const response = page.waitForResponse((item) => item.request().method() === "PUT"
        && requestTarget(new URL(item.url())) === routeKey("api/ui/settings"));
      release();
      const acknowledged = await response;
      expect(acknowledged.status()).toBe(200);
      expect(await acknowledged.json()).toEqual(ok({ values: { tools: { action_approval_mode: "ask" } }, document_revision: 2 }));
      await expect(approval).toContainText("承認");
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await expect(composer).toHaveValue(expectedDraft);
      expect(writes).toHaveLength(1);
      expect(turns).toEqual([]);
    } finally { release(); }
  });
}

for (const outcome of ["conflict", "unconfirmed"] as const) {
  test(`approval preference ${outcome} response preserves draft and effective mode`, async ({ page }) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const writes: Record<string, unknown>[] = [];
    const turns: Array<{ request: SavedTurnRequest }> = [];
    await openDefaultspack(page, "/chat", {
      initialSettingsValues: { tools: { action_approval_mode: "full" } },
      beforeSettingsWriteResponse: () => gate,
      settingsWriteResponse: outcome,
      onSettingsWrite: (payload) => writes.push(payload),
      onSavedTurnRequest: (payload) => turns.push(payload),
    });
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    const approval = page.getByRole("button", { name: "アクションの承認方法" });
    try {
      await composer.fill("/yolo off");
      await composer.press("Enter");
      await expect.poll(() => writes.length).toBe(1);
      expect(writes[0]).toEqual({ changes: { tools: { action_approval_mode: "ask" } }, expected_revision: 1 });
      await expect(composer).toHaveValue("/yolo off");
      await expect(approval).toContainText("フル");
      release();
      await expect(page.getByRole("alert")).toContainText("承認方式を保存できませんでした");
      await expect(composer).toHaveValue("/yolo off");
      await expect(approval).toContainText("フル");
      expect(writes).toHaveLength(1);
      expect(turns).toEqual([]);
    } finally { release(); }
  });
}

test("approval preference old Profile completion cannot clear a new Profile document draft", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let oldWriteCommitted = false;
  const oldWrites: Record<string, unknown>[] = [];
  const newWrites: Record<string, unknown>[] = [];
  const turns: Array<{ request: SavedTurnRequest }> = [];
  await openDefaultspack(page, "/chat", {
    initialSettingsValues: { tools: { action_approval_mode: "full" } },
    onSettingsWrite: (payload) => oldWrites.push(payload),
    beforeSettingsWriteResponse: () => gate,
    onSettingsWriteCommitted: () => { oldWriteCommitted = true; },
    onSavedTurnRequest: (payload) => turns.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  try {
    await composer.fill("/yolo off");
    await composer.press("Enter");
    await expect.poll(() => oldWrites.length).toBe(1);
    expect(oldWrites[0]).toEqual({ changes: { tools: { action_approval_mode: "ask" } }, expected_revision: 1 });
    await expect(composer).toHaveValue("/yolo off");
    // Profile changes use full navigation in production. Each fixture installation
    // owns independent Settings state and a matching captured Application catalog.
    await installDefaultspackApiMocks(page, {
      applicationChat: true,
      runtimeProfileId: "approval-other",
      initialSettingsValues: { tools: { action_approval_mode: "ask" } },
      onSettingsWrite: (payload) => newWrites.push(payload),
      onSavedTurnRequest: (payload) => turns.push(payload),
    });
    await page.goto(frontendHostFixtureScreenPath("/chat", "approval-other"));
    await expect(page.getByRole("tab", { name: "Preview Calendar Chat", exact: true })).toHaveAttribute("aria-selected", "true");
    await composer.fill("Other Profile draft must survive");
    release();
    // The old document may have cancelled its fetch; this proves fixture-side
    // state commit and new-document isolation, not acknowledgment by the old document.
    await expect.poll(() => oldWriteCommitted).toBe(true);
    expect(oldWrites).toHaveLength(1);
    await expect(page).toHaveURL(/\/p\/approval-other\/chat(?:\?|$)/);
    await expect(composer).toHaveValue("Other Profile draft must survive");
    await expect(page.getByRole("button", { name: "アクションの承認方法" })).toContainText("承認");
    expect(newWrites).toEqual([]);
    expect(turns).toEqual([]);
  } finally { release(); }
});

test("new chat structured options open above the compact composer and apply values", async ({ page }) => {
  await openDefaultspack(page, "/chat", { structuredComposer: true });
  await page.getByRole("button", { name: "New Chat", exact: true }).click();

  const options = page.locator('[data-structured-composer="contract_composer"] > button[aria-haspopup="dialog"]');
  await expect(options).toHaveAttribute("aria-expanded", "false");
  await expect(options).toContainText("3/4");
  await options.click();

  const dialog = page.getByRole("dialog", { name: "入力オプション" });
  await expect(dialog).toBeVisible();
  await expect(options).toHaveAttribute("aria-expanded", "true");
  await dialog.getByLabel("補足").fill("比較対象を含める");
  await dialog.getByRole("button", { name: "入力に反映" }).click();
  await expect(dialog).toBeHidden();
  await expect(options).toContainText("4/4");

  const panelHeight = await page.locator(".rumi-composer-main-panel").evaluate((element) => element.getBoundingClientRect().height);
  expect(panelHeight).toBeLessThanOrEqual(133);
});

test("browser approval uses the shared user-first decision surface at narrow width", async ({ page }) => {
  let denialPayload: Record<string, unknown> | null = null;
  await page.setViewportSize({ width: 390, height: 844 });
  await openDefaultspack(page, "/chat", {
    conversationMutator: (conversation) => {
      conversation.messages[1].events.push({
        type: "approval_requested",
        phase: "approval_requested",
        approval_required: true,
        approval_request_id: "approval-browser-contract",
        tool_name: "browser_computer",
        action: "browser.open_url",
        risk_level: "medium",
        display_summary: "example.test を開き、ページ内容を外部サイトから読み込みます。",
        payload: { url: "https://example.test/long/path" },
        timestamp: now,
      });
    },
    onApprovalDecision: (decision, payload) => {
      if (decision === "deny") denialPayload = payload;
    },
  });

  const surface = page.locator('[data-approval-source="browser"]');
  await expect(surface).toBeVisible();
  await expect(surface).toContainText("Tobkiri が許可を求めています");
  await expect(surface).toContainText("https://example.test/long/path");
  await expect(surface).toContainText("必要な理由");
  await expect(surface).toContainText("許可範囲");
  await expect(surface.getByText("技術的な詳細")).toBeVisible();
  await expect(surface.locator("pre")).toBeHidden();
  await expect(surface.getByRole("button", { name: "拒否（2）" })).toBeVisible();
  await expect(surface.getByRole("button", { name: "許可（3）" })).toBeVisible();

  const composer = page.locator("textarea.rumi-composer-textarea");
  await composer.fill("2");
  await page.keyboard.press("3");
  await expect(composer).toHaveValue("23");
  await expect(surface).toBeVisible();

  await surface.getByRole("button", { name: "拒否（2）" }).click();
  await expect(surface).toBeHidden();
  expect(denialPayload).toMatchObject({ approval_request_id: "approval-browser-contract" });
});

test("settings modal contains focus, dismisses nested layers in order, and restores its opener", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  await page.goto(frontendHostFixtureScreenPath("/"));
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();

  const opener = page.getByTitle("Settings").last();
  await opener.focus();
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Settings" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeFocused();
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect(page.getByRole("button", { name: "Close settings" })).toBeVisible();

  const backgroundState = await page.getByTestId("settings-modal-layer").evaluate((layer) => (
    Array.from(layer.parentElement?.children ?? [])
      .filter((element) => element !== layer)
      .map((element) => ({ inert: (element as HTMLElement).inert, ariaHidden: element.getAttribute("aria-hidden") }))
  ));
  expect(backgroundState.length).toBeGreaterThan(0);
  expect(backgroundState.every((state) => state.inert && state.ariaHidden === "true")).toBe(true);

  const focusWrapResult = await dialog.evaluate((element) => {
    const selector = "button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])";
    const focusable = Array.from(element.querySelectorAll<HTMLElement>(selector)).filter((item) => item.offsetParent !== null);
    focusable.at(-1)?.focus();
    return focusable.length;
  });
  expect(focusWrapResult).toBeGreaterThan(1);
  await page.keyboard.press("Tab");
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null)).toBe(true);

  const placementTrigger = page.getByRole("button", { name: "Add an item to Settings" });
  await placementTrigger.click();
  await expect(page.getByRole("menu", { name: "Add an item to Settings" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu", { name: "Add an item to Settings" })).toBeHidden();
  await expect(dialog).toBeVisible();
  await expect(placementTrigger).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(dialog).toBeVisible();
  await page.getByTestId("settings-modal-layer").click({ position: { x: 2, y: 2 } });
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(dialog).toBeVisible();
  await page.setViewportSize({ width: 390, height: 640 });
  const narrowBounds = await dialog.boundingBox();
  expect(narrowBounds).not.toBeNull();
  expect(narrowBounds!.x).toBeGreaterThanOrEqual(0);
  expect(narrowBounds!.y).toBeGreaterThanOrEqual(0);
  expect(narrowBounds!.x + narrowBounds!.width).toBeLessThanOrEqual(390);
  expect(narrowBounds!.y + narrowBounds!.height).toBeLessThanOrEqual(640);
  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(dialog).toBeHidden();
});

test("tool hub service selections can be scoped to the conversation and survive reload", async ({ page }) => {
  await openDefaultspack(page);

  await page.locator('button[title="機能"]').click();
  await page.getByRole("button", { name: "会話の既定", exact: true }).click();
  const githubCard = page.locator("div.rounded-md")
    .filter({ has: page.getByText("GitHub", { exact: true }) })
    .filter({ has: page.getByRole("button", { name: /^(サービスを使う|サービス指定を解除)$/ }) });
  await expect(githubCard).toBeVisible();
  await githubCard.getByTitle("サービスを使う").click();
  await expect(githubCard).toContainText("会話固定");

  // Revisit the same captured Profile through its supported Application route.
  await page.goto(frontendHostFixtureScreenPath("/chat"));
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();
  await page.locator('button[title="機能"]').click();
  await page.getByRole("button", { name: "会話の既定", exact: true }).click();
  const reloadedGithubCard = page.locator("div.rounded-md")
    .filter({ has: page.getByText("GitHub", { exact: true }) })
    .filter({ has: page.getByRole("button", { name: /^(サービスを使う|サービス指定を解除)$/ }) });
  await expect(reloadedGithubCard).toContainText("会話固定");
});

test("composer at mention selects tools skills and services with semantic metadata", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });

  const composer = page.locator("textarea.rumi-composer-textarea");
  await composer.fill("Use @web");
  const mentions = page.getByTestId("composer-at-mention-candidates");
  await expect(mentions).toBeVisible();
  await expect(mentions).toContainText("@Web Search");
  await expect(mentions).not.toContainText("web_search");

  await composer.press("Enter");
  await expect(composer).toHaveValue("Use @Web Search ");
  await expect(page.locator(".rumi-composer-frame")).toContainText("Web Search");

  await composer.pressSequentially("@live");
  await expect(mentions).toBeVisible();
  await expect(mentions).toContainText("@Live Review");
  await expect(mentions).not.toContainText("feedback/live-review");
  await page.getByRole("option", { name: /@live review/i }).click();
  await expect(composer).toHaveValue("Use @Web Search @Live Review ");
  await expect(page.locator(".rumi-composer-frame")).toContainText("Live Review");

  await composer.press("End");
  await composer.pressSequentially("@gith");
  await expect(mentions).toBeVisible();
  const githubService = page.getByRole("option", { name: "@GitHub（まとめ） 接続されたサービス · 1件のツールをまとめて選択 接続", exact: true });
  await expect(githubService).toBeVisible();
  await githubService.click();
  await expect(composer).toHaveValue("Use @Web Search @Live Review @GitHub ");

  await page.locator(".rumi-send-button").click();
  await expect.poll(() => streamRequests.length).toBe(1);

  const request = streamRequests[0];
  expect(request.tools).toEqual(["web_search", "github_issue_search"]);
  const params = request.params as Record<string, unknown>;
  const toolSelection = params.tool_selection as Record<string, unknown>;
  expect(toolSelection.mode).toBe("manual");
  expect(toolSelection.scope).toBe("turn");
  expect(toolSelection.include).toEqual([
    { kind: "tool", id: "web_search" },
    { kind: "tool", id: "github_issue_search" },
  ]);

  const message = request.message as Record<string, unknown>;
  expect(message.content).toBe("Use @Web Search @Live Review @GitHub");
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.selected_tools).toEqual(["web_search", "github_issue_search"]);
  expect(metadata.skills).toEqual(["feedback/live-review"]);
  expect(metadata.skill_mentions).toEqual([{ id: "feedback/live-review", label: "Live Review" }]);
  expect(metadata.mentions).toEqual([
    { id: "web_search", kind: "tool", label: "Web Search", syntax: "@Web Search" },
    { id: "feedback/live-review", kind: "skill", label: "Live Review", syntax: "@Live Review" },
    { id: "github", kind: "service", label: "GitHub", syntax: "@GitHub" },
  ]);
  expect(metadata.dropped_widgets).toEqual([
    expect.objectContaining({
      id: "web_search",
      type: "tool",
      label: "Web Search",
      widgetKind: "tool_toggle",
      sourceItemId: "web_search",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          id: "web_search",
          kind: "tool",
          label: "Web Search",
          syntax: "@Web Search",
          tool_id: "web_search",
        },
        tool: expect.objectContaining({
          id: "web_search",
          label: "Web Search",
          tags: ["research"],
        }),
      }),
    }),
    expect.objectContaining({
      id: "feedback/live-review",
      type: "skill",
      label: "Live Review",
      widgetKind: "skill_prompt",
      sourceItemId: "feedback/live-review",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          id: "feedback/live-review",
          kind: "skill",
          label: "Live Review",
          syntax: "@Live Review",
          skill_id: "feedback/live-review",
        },
        skill: expect.objectContaining({
          id: "feedback/live-review",
          label: "Live Review",
          aliases: ["reality", "live-review"],
        }),
      }),
    }),
    expect.objectContaining({
      id: "mention-service:github",
      type: "service",
      label: "GitHub",
      widgetKind: "service_reference",
      sourceItemId: "github",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          id: "github",
          kind: "service",
          label: "GitHub",
          syntax: "@GitHub",
        },
        service: {
          id: "github",
          label: "GitHub",
          tool_ids: ["github_issue_search"],
        },
      }),
    }),
  ]);
});

test("composer removes semantic tool state after an escaped edit", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });

  await composer.fill("Use @web");
  await expect(page.getByRole("option", { name: /@web search/i })).toBeVisible();
  await composer.press("Enter");
  await composer.fill("Use \\@Web Search");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => savedTurnRequests.length).toBe(1);

  const actualSavedTurnRequest = savedTurnRequests[0].request;
  expect(actualSavedTurnRequest.content).toBe("Use \\@Web Search");
  // Confirmed ids only temporarily promote auto to manual; escaped prose grants no selection.
  expect(actualSavedTurnRequest.tool_selection).toEqual({
    mode: "auto", include: [], exclude: [], scope: "turn", must_use: false,
  });
  for (const legacyField of ["tools", "params", "message", "metadata", "mentions", "selected_tools", "dropped_widgets"]) {
    expect(actualSavedTurnRequest).not.toHaveProperty(legacyField);
  }
});

test("composer renders semantic tool mentions inline and clears state after an escaped edit", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const chipComposer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await chipComposer.fill("Use @web");
  await expect(page.getByRole("option", { name: /@web search/i })).toBeVisible();
  await chipComposer.press("Enter");
  await expect(chipComposer).toHaveValue("Use @Web Search ");
  await expect(page.locator('[data-composer-inline-mentions] .rumi-composer-inline-mention')).toContainText("@Web Search");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe('["web_search"]');
  await chipComposer.fill("Use \\@Web Search");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => savedTurnRequests.length).toBe(1);
  const actualSavedTurnRequest = savedTurnRequests[0].request;
  expect(actualSavedTurnRequest.content).toBe("Use \\@Web Search");
  expect(actualSavedTurnRequest.tool_selection).toEqual({
    mode: "auto", include: [], exclude: [], scope: "turn", must_use: false,
  });
  for (const legacyField of ["tools", "params", "message", "metadata", "mentions", "selected_tools", "dropped_widgets"]) {
    expect(actualSavedTurnRequest).not.toHaveProperty(legacyField);
  }
});

test("composer reconciles an escaped service mention before submit", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });

  await composer.fill("Use @gith");
  const githubOption = page.getByRole("option", { name: "@GitHub（まとめ） 接続されたサービス · 1件のツールをまとめて選択 接続", exact: true });
  await expect(githubOption).toBeVisible();
  await githubOption.click();
  await expect(composer).toHaveValue("Use @GitHub ");
  await expect(page.locator('[data-composer-inline-mentions] .rumi-composer-inline-mention')).toContainText("@GitHub");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe('["github_issue_search"]');
  await composer.fill("Use \\@GitHub");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => savedTurnRequests.length).toBe(1);
  const actualSavedTurnRequest = savedTurnRequests[0].request;
  expect(actualSavedTurnRequest.content).toBe("Use \\@GitHub");
  expect(actualSavedTurnRequest.tool_selection).toEqual({
    mode: "auto", include: [], exclude: [], scope: "turn", must_use: false,
  });
  for (const legacyField of ["tools", "params", "message", "metadata", "mentions", "selected_tools", "dropped_widgets"]) {
    expect(actualSavedTurnRequest).not.toHaveProperty(legacyField);
  }
});

test("slash and mention candidates share one full-width JSON palette", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("@");
  const mentions = page.getByTestId("composer-at-mention-candidates");
  await expect(mentions).toBeVisible();
  await expect(mentions).toHaveAttribute("data-json-list-template", "composer-at-mention");
  const mentionBox = await mentions.boundingBox();
  expect(mentionBox).not.toBeNull();

  await composer.fill("/");
  const commands = page.getByTestId("composer-slash-command-candidates");
  await expect(commands).toBeVisible();
  await expect(commands).toHaveAttribute("data-json-list-template", "composer-slash-command");
  await expect(commands).toContainText("/coding");
  const commandBox = await commands.boundingBox();
  expect(commandBox).not.toBeNull();

  expect(Math.abs(commandBox!.x - mentionBox!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(commandBox!.width - mentionBox!.width)).toBeLessThanOrEqual(1);
  await expect(composer).toHaveAttribute("aria-controls", "composer-slash-command-listbox");
  await expect(composer).toHaveAttribute("aria-activedescendant", "composer-slash-command-option-0");
});

test("composer removes file mention metadata when its attachment is removed", async ({ page }) => {
  const fileRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => fileRequests.push(payload),
  });
  const fileComposer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await fileComposer.fill("/coding");
  await fileComposer.press("Enter");
  await fileComposer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toBeVisible();
  await page.getByRole("button", { name: "README.md を削除" }).click();
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => fileRequests.length).toBe(1);
  const fileMessage = fileRequests[0].message as Record<string, unknown>;
  expect(fileMessage.attachments).toBeUndefined();
  const fileMetadata = fileMessage.metadata as Record<string, unknown>;
  expect(fileMetadata.mentions).toBeUndefined();
  expect(fileMetadata.attachments).toEqual([]);
  expect(fileMetadata.dropped_widgets).toEqual([]);
});

test("Unicode tool fixture keeps display text separate from valid saved identity", () => {
  const sidebarTool = sidebarItems.find((item) => item.id === "unicode_tool");
  const catalogTool = toolCatalogTools.find((tool) => tool.tool_id === sidebarTool?.id);
  expect(sidebarTool?.label).toBe("𐐀tool");
  expect(sidebarTool?.ui.composer_label).toBe("𐐀tool");
  expect(catalogTool?.name).toBe("𐐀tool");
  expect(validSavedToolSelection({
    mode: "manual", include: [{ kind: "tool", id: catalogTool?.tool_id }],
    exclude: [], scope: "turn", must_use: true,
  })).toBe(true);
  expect(validSavedToolSelection({
    mode: "manual", include: [{ kind: "tool", id: catalogTool?.name }],
    exclude: [], scope: "turn", must_use: true,
  })).toBe(false);
});

test("unregistered Unicode sidebar tools are not offered as confirmed references", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  let catalogRead = false;
  const catalogBinding = frontendFixtureBinding("toolCatalog");
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const binding = frontendFixtureRequest(request.url(), request.method());
    if (!matchesFrontendFixtureBinding(binding, catalogBinding)) return route.fallback();
    catalogRead = true;
    const tools = toolCatalogTools.filter((tool) => tool.tool_id !== "unicode_tool");
    return fulfill(route, { services: toolCatalogServices, tools, count: tools.length });
  });
  await page.goto(frontendHostFixtureScreenPath("/chat"));
  await expect.poll(() => catalogRead).toBe(true);
  await page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" }).fill("Use @𐐀");
  await expect(page.getByRole("listbox", { name: "メンション候補", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: /@𐐀tool/ })).toHaveCount(0);
});

test("composer supplementary-plane mention keeps textarea and parser indices aligned", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });

  await composer.fill("先𐐀 @𐐀");
  await expect(page.getByRole("option", { name: /@𐐀tool/i })).toBeVisible();
  await composer.press("Enter");
  await expect(composer).toHaveValue("先𐐀 @𐐀tool ");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => savedTurnRequests.length).toBe(1);
  const actualSavedTurnRequest = savedTurnRequests[0].request;
  expect(actualSavedTurnRequest.content).toBe("先𐐀 @𐐀tool");
  expect(actualSavedTurnRequest.tool_selection).toEqual({
    mode: "manual", include: [{ kind: "tool", id: "unicode_tool" }], exclude: [], scope: "turn", must_use: true,
  });
  for (const legacyField of ["tools", "params", "message", "metadata", "mentions", "selected_tools", "dropped_widgets"]) {
    expect(actualSavedTurnRequest).not.toHaveProperty(legacyField);
  }
});

test("composer removes a no-space mention atomically without leaving tool state", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });

  await composer.fill("Use @𐐀");
  await composer.press("Enter");
  await expect(composer).toHaveValue("Use @𐐀tool ");
  await expect(page.getByRole("button", { name: "𐐀tool", exact: true })).toHaveCount(0);
  await composer.press("End");
  await composer.press("Backspace");
  await composer.press("Backspace");
  await expect(composer).toHaveValue("Use ");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  await composer.pressSequentially("and summarize the result");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => savedTurnRequests.length).toBe(1);

  const actualSavedTurnRequest = savedTurnRequests[0].request;
  expect(actualSavedTurnRequest.content).toBe("Use and summarize the result");
  expect(actualSavedTurnRequest.tool_selection).toEqual({
    mode: "auto", include: [], exclude: [], scope: "turn", must_use: false,
  });
  for (const legacyField of ["tools", "params", "message", "metadata", "mentions", "selected_tools", "dropped_widgets"]) {
    expect(actualSavedTurnRequest).not.toHaveProperty(legacyField);
  }
});

test("editing and reselecting an atomically deleted no-space mention restores it", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });

  const mentions = page.getByTestId("composer-at-mention-candidates");
  const unicodeOption = mentions.getByRole("option", { name: /^@𐐀tool(?:\s|$)/ });
  const confirmUnicodeMention = async (prefix: string) => {
    await composer.fill(`${prefix}@𐐀`);
    await expect(mentions).toBeVisible();
    await expect(unicodeOption).toBeVisible();
    await expect(unicodeOption).toHaveAttribute("aria-selected", "true");
    const optionId = await unicodeOption.getAttribute("id");
    expect(optionId).toBeTruthy();
    await expect(composer).toHaveAttribute("aria-activedescendant", optionId!);
    await composer.press("Enter");
    await expect(composer).toHaveValue(`${prefix}@𐐀tool `);
    await expect(mentions).toBeHidden();
    await expect.poll(() => composer.evaluate((element: HTMLTextAreaElement) => element.selectionStart)).toBe(`${prefix}@𐐀tool `.length);
    expect(savedTurnRequests).toHaveLength(0);
  };

  await confirmUnicodeMention("Use ");
  await composer.press("End");
  await composer.press("Backspace");
  await composer.press("Backspace");
  await expect(composer).toHaveValue("Use ");
  await expect(page.locator('[data-composer-inline-mentions] .rumi-composer-inline-mention').filter({ hasText: "@𐐀tool" })).toHaveCount(0);
  await confirmUnicodeMention("Use again ");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => savedTurnRequests.length).toBe(1);

  const actualSavedTurnRequest = savedTurnRequests[0].request;
  expect(actualSavedTurnRequest.content).toBe("Use again @𐐀tool");
  expect(actualSavedTurnRequest.tool_selection).toEqual({
    mode: "manual", include: [{ kind: "tool", id: "unicode_tool" }], exclude: [], scope: "turn", must_use: true,
  });
  for (const legacyField of ["tools", "params", "message", "metadata", "mentions", "selected_tools", "dropped_widgets"]) {
    expect(actualSavedTurnRequest).not.toHaveProperty(legacyField);
  }
  await expect(page.getByText("Saved response accepted.", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("");
  await expect(composer).toBeEditable();
  expect(savedTurnRequests).toHaveLength(1);
});

test("workspace mention waits for its attachment before submit", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();

  const pendingSend = page.getByRole("button", { name: "ファイルを読み込み中" });
  await expect(pendingSend).toBeDisabled();
  await expect(page.getByRole("status", { name: "README.md を読み込み中" }).first()).toBeVisible();
  await composer.press("Enter");
  expect(streamRequests).toHaveLength(0);

  releaseRead();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toBeVisible();
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  const message = streamRequests[0].message as Record<string, unknown>;
  expect(message.attachments).toEqual([
    expect.objectContaining({ name: "README.md", sourcePath: "README.md" }),
  ]);
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "README.md", kind: "file", label: "README.md", syntax: "@README.md" },
  ]);
});

test("cancelling a pending workspace mention discards its late result", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await page.getByRole("button", { name: "README.md の読み込みを取り消す" }).click();

  releaseRead();
  await expect(page.getByRole("status", { name: "README.md を読み込み中" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "README.md を削除" })).toHaveCount(0);
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  const message = streamRequests[0].message as Record<string, unknown>;
  expect(message.attachments).toBeUndefined();
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.mentions).toBeUndefined();
  expect(metadata.attachments).toEqual([]);
});

test("cancelling one pending workspace mention preserves another transaction", async ({ page }) => {
  let releaseReadme!: () => void;
  let releaseApp!: () => void;
  const readmeGate = new Promise<void>((resolve) => { releaseReadme = resolve; });
  const appGate = new Promise<void>((resolve) => { releaseApp = resolve; });
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: (payload) => (
      payload.path === "README.md" ? readmeGate : appGate
    ),
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await composer.press("End");
  await composer.pressSequentially(" @src");
  await page.getByRole("option").filter({ hasText: "@src/App.tsx" }).click();

  await page.getByRole("button", { name: "README.md の読み込みを取り消す" }).click();
  releaseApp();
  await expect(page.getByRole("button", { name: "App.tsx を削除" })).toBeVisible();
  releaseReadme();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toHaveCount(0);
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);

  const message = streamRequests[0].message as Record<string, unknown>;
  expect(message.attachments).toEqual([
    expect.objectContaining({ name: "src/App.tsx", sourcePath: "src/App.tsx" }),
  ]);
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "src/App.tsx", kind: "file", label: "src/App.tsx", syntax: "@src/App.tsx" },
  ]);
});

test("starting a new draft discards a pending workspace mention result", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await page.getByTitle("New Chat").first().click();
  await expect(page.locator(".rumi-new-chat-stage")).toBeVisible();

  releaseRead();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toHaveCount(0);
  await expect(page.getByRole("status", { name: "README.md を読み込み中" })).toHaveCount(0);
});

test("migrated keyboard navigation marker keeps composer controls reachable", async ({ page }) => {
  await openDefaultspack(page, "/chat", {
    initialSettingsValues: {
      general: {
        settings_version: 2,
        keyboard_button_navigation: true,
        keyboard_button_navigation_source: "legacy_default_migrated",
        composer_placeholder: "Migrated placeholder",
      },
    },
  });

  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.focus();
  await composer.press("Tab");
  await expect(composer).not.toBeFocused();
});

test("composer mention keyboard and ARIA contracts stay predictable at Unicode and empty boundaries", async ({ page }) => {
  const savedRequests: Array<{ request: SavedTurnRequest }> = [];
  await openDefaultspack(page, "/chat", {
    onSavedTurnRequest: (payload) => savedRequests.push(payload),
  });

  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  const mentions = page.getByTestId("composer-at-mention-candidates");

  await composer.fill("@");
  await expect(mentions).toBeVisible();
  await expect(mentions.getByRole("option").first()).toBeVisible();

  await composer.fill("調べて@web");
  await expect(mentions).toBeVisible();
  await expect(composer).toHaveAttribute("aria-expanded", "true");
  await expect(composer).toHaveAttribute("aria-controls", "composer-at-mention-listbox");
  await expect(composer).toHaveAttribute("aria-activedescendant", "composer-at-mention-option-0");
  await expect(page.getByRole("option", { name: /@web search/i })).toHaveAttribute("aria-selected", "true");

  await composer.fill("調べて @web_search。");
  await expect(mentions).toBeHidden();
  await expect(composer).toHaveAttribute("aria-expanded", "false");

  await composer.fill("mail@example.com https://example.com/@name \\@web_search @@web_search");
  await expect(mentions).toBeHidden();

  await composer.fill("ユーザー@example.com https://example.com/日本@pm");
  await expect(mentions).toBeHidden();

  await composer.fill("https://example.com。@web");
  await expect(mentions).toBeVisible();
  await expect(page.getByRole("option", { name: /@web search/i })).toBeVisible();

  await composer.fill("https://example.com)@web");
  await expect(mentions).toBeVisible();
  await expect(page.getByRole("option", { name: /@web search/i })).toBeVisible();

  await composer.fill("@this_candidate_does_not_exist");
  await expect(mentions).toBeVisible();
  await expect(page.getByTestId("composer-at-mention-empty")).toBeVisible();
  await expect(composer).not.toHaveAttribute("aria-activedescendant");
  await composer.press("Tab");
  await expect(composer).not.toBeFocused();

  await composer.focus();
  await composer.evaluate((element) => {
    const end = element.value.length;
    element.setSelectionRange(end, end);
  });
  await expect.poll(() => composer.evaluate((element) => element.selectionStart)).toBe(
    "@this_candidate_does_not_exist".length,
  );
  await composer.press("Escape");
  await expect(mentions).toBeHidden();

  await composer.fill("@this_candidate_does_not_exist");
  await composer.evaluate((element) => {
    const end = element.value.length;
    element.setSelectionRange(end, end);
  });
  await expect.poll(() => composer.evaluate((element) => element.selectionStart)).toBe(
    "@this_candidate_does_not_exist".length,
  );
  await composer.press("Shift+Enter");
  await expect(composer).toHaveValue("@this_candidate_does_not_exist\n");
  expect(savedRequests).toHaveLength(0);

  await composer.fill("@this_candidate_does_not_exist");
  await composer.press("Enter");
  await expect.poll(() => savedRequests.length).toBe(1);
  expect(savedRequests[0].request.content).toBe("@this_candidate_does_not_exist");
  expect(savedRequests[0].request.tool_selection?.include ?? []).toEqual([]);
  expect(savedRequests[0].request).not.toHaveProperty("metadata");
  await expect(page.getByText("Saved response accepted.", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("");
  await expect(composer).toBeEditable();
  await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
  expect(savedRequests).toHaveLength(1);
});

test("coding file mentions keep stable semantic metadata through submit", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });

  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  await composer.fill("確認@README.md");
  const readmeOption = page.getByRole("option").filter({ hasText: "@README.md" });
  await expect(readmeOption).toBeVisible();
  await readmeOption.click();
  await expect(composer).toHaveValue("確認@README.md ");
  await expect(page.locator(".rumi-composer-frame")).toContainText("README.md");

  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  const sentMessage = streamRequests[0].message as Record<string, unknown>;
  const metadata = sentMessage.metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "README.md", kind: "file", label: "README.md", syntax: "@README.md" },
  ]);
  expect(metadata.dropped_widgets).toEqual([
    expect.objectContaining({
      id: "mention-file:README.md",
      type: "file",
      label: "README.md",
      sourceItemId: "README.md",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          file_path: "README.md",
          id: "README.md",
          kind: "file",
          label: "README.md",
          syntax: "@README.md",
        },
      }),
    }),
  ]);
});

async function assertVisibleTouchTarget(control: Locator) {
  await expect(control).toBeVisible();
  await control.scrollIntoViewIfNeeded();
  await expect.poll(() => control.evaluate((element) => {
    let active = 0;
    for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
      active += ancestor.getAnimations().filter((animation) => animation.pending || animation.playState === "running").length;
    }
    return active;
  })).toBe(0);
  const box = await control.boundingBox();
  const label = await control.getAttribute("aria-label");
  expect(label).toBeTruthy();
  expect(box).not.toBeNull();
  expect(box!.width, `${label} width`).toBeGreaterThanOrEqual(44);
  expect(box!.height, `${label} height`).toBeGreaterThanOrEqual(44);
  expect(await control.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return hit === element || (hit !== null && element.contains(hit));
  }), `${label} receives input at its visible center`).toBe(true);
}

test.describe("coarse-pointer composer controls", () => {
  test.use({ hasTouch: true });

  test("composer controls are keyboard reachable, visibly named, and at least 44px", async ({ page }) => {
    await openDefaultspack(page, "/chat");
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await composer.fill("Keyboard reachability draft");
    const controls = [
      page.getByRole("button", { name: "添付とコマンド", exact: true }),
      page.getByRole("button", { name: "Start reviewable voice input", exact: true }),
      page.getByRole("button", { name: "アクションの承認方法", exact: true }),
      page.getByRole("button", { name: /^モデル:/ }),
      page.getByRole("button", { name: "メッセージを送信", exact: true }),
    ];
    for (const control of controls) await assertVisibleTouchTarget(control);
    await composer.focus();
    const remaining = new Set(controls);
    const tabBudget = await page.locator("button,input,textarea,select,a[href],[tabindex]").count() + 1;
    for (let step = 0; step < tabBudget && remaining.size; step++) {
      await page.keyboard.press("Tab");
      for (const control of remaining) {
        if (await control.evaluate((element) => element === document.activeElement)) {
          await expect(control).toBeFocused();
          expect(await control.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");
          remaining.delete(control);
        }
      }
    }
    expect(remaining.size, "every named control is reachable through normal Tab navigation").toBe(0);
  });

  test("mobile composer attachment target stays 44px and opens the file menu without overlapping its editor", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const savedTurnRequests: Array<{ request: SavedTurnRequest }> = [];
    await openDefaultspack(page, "/chat", { onSavedTurnRequest: (payload) => savedTurnRequests.push(payload) });
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
    const plus = page.getByRole("button", { name: "添付とコマンド", exact: true });
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await composer.fill("Mobile target draft");
    await assertVisibleTouchTarget(plus);
    await assertVisibleTouchTarget(page.getByRole("button", { name: "メッセージを送信", exact: true }));
    const plusBox = await plus.boundingBox();
    const editorBox = await composer.boundingBox();
    expect(plusBox).not.toBeNull();
    expect(editorBox).not.toBeNull();
    expect(plusBox!.x + plusBox!.width).toBeLessThanOrEqual(editorBox!.x);
    await expect(page.locator('[data-composer-widget="file-attach"]')).toHaveCSS("min-width", "44px");
    await plus.tap();
    await expect(plus).toHaveAttribute("aria-expanded", "true");
    const commands = page.getByRole("listbox", { name: "Composer commands", exact: true });
    const attach = commands.getByRole("option", { name: /^\/attach(?:\s|$)/ });
    await expect(commands).toBeVisible();
    await expect(attach).toBeVisible();
    await composer.press("Escape");
    await expect(plus).toHaveAttribute("aria-expanded", "false");
    await expect(composer).toHaveValue("Mobile target draft");

    await plus.tap();
    await expect(attach).toBeEnabled();
    const fileChooser = page.waitForEvent("filechooser");
    await attach.tap();
    const chooser = await fileChooser;
    expect(chooser.isMultiple()).toBe(true);
    expect(await chooser.element().getAttribute("type")).toBe("file");
    expect(await chooser.element().getAttribute("accept")).toBeNull();
    // Exercise the actual chooser action without fabricating an upload.
    await chooser.setFiles([]);
    await expect(commands).toBeHidden();
    await expect(plus).toHaveAttribute("aria-expanded", "false");
    await expect(composer).toHaveValue("Mobile target draft");
    expect(savedTurnRequests).toEqual([]);
  });
});

test("composer uses a leading plus menu and accepts clipboard and workspace file drops", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  await openDefaultspack(page, "/chat", { onSavedTurnRequest: (payload) => savedTurnRequests.push(payload) });
  await page.getByTitle("New Chat").first().click();
  await expect(page.locator(".rumi-composer-new")).toHaveCSS("filter", "none");

  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  const attach = page.getByRole("button", { name: "添付とコマンド", exact: true });
  const composerBox = await composer.boundingBox();
  const attachBox = await attach.boundingBox();
  expect(composerBox).not.toBeNull();
  expect(attachBox).not.toBeNull();
  // Home editor children use self-end: the desktop plus is 32px beside a 44px
  // textarea. Assert the actual shared baseline, not equal-height centers.
  expect(attachBox!.x + attachBox!.width).toBeLessThanOrEqual(composerBox!.x);
  expect(attachBox!.y + attachBox!.height).toBe(composerBox!.y + composerBox!.height);
  expect(attachBox!.y).toBeGreaterThanOrEqual(composerBox!.y);
  expect(await attach.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return hit === element || (hit !== null && element.contains(hit));
  })).toBe(true);

  await attach.click();
  const commands = page.getByRole("listbox", { name: "Composer commands", exact: true });
  await expect(commands).toBeVisible();
  await expect(commands.getByRole("option", { name: /\/attach/ })).toBeVisible();
  await attach.click();
  await expect(commands).toBeHidden();
  await expect(composer).toBeEditable();

  await composer.evaluate((target) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(["clipboard"], "clipboard.txt", { type: "text/plain" }));
    target.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dataTransfer,
    }));
  });
  const removeClipboardAttachment = page.getByRole("button", { name: "clipboard.txt を削除" });
  await expect(removeClipboardAttachment).toBeVisible();
  await expect.poll(() => removeClipboardAttachment.evaluate((element) => {
    for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.getAnimations().some((animation) => animation.pending || animation.playState === "running")) return false;
    }
    return true;
  })).toBe(true);
  const removeAttachmentBox = await removeClipboardAttachment.boundingBox();
  expect(removeAttachmentBox).not.toBeNull();
  expect(removeAttachmentBox!.width).toBeGreaterThanOrEqual(44);
  expect(removeAttachmentBox!.height).toBeGreaterThanOrEqual(44);
  const attachmentRegion = page.locator("[data-composer-attachment-region]");
  const composerPanel = page.locator(".rumi-composer-main-panel");
  await expect(attachmentRegion).toHaveAttribute("data-attachment-state", "expanded");
  await expect(composerPanel.locator("[data-composer-attachment-region]")).toHaveCount(1);
  const regionBox = await attachmentRegion.boundingBox();
  const inputBoxAfterAttachment = await composer.boundingBox();
  expect(regionBox).not.toBeNull();
  expect(inputBoxAfterAttachment).not.toBeNull();
  expect(regionBox!.y).toBeLessThan(inputBoxAfterAttachment!.y);
  const attachmentTransition = await attachmentRegion.evaluate(
    (element) => getComputedStyle(element).transitionProperty,
  );
  expect(attachmentTransition).toContain("grid-template-rows");

  await page.locator("main").evaluate((target) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(["drop"], "workspace-drop.txt", { type: "text/plain" }));
    target.dispatchEvent(new DragEvent("dragenter", {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    }));
  });
  await expect(page.getByRole("status", { name: "ファイルをここにドロップ" })).toBeVisible();
  await page.locator("main").evaluate((target) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(["drop"], "workspace-drop.txt", { type: "text/plain" }));
    target.dispatchEvent(new DragEvent("drop", {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    }));
  });
  await expect(page.getByRole("status", { name: "ファイルをここにドロップ" })).toBeHidden();
  await expect(page.getByRole("button", { name: "workspace-drop.txt を削除" })).toBeVisible();
  await removeClipboardAttachment.click();
  await page.getByRole("button", { name: "workspace-drop.txt を削除" }).click();
  await expect(attachmentRegion).toHaveAttribute("data-attachment-state", "collapsed");
  await expect.poll(async () => (await attachmentRegion.boundingBox())?.height ?? -1).toBe(0);
  await expect(composer).toBeEditable();
  expect(savedTurnRequests).toEqual([]);
});

test("composer mentions paste portably and delete as one semantic unit", async ({ page }) => {
  await openDefaultspack(page, "/chat");
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });

  await composer.evaluate((target) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.setData("text/plain", '[@Web Search](plugin://web_search@openai-bundled")');
    target.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dataTransfer,
    }));
  });
  await expect(composer).toHaveValue("@Web Search");
  await expect(page.locator("[data-composer-inline-mentions]")).toContainText("@Web Search");

  await composer.press("End");
  await composer.press("Backspace");
  await expect(composer).toHaveValue("");
  await expect(page.locator("[data-composer-inline-mentions]")).toHaveCount(0);
});

test("attachment remove and cancel actions expose 44px visible focus targets", async ({ page }) => {
  const settledAttachmentBox = async (control: Locator) => {
    const reveal = page.locator("[data-composer-attachment-region]").filter({ has: control });
    await expect(reveal).toHaveAttribute("data-attachment-state", "expanded");
    await expect(reveal).toHaveCSS("opacity", "1");
    await expect(reveal).toHaveCSS("transform", "matrix(1, 0, 0, 1, 0, 0)");
    // The reveal animates its clipped grid as well as translation. Wait for
    // these actual transitions, not an arbitrary delay or rounded geometry.
    await expect.poll(() => control.evaluate((element) => {
      let active = 0;
      // Include the chat pane's mount animation, but not descendant spinners.
      for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
        active += ancestor.getAnimations().filter((animation) => animation.pending
          || animation.playState === "running").length;
      }
      return active;
    })).toBe(0);
    await expect(control).toBeVisible();
    const geometry = await control.evaluate(async (element) => {
      const before = element.getBoundingClientRect().toJSON();
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      return {
        before, after: element.getBoundingClientRect().toJSON(),
        clip: element.closest(".rumi-composer-attachment-reveal-inner")?.getBoundingClientRect().toJSON(),
      };
    });
    expect(geometry.after).toEqual(geometry.before);
    expect(geometry.clip).toBeDefined();
    expect(geometry.after.top).toBeGreaterThanOrEqual(geometry.clip!.top);
    expect(geometry.after.bottom).toBeLessThanOrEqual(geometry.clip!.bottom);
    expect(geometry.after.left).toBeGreaterThanOrEqual(geometry.clip!.left);
    expect(geometry.after.right).toBeLessThanOrEqual(geometry.clip!.right);
    return control.boundingBox();
  };
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
  });
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();

  const cancel = page.getByRole("button", { name: "README.md の読み込みを取り消す" });
  const cancelBox = await settledAttachmentBox(cancel);
  expect(cancelBox).not.toBeNull();
  expect(cancelBox!.width).toBeGreaterThanOrEqual(44);
  expect(cancelBox!.height).toBeGreaterThanOrEqual(44);
  await cancel.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(cancel).toBeFocused();
  expect(await cancel.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");

  releaseRead();
  const inlineRemove = page.getByRole("button", { name: "README.md を削除" });
  await expect(inlineRemove).toBeVisible();
  const inlineBox = await settledAttachmentBox(inlineRemove);
  expect(inlineBox).not.toBeNull();
  expect(inlineBox!.width).toBeGreaterThanOrEqual(44);
  expect(inlineBox!.height).toBeGreaterThanOrEqual(44);
  await inlineRemove.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(inlineRemove).toBeFocused();
  expect(await inlineRemove.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");

  await page.getByTitle("New Chat").first().click();
  const newComposer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await newComposer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  const cardRemove = page.getByRole("button", { name: "README.md を削除" });
  const cardBox = await settledAttachmentBox(cardRemove);
  expect(cardBox).not.toBeNull();
  expect(cardBox!.width).toBeGreaterThanOrEqual(44);
  expect(cardBox!.height).toBeGreaterThanOrEqual(44);
  await cardRemove.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(cardRemove).toBeFocused();
  const focusedCardStyle = await cardRemove.evaluate((element) => ({
    outlineStyle: getComputedStyle(element).outlineStyle,
    opacity: getComputedStyle(element).opacity,
  }));
  expect(focusedCardStyle.opacity).toBe("1");
  expect(focusedCardStyle.outlineStyle).not.toBe("none");
});

test("history reload restores localized semantic mentions inline", async ({ page }) => {
  await openDefaultspack(page, "/chat");
  const mention = page.locator(".rumi-message-mention").filter({ hasText: "@Web Search" });
  const assertLocalizedMention = async () => {
    await expect(mention).toHaveCount(1);
    await expect(mention).toBeVisible();
    await expect(mention).toHaveText("@Web Search");
    const message = mention.locator("..");
    await expect(message).toHaveText("Show the current @Web Search state.");
    await expect(message).not.toContainText("web_search");
    await expect(page.getByTestId("message-mention-badge")).toHaveCount(0);
  };
  await assertLocalizedMention();

  let newDocument = false;
  const freshReads = new Set<Request>();
  page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) newDocument = true; });
  page.on("request", (request) => {
    if (newDocument && requestConversationId(request) === "c-smoke") freshReads.add(request);
  });
  const read = page.waitForResponse((response) => freshReads.has(response.request()));
  await page.goto(frontendHostFixtureScreenPath("/chat"));
  const response = await read;
  expect(newDocument).toBe(true);
  expect(response.status()).toBe(200);
  expect(await response.json()).toMatchObject({ data: { id: "c-smoke" } });
  await assertLocalizedMention();
});

test("composer browser behavior covers long text popovers and mobile coding trust", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  await page.getByTitle("New Chat").first().click();
  await expect(page.locator(".rumi-new-chat-stage")).toBeVisible();

  const homeComposer = page.locator("textarea.rumi-composer-textarea");
  const longPrompt = Array.from({ length: 80 }, (_, index) => `長文入力 ${index} @README.md`).join("\n");
  await homeComposer.fill(longPrompt);
  await expect(homeComposer).toHaveValue(longPrompt);
  await expect(page.locator(".rumi-composer-mention-overlay")).toHaveCount(0);
  const homeMetrics = await homeComposer.evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      color: style.color,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    };
  });
  expect(homeMetrics.color).not.toBe("rgba(0, 0, 0, 0)");
  expect(homeMetrics.scrollHeight).toBeGreaterThan(homeMetrics.clientHeight);

  await homeComposer.fill("/coding");
  await expect(page.getByText("Commands")).toBeVisible();

  await openDefaultspack(page, "/chat");
  const codingComposer = page.locator("textarea.rumi-composer-textarea");
  await codingComposer.fill("/coding");
  await codingComposer.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  await codingComposer.fill("@REA");
  const mentions = page.getByTestId("composer-at-mention-candidates");
  await expect(mentions).toBeVisible();
  await expect(mentions).toContainText("README.md");
  const mentionBox = await mentions.boundingBox();
  expect(mentionBox).not.toBeNull();
  expect(mentionBox!.x).toBeGreaterThanOrEqual(0);
  expect(mentionBox!.y).toBeGreaterThanOrEqual(0);
  expect(mentionBox!.x + mentionBox!.width).toBeLessThanOrEqual(page.viewportSize()!.width);

  await page.getByLabel("close mention menu").click({ position: { x: 4, y: 4 } });
  await expect(mentions).toBeHidden();

  await page.setViewportSize({ width: 390, height: 820 });
  const workspacePicker = page.locator(".rumi-workspace-picker");
  await expect(workspacePicker).toBeVisible();
  await expect(workspacePicker.locator("svg.text-emerald-300").first()).toBeVisible();
});

async function openSchedulerCanvas(page: Page): Promise<Locator> {
  const sidebar = page.getByRole("complementary", { name: "Tools and utility panels" });
  await sidebar.getByRole("button", { name: "機能", exact: true }).click();
  await page.getByPlaceholder("機能を検索").fill("scheduler");
  const scheduler = page.getByTestId("tool-manager-candidates").getByRole("button", { name: /Scheduler/ });
  await expect(scheduler).toHaveCount(1);
  await scheduler.click();
  await expect(sidebar.getByText("Calendar and trigger smoke surface.", { exact: true })).toBeVisible();
  const read = page.waitForResponse((response) => canonicalRequestQuery(response.request(), "api/agent/schedules", "GET") !== null);
  await sidebar.getByTitle("Calendar", { exact: true }).click();
  const response = await read;
  expect(response.status()).toBe(200);
  expect(frontendFixtureRequest(response.request().url(), "GET")).toMatchObject({
    contributionId: "defaults.calendar.schedules.list",
    contractId: "tobkiri.resource.calendar.schedule.v1",
    operationId: "rumi_schedule_store_pack.calendar-read",
  });
  expect(await response.json()).toMatchObject({ data: { schedules: [{ id: "nightly-review", name: "nightly-review", schedule: "every 1h" }] } });
  const canvas = page.getByTestId("canvas-widget-panel");
  await expect(canvas).toBeVisible();
  await expect(canvas.getByRole("button", { name: "Calendar.json", exact: true })).toBeVisible();
  await expect(canvas).toContainText("nightly-review");
  return canvas;
}

test("resizable canvas and tool widgets persist their shared width", async ({ page }) => {
  // A fresh test context starts empty; preserve its real width across navigation.
  await openDefaultspack(page, "/chat", { preserveLocalStorage: true });
  const canvas = await openSchedulerCanvas(page);
  const canvasHandle = canvas.getByRole("separator", { name: "Canvas widget幅を変更", exact: true });
  const widthKey = "rumi-right-sidebar-panel-width";
  const dragWider = async (handle: Locator, delta: number) => {
    await expect(handle).toBeVisible();
    // A tool panel can still be sliding into place when it first becomes visible.
    await expect.poll(() => handle.evaluate((element) => {
      let active = 0;
      for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
        active += ancestor.getAnimations().filter((animation) => animation.pending
          || animation.playState === "running").length;
      }
      return active;
    })).toBe(0);
    const initial = Number(await handle.getAttribute("aria-valuenow"));
    const expected = Math.min(520, initial + delta);
    // This test proves movement, not a no-op at an already persisted width limit.
    expect(expected).toBeGreaterThan(initial);
    const bounds = await handle.boundingBox();
    expect(bounds).not.toBeNull();
    const x = bounds!.x + bounds!.width / 2;
    const y = bounds!.y + bounds!.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x - delta, y, { steps: 5 });
    await page.mouse.up();
    await expect(handle).toHaveAttribute("aria-valuenow", String(expected));
    await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), widthKey)).toBe(String(expected));
    return expected;
  };
  const canvasWidth = await dragWider(canvasHandle, 80);
  await expect(canvas).toHaveCSS("width", `${canvasWidth}px`);

  const sidebar = page.getByRole("complementary", { name: "Tools and utility panels" });
  await sidebar.getByRole("button", { name: "機能", exact: true }).click();
  const toolHandle = sidebar.getByRole("separator", { name: "機能パネル幅を変更", exact: true });
  await expect(toolHandle).toHaveAttribute("aria-valuenow", String(canvasWidth));
  const sharedWidth = await dragWider(toolHandle, 90);
  await sidebar.getByRole("button", { name: "Canvas", exact: true }).click();
  await expect(canvas).toBeVisible();
  await expect(canvas).toHaveCSS("width", `${sharedWidth}px`);

  await page.goto(frontendHostFixtureScreenPath("/chat"));
  await expect(page.getByRole("tab", { name: "Preview Calendar Chat", exact: true })).toBeVisible();
  const restored = await openSchedulerCanvas(page);
  await expect(restored.getByRole("separator", { name: "Canvas widget幅を変更", exact: true })).toHaveAttribute("aria-valuenow", String(sharedWidth));
  await expect(restored).toHaveCSS("width", `${sharedWidth}px`);
});

test("open utility panel never covers the Home composer at desktop breakpoints", async ({ page }) => {
  await page.setViewportSize({ width: 980, height: 760 });
  await openDefaultspack(page, "/chat");
  await page.getByTitle("New Chat").first().click();
  await page.locator('button[title="機能"]').click();

  const panel = page.locator(".rumi-right-sidebar-panel:not([hidden])");
  const composer = page.locator(".rumi-composer-frame");
  const send = page.locator(".rumi-send-button");
  await expect(panel).toBeVisible();
  await expect(send).toBeVisible();

  const [panelBox, composerBox, sendBox] = await Promise.all([
    panel.boundingBox(),
    composer.boundingBox(),
    send.boundingBox(),
  ]);
  expect(panelBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(sendBox).not.toBeNull();
  expect(composerBox!.x + composerBox!.width).toBeLessThanOrEqual(panelBox!.x + 1);
  expect(sendBox!.x + sendBox!.width).toBeLessThanOrEqual(panelBox!.x + 1);

  const sendOwnsCenterPoint = await send.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return hit === element || element.contains(hit);
  });
  expect(sendOwnsCenterPoint).toBe(true);
});

test("model picker search supports @provider filters on registered routes", async ({ page }) => {
  await openDefaultspack(page);
  const selected = page.getByRole("button", { name: "モデル: Stub Default", exact: true });
  await selected.click();
  const search = page.getByPlaceholder(/モデルを検索/);
  const models = page.getByRole("listbox", { name: "登録済みモデル", exact: true });
  await search.fill("@opencode");
  await expect(models.getByRole("option")).toHaveCount(2);
  await expect(models.getByRole("option", { name: /Qwen3.5 Plus via OpenCode Go/ })).toBeVisible();
  await expect(models.getByRole("option", { name: /MiniMax M3 Free via OpenCode Zen/ })).toBeVisible();
  await expect(models.getByRole("option", { name: /Gemini 2.5 Flash/ })).toHaveCount(0);

  await search.fill("@opencode-zen");
  await expect(models.getByRole("option")).toHaveCount(1);
  await expect(models.getByRole("option", { name: /MiniMax M3 Free via OpenCode Zen/ })).toBeVisible();
  await expect(models.getByRole("option", { name: /Qwen3.5 Plus via OpenCode Go/ })).toHaveCount(0);

  await search.fill("@google flash");
  await expect(models.getByRole("option")).toHaveCount(1);
  await expect(models.getByRole("option", { name: /Gemini 2.5 Flash/ })).toBeVisible();
  await search.fill("@unregistered-provider");
  await expect(models.getByRole("option")).toHaveCount(0);
  await search.press("Escape");
  await expect(models).toBeHidden();
  await expect(selected).toBeVisible();
});

test("model picker keeps unconfigured opencode zen visible for first-run setup", async ({ page }) => {
  await openDefaultspack(page);

  await page.getByRole("button", { name: /Stub Default/ }).click();
  const search = page.getByPlaceholder(/モデルを検索/);
  await search.fill("minimax");
  await expect(page.getByText("MiniMax M3 Free via OpenCode Zen")).toBeVisible();
});

test("preview pane opens from the chat canvas peek", async ({ page }) => {
  await openDefaultspack(page, "/chat", { calendarPreview: true });

  await page.getByTitle("Canvas を開く").click();

  const preview = page.getByLabel("Activity preview");
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("calendar-smoke.json");
});

test("calendar action renders a scheduler preview in the Canvas widget", async ({ page }) => {
  await openDefaultspack(page);
  const canvas = await openSchedulerCanvas(page);
  await expect(canvas).toContainText("Calendar.json");
  await expect(canvas).toContainText("nightly-review");
  await canvas.getByRole("button", { name: "Canvasを閉じる", exact: true }).click();
  await expect(canvas).toBeHidden();
  await page.getByRole("complementary", { name: "Tools and utility panels" }).getByRole("button", { name: "Canvas", exact: true }).click();
  await expect(canvas).toBeVisible();
  await expect(canvas).toContainText("nightly-review");
});

test("calendar mode opens quick add and renders new tasks in blue", async ({ page }) => {
  await openDefaultspack(page, "/coding");

  await page.locator('button[title="Calendar"]').first().click();
  await expect(page.getByLabel("Calendar month")).toBeVisible();

  const now = new Date();
  const dayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-09`;
  const nextDayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-10`;
  const nextMonth = new Date(now.getFullYear(), now.getMonth() + 1, 1);
  const nextMonthKey = `${nextMonth.getFullYear()}-${String(nextMonth.getMonth() + 1).padStart(2, "0")}-01`;
  const dayLabel = `${now.getFullYear()}年${now.getMonth() + 1}月9日`;
  const nextDayLabel = `${now.getFullYear()}年${now.getMonth() + 1}月10日`;
  await page.getByLabel("次の月").click();
  await expect(page.getByTestId(`calendar-day-${nextMonthKey}`)).toBeVisible();
  await page.getByLabel("今日").click();
  await page.getByTestId(`calendar-day-${dayKey}`).click();
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toBeHidden();
  await expect(page.getByRole("dialog", { name: `${nextDayLabel}に追加` })).toBeHidden();
  await page.getByTestId(`calendar-day-${nextDayKey}`).click();
  await expect(page.getByRole("dialog", { name: `${nextDayLabel}に追加` })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId(`calendar-day-${dayKey}`).click();
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toBeVisible();

  await page.getByPlaceholder("何を追加しますか？").fill("Design review");
  await page.getByRole("button", { name: "追加", exact: true }).click();

  const task = page.getByText("Design review");
  await expect(task).toBeVisible();
  await expect(task).toHaveClass(/bg-blue-500\/90/);
  await task.click();
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toContainText("項目を編集");
  await page.getByLabel("カレンダー項目の時刻").click();
  await expect(page.getByRole("listbox", { name: "カレンダー時刻候補" })).toContainText("午前12:30");
  await page.getByPlaceholder("何を追加しますか？").fill("Design review edited");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Design review edited")).toBeVisible();

  const rangeStart = page.getByTestId(`${"calendar-day"}-${dayKey.replace("-09", "-12")}`);
  const rangeEnd = page.getByTestId(`${"calendar-day"}-${dayKey.replace("-09", "-14")}`);
  const startBox = await rangeStart.boundingBox();
  const endBox = await rangeEnd.boundingBox();
  expect(startBox).not.toBeNull();
  expect(endBox).not.toBeNull();
  await page.mouse.move(startBox!.x + startBox!.width / 2, startBox!.y + startBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(endBox!.x + endBox!.width / 2, endBox!.y + endBox!.height / 2, { steps: 6 });
  await page.mouse.up();
  await expect(page.getByRole("dialog", { name: `${dayLabel.replace("9日", "12日")} - ${dayLabel.replace("9日", "14日")}に追加` })).toBeVisible();
  await page.getByPlaceholder("何を追加しますか？").fill("Range task");
  await page.getByRole("button", { name: "追加", exact: true }).click();
  await expect(page.getByText("Range task")).toHaveCount(3);

  await page.getByText("Range task").first().click();
  await page.getByRole("button", { name: "削除", exact: true }).click();
  await expect(page.getByText("Range task")).toHaveCount(0);

  await page.getByTitle("Settings").last().click();
  const settingsDialog = page.getByRole("dialog", { name: "Settings" });
  await expect(settingsDialog).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Settings categories" })).toBeVisible();
});

test("history card drag confirms a canonical reference and sends its identity", async ({ page }) => {
  const savedTurnRequests: { request: SavedTurnRequest }[] = [];
  const referenceResolutions: Response[] = [];
  const catalogRequests: Request[] = [];
  const referenceOrder: string[] = [];
  page.on("request", (request) => {
    if (canonicalRequestQuery(request, "api/chat/references", "GET") !== null) catalogRequests.push(request);
    if (canonicalRequestQuery(request, "api/chat/references/resolve", "POST") !== null) referenceOrder.push("resolve-request");
    if (chatRequestKind(request) === "startTurn") referenceOrder.push("saved-send-request");
  });
  page.on("response", (response) => {
    if (canonicalRequestQuery(response.request(), "api/chat/references/resolve", "POST") !== null) {
      referenceResolutions.push(response);
      referenceOrder.push("resolve-response");
    }
  });
  const listed = page.waitForResponse((response) =>
    canonicalRequestQuery(response.request(), "api/chat/references", "GET") !== null);
  await openDefaultspack(page, "/chat", {
    historyReferences: true,
    onSavedTurnRequest: (payload) => savedTurnRequests.push(payload),
  });

  const source = page.getByTestId("history-chat-card-c-smoke");
  const input = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await expect(source).toBeVisible();
  await expect(input).toBeEditable();
  await expect(input).toHaveValue("");
  expect(catalogRequests).toEqual([]);
  // The registered PointerSensor suppresses native HTML drag and needs a move
  // after crossing its 8px activation threshold. Observe real input and the
  // application's own candidate event; the observer never dispatches either.
  await page.evaluate((eventName) => {
    const source = document.querySelector('[data-testid="history-chat-card-c-smoke"]');
    const target = document.querySelector('[data-history-reference-drop-target="composer"]');
    if (!source || !target) throw new Error("history source or composer target not found");
    const state = { down: false, up: false, moves: 0, release: null as null | { x: number; y: number },
      drops: [] as Array<{ rawPayload: string; point: { x: number; y: number }; targetId: string }>, targetId: target.id };
    (window as Window & { __historyPointer?: typeof state }).__historyPointer = state;
    document.addEventListener("pointerdown", (event) => {
      if (event.target instanceof Node && source.contains(event.target)) state.down = event.isTrusted && event.button === 0;
    }, { capture: true });
    document.addEventListener("pointermove", (event) => {
      if (state.down && !state.up && event.isTrusted && event.buttons === 1) state.moves += 1;
    }, { capture: true });
    document.addEventListener("pointerup", (event) => {
      if (!state.down) return;
      const top = document.elementFromPoint(event.clientX, event.clientY);
      state.up = event.isTrusted && event.button === 0 && top !== null && target.contains(top);
      state.release = { x: event.clientX, y: event.clientY };
    }, { capture: true });
    window.addEventListener(eventName, (event) => {
      const detail = (event as CustomEvent).detail;
      state.drops.push(structuredClone(detail));
    });
  }, historyReferenceDropEvent);
  await source.scrollIntoViewIfNeeded();
  await input.scrollIntoViewIfNeeded();
  const start = await source.boundingBox();
  expect(start).not.toBeNull();
  const startPoint = { x: Math.floor(start!.x + start!.width / 2), y: Math.floor(start!.y + start!.height / 2) };
  await page.mouse.move(startPoint.x, startPoint.y);
  await page.mouse.down();
  try {
    // First activate inside the source, then let subsequent moves update delta.
    await page.mouse.move(startPoint.x + 12, startPoint.y);
    await expect(source).toHaveAttribute("aria-pressed", "true");
    const end = await input.boundingBox();
    expect(end).not.toBeNull();
    const endPoint = { x: Math.floor(end!.x + end!.width / 2), y: Math.floor(end!.y + end!.height / 2) };
    await page.mouse.move(endPoint.x, endPoint.y, { steps: 8 });
    await expect.poll(() => input.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const target = element.closest('[data-history-reference-drop-target="composer"]');
      const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return target !== null && top !== null && target.contains(top);
    })).toBe(true);
  } finally {
    await page.mouse.up();
  }
  const dragEvidence = await page.evaluate(() =>
    (window as Window & { __historyPointer?: {
      down: boolean; up: boolean; moves: number; release: { x: number; y: number } | null;
      drops: Array<{ rawPayload: string; point: { x: number; y: number }; targetId: string }>; targetId: string;
    } }).__historyPointer);
  await test.info().attach("history-pointer-transport", { body: JSON.stringify(dragEvidence), contentType: "application/json" });
  expect(dragEvidence?.down).toBe(true);
  expect(dragEvidence?.up).toBe(true);
  expect(dragEvidence!.moves).toBeGreaterThan(1);
  expect(dragEvidence?.drops).toHaveLength(1);
  expect(dragEvidence!.drops[0].targetId).toBe(dragEvidence!.targetId);
  expect(dragEvidence!.drops[0].point).toEqual(dragEvidence!.release);
  expect(JSON.parse(dragEvidence!.drops[0].rawPayload)).toEqual({
    schema: "io.tobkiri.history-reference.v1", kind: "chat", profile_id: "defaults",
    id: "c-smoke", label: "Preview Calendar Chat",
  });
  await expect.poll(() => referenceResolutions.length).toBe(1);
  const receipt = referenceResolutions[0];
  expect(receipt.status()).toBe(200);
  expect(frontendFixtureRequest(receipt.url(), receipt.request().method())).toEqual(frontendFixtureBinding("chatReferencesResolve"));
  expect(receipt.request().postDataJSON()).toEqual({ references: [{ kind: "chat", id: "c-smoke" }] });
  const snapshot = (await receipt.json()).data;
  expect(snapshot).toMatchObject({
    kind: "tobkiri.chat.reference.snapshot.v1", profile_id: "defaults",
    store_revision: 1, project_revision: 0, next_cursor: null, truncated: false,
    references: [{ kind: "chat", id: "c-smoke", label: "Preview Calendar Chat",
      conversation_ids: ["c-smoke"], member_count: 1, membership_complete: true }],
  });
  await expect(input).toHaveValue("@chat:c-smoke ");
  // Suggestions load lazily when the confirmed drop inserts @. Empty drafts
  // do not request this catalog; resolution itself is available before it.
  const catalog = await listed;
  expect(catalog.status()).toBe(200);
  expect(frontendFixtureRequest(catalog.url(), catalog.request().method())).toEqual(frontendFixtureBinding("chatReferencesList"));
  expect([...canonicalRequestQuery(catalog.request(), "api/chat/references", "GET")!]).toEqual([["limit", "100"]]);
  expect(await catalog.json()).toMatchObject({ status: "ok", success: true, data: { profile_id: "defaults", references: [{ kind: "chat", id: "c-smoke" }] } });
  expect(catalogRequests).toEqual([catalog.request()]);

  await expect(page.locator("[data-composer-inline-mentions]")).toContainText("@chat:c-smoke");
  expect(savedTurnRequests).toEqual([]);

  // Preserve the confirmed token's range; replacing the draft would revoke it.
  await input.press("End");
  await input.pressSequentially("Use this dropped chat as context.");
  await expect(input).toHaveValue("@chat:c-smoke Use this dropped chat as context.");
  expect(referenceResolutions).toHaveLength(1);
  expect(referenceOrder).toEqual(["resolve-request", "resolve-response"]);
  const revalidated = page.waitForResponse((response) =>
    canonicalRequestQuery(response.request(), "api/chat/references/resolve", "POST") !== null);
  const completed = page.waitForResponse((response) => chatRequestKind(response.request()) === "startTurn");
  await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
  const [revalidation, completion] = await Promise.all([revalidated, completed]);
  expect(revalidation).not.toBe(receipt);
  expect(referenceOrder).toEqual(["resolve-request", "resolve-response", "resolve-request", "resolve-response", "saved-send-request"]);
  expect(completion.status()).toBe(200);
  await expect.poll(() => savedTurnRequests.length).toBe(1);
  const request = savedTurnRequests[0].request;
  expect(request.conversation_id).toBe("c-smoke");
  expect(request.content).toBe("@chat:c-smoke Use this dropped chat as context.");
  expect(request.chat_references).toEqual([{ kind: "chat", profile_id: "defaults", id: "c-smoke" }]);
  // Selection and send each require their own successful owner read.
  expect(referenceResolutions).toHaveLength(2);
  for (const resolution of referenceResolutions) {
    expect(resolution.status()).toBe(200);
    expect(resolution.request().postDataJSON()).toEqual({ references: [{ kind: "chat", id: "c-smoke" }] });
    expect(frontendFixtureRequest(resolution.url(), resolution.request().method())).toEqual(frontendFixtureBinding("chatReferencesResolve"));
    expect(await resolution.json()).toMatchObject({ status: "ok", success: true, data: { profile_id: "defaults", references: [{ kind: "chat", id: "c-smoke", conversation_ids: ["c-smoke"], membership_complete: true }] } });
  }
  for (const legacy of ["message", "metadata", "dropped_widgets", "mentions", "tools", "params"]) {
    expect(request).not.toHaveProperty(legacy);
    expect(savedTurnRequests[0]).not.toHaveProperty(legacy);
  }
  expect(await completion.json()).toMatchObject({ status: "ok", success: true, data: { status: "completed", turn: { id: request.turn_id, conversation_id: "c-smoke", status: "completed" } } });
  await expect(page.getByText("Saved response accepted.", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(input).toBeEditable();
  await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
});

test("actual ChatApp ignores late tool progress after canonical completion without reviving an empty draft", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  const base = lateSavedProgressConversation;
  let fixture: ReturnType<typeof lateSavedProgressFixture> | undefined;
  let saved = false;
  let starts = 0;
  let progressReads = 0;
  let terminalReads = 0;
  let lateRequest: Request | undefined;
  const controls: string[] = [];
  let releaseStart!: () => void;
  let releaseLate!: () => void;
  const startGate = new Promise<void>((resolve) => { releaseStart = resolve; });
  const lateGate = new Promise<void>((resolve) => { releaseLate = resolve; });
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const binding = frontendFixtureRequest(request.url(), request.method());
    const kind = chatRequestKind(request);
    if (kind === "listConversations") {
      return fulfill(route, { conversations: [{ ...base, messages: [] }], total: 1, store_revision: 1 });
    }
    if (kind === "getConversation") {
      expect(requestConversationId(request)).toBe(base.id);
      return fulfill(route, fixture ? saved ? fixture.completedConversation : fixture.runningConversation : base);
    }
    if (kind === "startTurn") {
      starts += 1;
      expect(starts).toBe(1);
      const submitted = (request.postDataJSON() as { request: SavedTurnRequest }).request;
      expect(submitted).toMatchObject({ conversation_id: base.id, conversation_revision: 1, content: lateSavedProgressText.draft });
      fixture = lateSavedProgressFixture(submitted.turn_id, Date.now());
      await startGate;
      return fulfill(route, { status: "completed", turn: fixture.completedTurn });
    }
    if (kind === "turns") {
      expect([...canonicalRequestQuery(request, "api/chat/turns", "GET")!]).toEqual([["conversation_id", base.id]]);
      return fulfill(route, { turns: fixture ? [saved ? fixture.completedTurn : fixture.runningTurn] : [] });
    }
    if (kind === "turnEvents" || kind === "reconcile") {
      expect(fixture).toBeDefined();
      if (kind === "reconcile") {
        expect(request.postDataJSON()).toEqual({ turn_id: fixture!.runningTurn.id });
        return fulfill(route, { status: saved ? "completed" : "reconciliation_required",
          turn: saved ? fixture!.completedTurn : fixture!.runningTurn });
      }
      expect([...canonicalRequestQuery(request, "api/chat/turn/events", "GET")!].sort()).toEqual(
        Object.entries({ turn_id: fixture!.runningTurn.id, conversation_id: base.id }).sort(),
      );
      if (saved) terminalReads += 1;
      return fulfill(route, saved ? fixture!.completedSnapshot : fixture!.runningSnapshot);
    }
    if (binding?.contributionId === "defaults.conversations.turn.progress") {
      expect(binding).toMatchObject({ contractId: "tobkiri.resource.turn.progress.v1",
        operationId: "rumi_turn_runtime_pack.turn-progress-resource", method: "GET" });
      expect(fixture).toBeDefined();
      const query = canonicalRequestQuery(request, "api/chat/turn/progress", "GET");
      expect(query).not.toBeNull();
      progressReads += 1;
      expect(progressReads).toBeLessThanOrEqual(2);
      const initial = progressReads === 1;
      expect([...query!].sort()).toEqual(Object.entries({ turn_id: fixture!.runningTurn.id,
        conversation_id: base.id, cursor: initial ? "0" : "1",
        ...(initial ? {} : { progress_id: fixture!.initialPage.progress_id! }) }).sort());
      // Capture a valid running-owner response while the canonical turn is
      // still active, but deliver its tool event only after the UI settles.
      expect(saved).toBe(false);
      const response = initial ? fixture!.initialPage : fixture!.latePage;
      if (!initial) {
        lateRequest = request;
        await lateGate;
      }
      return fulfill(route, response);
    }
    if (kind === "stop" || kind === "steer") {
      controls.push(kind);
      return route.fulfill({ status: 409, json: { status: "error", error: "Unexpected control dispatch" } });
    }
    return route.fallback();
  });
  try {
    await page.goto(`/p/defaults/chat?chat=${base.id}`);
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await composer.fill(lateSavedProgressText.draft);
    await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
    await expect(page.getByText(lateSavedProgressText.provisional, { exact: true })).toBeVisible();
    await expect(page.locator('[data-message-id^="live-progress:"]')).toHaveCount(1);
    await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toBeEnabled();
    await expect.poll(() => progressReads).toBe(2);
    expect(terminalReads).toBe(0);
    const terminalResponse = page.waitForResponse(async (response) => {
      const query = canonicalRequestQuery(response.request(), "api/chat/turn/events", "GET");
      return query?.get("turn_id") === fixture!.runningTurn.id && query.get("conversation_id") === base.id
        && (await response.json()).data?.terminal?.status === "completed";
    });
    saved = true;
    releaseStart();
    const terminal = await terminalResponse;
    expect(terminal.status()).toBe(200);
    expect(await terminal.json()).toEqual(ok(fixture!.completedSnapshot));
    await terminal.finished();

    const expectSettled = async () => {
      await expect(page.getByText(lateSavedProgressText.answer, { exact: true })).toBeVisible();
      await expect(page.locator('[data-message-role="agent"]')).toHaveCount(1);
      await expect(page.locator('[data-message-id^="live-progress:"]')).toHaveCount(0);
      await expect(page.getByText(lateSavedProgressText.provisional, { exact: true })).toHaveCount(0);
      await expect(page.getByRole("region", { name: "ツール履歴", exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "生成を停止", exact: true })).toHaveCount(0);
      await expect(page.locator('[data-runtime-activity="running"]')).toHaveCount(0);
      await expect(composer).toBeEditable();
      await expect(composer).toHaveValue("");
      const send = page.getByRole("button", { name: "メッセージを送信", exact: true });
      await expect(send).toBeVisible();
      await expect(send).toBeDisabled();
      await expect(page).not.toHaveURL(/pending=1/);
      await expect.poll(() => page.evaluate((id) => {
        const stored = JSON.parse(localStorage.getItem("rumi-pending-chat-v2:defaults") ?? "null");
        return stored?.scopes?.[`sha256:${"a".repeat(64)}`]?.[id] ?? null;
      }, base.id)).toBeNull();
      await expect(page.getByText("レスポンス本文が空でした。stream が途中で閉じたか、thinking のみで終了した可能性があります。")).toHaveCount(0);
      await expect(page.getByText("tool 準備中")).toHaveCount(0);
    };
    await expectSettled();
    expect(terminalReads).toBeGreaterThan(0);
    await page.clock.install();
    expect(lateRequest).toBeDefined();
    expect(fixture!.latePage.expires_at_ms).toBeGreaterThan(await page.evaluate(() => Date.now()));
    const lateResponse = page.waitForResponse((response) => response.request() === lateRequest);
    releaseLate();
    const delivered = await lateResponse;
    expect(delivered.status()).toBe(200);
    expect(await delivered.json()).toEqual(ok(fixture!.latePage));
    await delivered.finished();
    // Cover three progress-poll periods (750ms) and a saved-turn poll (1500ms)
    // after actual delivery, so a briefly hidden revived request cannot pass.
    await page.clock.runFor(2_250);
    await expectSettled();
    expect(progressReads).toBe(2);
    expect(starts).toBe(1);
    expect(controls).toEqual([]);
  } finally {
    releaseStart();
    releaseLate();
  }
});

test("coding slash command toggles coding mode off again", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  await page.locator("textarea.rumi-composer-textarea").fill("/coding");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  await expect(page.getByRole("button", { name: "Coding widget", exact: true })).toBeVisible();

  await page.locator("textarea.rumi-composer-textarea").fill("/coding");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/chat(?:\?|$)/);
  await expect(page.getByRole("button", { name: "Coding widget", exact: true })).toBeHidden();
});

test("tool timeline shows streamed activity details", async ({ page }) => {
  await openDefaultspack(page);

  await expect(page.locator(".rumi-tool-activity")).toHaveCount(1);
  const toggle = page.getByRole("button", { name: /作業状況を開く:/ });
  await expect(toggle).toBeVisible();
  await expect(toggle).toContainText("詳細");

  await toggle.click();
  const expandedToggle = page.getByRole("button", { name: /作業状況を閉じる:/ });
  await expect(expandedToggle).toBeVisible();
  await expect(expandedToggle).toContainText("閉じる");
  const timeline = page.locator(".rumi-tool-activity");
  await expect(timeline).toBeVisible();
  await expect(timeline).toContainText("ファイル");
  await expect(timeline).toContainText("src");
  await expect(timeline).toContainText("Listed 2 files");
});

test("mocked coding cockpit renders MCP server state", async ({ page }) => {
  await openCodingWidget(page);

  await expect(page.locator(".coding-cockpit")).toBeVisible();
  const mcpServers = page.getByLabel("MCP servers");
  await expect(mcpServers).toContainText("Filesystem MCP");
  await expect(mcpServers).toContainText("approved");
});

test("mocked coding cockpit registers approves and connects an MCP server", async ({ page }) => {
  await openCodingWidget(page);

  await page.getByLabel("MCP server id").fill("contract_digest");
  await page.getByLabel("MCP command").fill("python");
  await page.getByLabel("MCP args").fill("digest_server.py");
  await page.getByTitle("Connect MCP server").click();

  const mcpServers = page.getByLabel("MCP servers");
  const approvals = page.getByLabel("Approval queue");
  await expect(approvals).toContainText("tool.mcp_connect");
  await expect(approvals).toContainText("contract_digest");
  await approvals.getByRole("button", { name: /許可|Approve/ }).click();
  await expect(mcpServers).toContainText("contract_digest");
  await expect(mcpServers).toContainText("approved");
  await expect(page.getByText("MCP connected: contract_digest (1 tools)")).toBeVisible();
});

test("coding approval queue refreshes immediately after terminal requests approval", async ({ page }) => {
  await openCodingWidget(page, { codingApprovalAfterTerminal: true });

  const terminal = page.getByRole("region", { name: "Terminal", exact: true });
  await terminal.locator("input").fill("echo qa-file");
  await terminal.getByTitle("Run command").click();

  await expect(terminal).toContainText("Approval required");
  const approvals = page.getByLabel("Approval queue");
  await expect(approvals).toContainText("terminal.exec");
  await expect(approvals.getByRole("button", { name: /許可|Approve/ })).toBeVisible();
  await expect(approvals.getByRole("button", { name: /拒否|Deny/ })).toBeVisible();
});

test("checkpoint create selects the new snapshot and approved restore settles successfully", async ({ page }) => {
  await openCodingWidget(page, { codingApprovalAfterRestore: true });

  const checkpoints = page.getByRole("region", { name: "Checkpoints", exact: true });
  await expect(checkpoints.locator("select")).toHaveValue("checkpoint-1");
  await checkpoints.getByTitle("Create checkpoint").click();
  await expect(checkpoints.locator("select")).toHaveValue("checkpoint-2");
  await expect(checkpoints).toContainText("Created checkpoint-2");
  await checkpoints.getByTitle("Review checkpoint restore").click();
  await checkpoints.getByRole("button", { name: "Confirm restore" }).click();

  await expect(checkpoints).toContainText("Approval required");
  const approvals = page.getByLabel("Approval queue");
  await expect(approvals).toContainText("file.restore");
  await approvals.getByRole("button", { name: /許可|Approve/ }).click();
  await expect(checkpoints).toContainText("Restored checkpoint-2");
  await expect(checkpoints).not.toContainText("Approval required");
});


test("actual ChatApp retains missing owner root despite earlier completed reply and explicit local recovery never replays", async ({ page }) => {
  const storeId = `sha256:${"a".repeat(64)}`;
  await installDefaultspackApiMocks(page, { applicationChat: true, initialPendingStorage: { scopes: {
    [storeId]: { "c-smoke": { conversationId: "c-smoke", operationId: "missing-third-turn",
      savedTurn: true, ownerTurnObserved: true, startedAt: 100, status: "照合中", toolNames: [],
      submittedText: "使えるtool教えて" } },
  }, archive: [] } });
  let reads = 0;
  let writes = 0;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = requestTarget(new URL(request.url()));
    if (target === "/api/chat/turns") { reads += 1; return route.fulfill({ json: ok({ turns: [] }) }); }
    if (target.startsWith("/api/chat/turn") && request.method() === "POST") {
      writes += 1; return route.fulfill({ status: 500, json: { error: "unexpected mutation" } });
    }
    return route.fallback();
  });
  await page.goto("/p/defaults/chat?chat=c-smoke&pending=1");
  await expect.poll(() => reads).toBeGreaterThanOrEqual(3);
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await expect(composer).toBeEnabled();
  await composer.fill("A preserved new draft");
  await composer.press("Enter");
  expect(writes).toBe(0);
  await page.getByRole("button", { name: "記録を残して待機を解除", exact: true }).click();
  await expect(composer).toHaveValue("A preserved new draft");
  await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeEnabled();
  await page.getByText("未確認の送信記録（1件）", { exact: true }).click();
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("rumi-pending-chat-v2:defaults")!).archive[0].request.operationId)).toBe("missing-third-turn");
  await expect(page.getByText("使えるtool教えて", { exact: true })).toBeVisible();
  expect(writes).toBe(0);
});

test("actual ChatApp exact pre-execution refusal restores draft without another start", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  let starts = 0;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = requestTarget(new URL(request.url()));
    if (target === "/api/chat/turn" && request.method() === "POST") {
      starts += 1;
      return route.fulfill({ status: 409, json: { success: false, error: "execution did not start", data: {
        host_operation_api_version: "io.tobkiri.host.operation.v1", state: "error",
        code: "SAVED_TURN_NOT_STARTED", retryable: false, write_set: [],
        request_id: request.headers()["x-tobkiri-request-id"],
      } } });
    }
    if (target === "/api/chat/turns") return route.fulfill({ json: ok({ turns: [] }) });
    return route.fallback();
  });
  await page.goto("/p/defaults/chat?chat=c-smoke");
  const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
  await composer.fill("A refused draft to preserve");
  await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
  await expect.poll(() => starts).toBe(1);
  await expect(composer).toHaveValue("A refused draft to preserve");
  await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "記録を残して待機を解除", exact: true })).toHaveCount(0);
  expect(starts).toBe(1);
});


test("actual ChatApp switches saved stores without locking foreign pending or applying late old completion", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  let storeId = `sha256:${"a".repeat(64)}`;
  let healthReads = 0;
  await page.route("**/health", (route) => { healthReads += 1; return route.fulfill({ json: ok({ status: "ok", saved_turn_store_id: storeId }) }); });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let starts = 0;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = requestTarget(new URL(request.url()));
    if (target === "/api/chat/turn" && request.method() === "POST") {
      starts += 1;
      const input = request.postDataJSON().request;
      await gate;
      return route.fulfill({ json: ok({ status: "completed", turn: {
        id: input.turn_id, conversation_id: input.conversation_id, status: "completed", revision: 2,
      } }) });
    }
    if (target === "/api/chat/turns") return route.fulfill({ json: ok({ turns: [] }) });
    return route.fallback();
  });
  try {
    await page.goto("/p/defaults/chat?chat=c-smoke");
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await composer.fill("Store A original request");
    await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
    await expect.poll(() => starts).toBe(1);
    storeId = `sha256:${"b".repeat(64)}`;
    const beforeHealth = healthReads;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(() => healthReads).toBeGreaterThan(beforeHealth);
    await expect(composer).toBeEnabled();
    await composer.fill("Store B untouched draft");
    await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeEnabled();
    await expect(page.getByText("未確認の送信記録（1件）", { exact: true })).toBeVisible();
    const oldResponse = page.waitForResponse((response) => requestTarget(new URL(response.url())) === "/api/chat/turn");
    release(); await oldResponse;
    await expect(composer).toHaveValue("Store B untouched draft");
    await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeEnabled();
    expect(starts).toBe(1);
  } finally { release(); }
});

test("actual ChatApp hung unregistered start becomes explicit recovery without replay and rejects its late completion", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let starts = 0;
  let reads = 0;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const target = requestTarget(new URL(request.url()));
    if (target === "/api/chat/turn" && request.method() === "POST") {
      starts += 1;
      const input = request.postDataJSON().request;
      await gate;
      return route.fulfill({ json: ok({ status: "completed", turn: {
        id: input.turn_id, conversation_id: input.conversation_id, status: "completed", revision: 2,
      } }) });
    }
    if (target === "/api/chat/turns") { reads += 1; return route.fulfill({ json: ok({ turns: [] }) }); }
    return route.fallback();
  });
  try {
    await page.goto("/p/defaults/chat?chat=c-smoke");
    await page.clock.install();
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    await composer.fill("Hung original draft");
    await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
    await expect.poll(() => starts).toBe(1);
    await expect(composer).toBeDisabled();
    await expect(page.getByRole("button", { name: "記録を残して待機を解除", exact: true })).toHaveCount(0);
    await page.clock.runFor(17_000);
    await expect.poll(() => reads).toBeGreaterThan(0);
    await expect(composer).toBeEnabled();
    await composer.fill("Newer local draft");
    await composer.press("Enter");
    expect(starts).toBe(1);
    await page.getByRole("button", { name: "記録を残して待機を解除", exact: true }).click();
    const response = page.waitForResponse((result) => requestTarget(new URL(result.url())) === "/api/chat/turn");
    release(); await response;
    await expect(composer).toHaveValue("Newer local draft");
    await expect(page.getByRole("button", { name: "メッセージを送信", exact: true })).toBeEnabled();
    expect(starts).toBe(1);
  } finally { release(); }
});


test("actual ChatApp sends the selected finite tool and displays authenticated live start/result before canonical saved logs", async ({ page }) => {
  await installDefaultspackApiMocks(page, { applicationChat: true,
    initialSelectedToolIds: ["calculator"], initialSettingsValues: { tools: { default_mode: "manual" } } });
  const base = { ...smokeConversation(), conversation_revision: 1, messages: [], tags: [], metadata: {} };
  const userId = `message:${"a".repeat(64)}`;
  const assistantId = `message:${"b".repeat(64)}`;
  const stageTool = "c".repeat(64);
  const stageAi = "d".repeat(64);
  const inputDigest = `sha256:${"a".repeat(64)}`;
  const requestId = "saved-turn.live-tool-fixture";
  let operationId = "";
  let starts = 0;
  let progressReads = 0;
  let toolComplete = false;
  let aiStage = false;
  let saved = false;
  let submitted: SavedTurnRequest | null = null;
  let release!: () => void;
  const startGate = new Promise<void>((resolve) => { release = resolve; });
  const result = JSON.stringify({ status: "success", result: 3, error: null });
  const reference = () => ({ conversation_id: base.id, conversation_revision: 3, user_message_id: userId,
    assistant_message_id: assistantId, outcome_digest: `sha256:${"e".repeat(64)}` });
  const turn = () => ({ id: operationId, conversation_id: base.id, request_id: requestId, input_digest: inputDigest,
    conversation_revision: 1, status: saved ? "completed" : "running", revision: saved ? 3 : 2,
    ...(saved ? { result_reference: reference() } : {}) });
  const user = () => ({ id: userId, conversation_id: base.id, role: "user", created_at: Date.now(),
    content: [{ type: "text", text: "Calculate 1+2 using @Calculator" }], metadata: { turn_id: operationId } });
  const conversation = () => operationId ? { ...base, conversation_revision: saved ? 3 : 2,
    current_node_id: saved ? assistantId : userId, messages: [user(), ...(saved ? [{
      id: assistantId, conversation_id: base.id, role: "assistant", created_at: Date.now(), finish_reason: "stop",
      content: [{ type: "text", text: "Canonical calculator answer 3" }], metadata: { turn_id: operationId },
      tool_logs: [{ tool_name: "calculator", tool_call_id: "call-calculator", arguments: { expression: "1+2" }, result }],
    }] : [])] } : base;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const target = requestTarget(url);
    if (target === "/api/tools/catalog") return route.fulfill({ json: ok({ services: [], count: 1, tools: [{
      tool_id: "calculator", service_id: "local", service_label: "Local", name: "Calculator",
      action_class: "read", connection_status: "connected",
    }] }) });
    if (target === "/api/chat/conversation" && request.method() === "GET") return route.fulfill({ json: ok(conversation()) });
    if (target === "/api/chat/conversations" && request.method() === "GET") return route.fulfill({ json: ok({ conversations: [{ ...base, messages: [] }], total: 1, store_revision: 1 }) });
    if (target === "/api/chat/turn" && request.method() === "POST") {
      starts += 1; submitted = request.postDataJSON().request; operationId = submitted!.turn_id;
      await startGate;
      return route.fulfill({ json: ok({ status: "completed", turn: turn() }) });
    }
    if (target === "/api/chat/turns") return route.fulfill({ json: ok({ turns: operationId ? [turn()] : [] }) });
    if (target === "/api/chat/turn/reconcile") return route.fulfill({ json: ok({ status: saved ? "completed" : "reconciliation_required", turn: turn() }) });
    if (target === "/api/chat/turn/events") return route.fulfill({ json: ok({
      turn_id: operationId, operation_id: operationId, conversation_id: base.id, request_id: requestId,
      turn_revision: turn().revision, status: turn().status, turn: turn(), events: [],
      terminal: saved ? { turn_id: operationId, operation_id: operationId, conversation_id: base.id,
        request_id: requestId, turn_revision: 3, status: "completed", result_reference: reference() } : null,
    }) });
    if (target === "/api/chat/turn/progress") {
      progressReads += 1;
      const operation = decodeURIComponent(url.pathname.split("/api/contracts/defaultspack/")[1] ?? url.pathname);
      const query = new URL(operation.slice(operation.indexOf(" ") + 1), url.origin).searchParams;
      const cursor = Number(query.get("cursor") ?? 0);
      const stage = aiStage ? stageAi : stageTool;
      const matchingStage = query.get("progress_id") === stage;
      const after = matchingStage ? cursor : 0;
      const events = aiStage ? [{ cursor: 1, event: { type: "text_delta", delta: "Provisional calculator answer" } }] : [
        { cursor: 1, event: { type: "tool_started", tool_id: "calculator", tool_call_id: "call-calculator", arguments: { expression: "1+2" } } },
        ...(toolComplete ? [{ cursor: 2, event: { type: "tool_completed", tool_id: "calculator", tool_call_id: "call-calculator", status: "success", content: result } }] : []),
      ];
      const fresh = events.filter((item) => item.cursor > after);
      return route.fulfill({ json: ok({ version: "tobkiri.turn-progress.v1", progress_id: stage, provisional: true,
        binding: { turn_id: operationId, conversation_id: base.id, parent_id: userId, request_id: requestId,
          conversation_revision: 2, input_digest: inputDigest, ai_input_digest: `sha256:${stage}` },
        events: fresh, cursor: fresh.at(-1)?.cursor ?? after, provider_complete: !aiStage && toolComplete,
        expires_at_ms: expiry, canonical_turn_status: "running",
      }) });
    }
    return route.fallback();
  });
  const expiry = Date.now() + 110_000;
  try {
    await page.goto(`/p/defaults/chat?chat=${base.id}`);
    const composer = page.getByRole("combobox", { name: "Tobkiriにメッセージを送信" });
    // Replacing the draft removes its old mention authority. Confirm the tool
    // again after filling so only the visible selected reference is submitted.
    await composer.fill("Calculate 1+2 using @cal");
    await page.getByRole("option").filter({ has: page.getByText("@Calculator", { exact: true }) }).click();
    await expect(composer).toHaveValue("Calculate 1+2 using @Calculator ");
    await page.getByRole("button", { name: "メッセージを送信", exact: true }).click();
    await expect.poll(() => starts).toBe(1);
    expect(submitted?.content).toBe("Calculate 1+2 using @Calculator");
    expect(submitted?.tool_selection).toMatchObject({ mode: "manual" });
    expect(submitted?.tool_selection?.include).toEqual([{ kind: "tool", id: "calculator" }]);
    await expect.poll(() => progressReads).toBeGreaterThan(0);
    const history = page.getByRole("region", { name: "ツール履歴", exact: true });
    await expect(history).toBeVisible();
    await expect(history).toContainText("作業中");
    toolComplete = true;
    await expect(history).not.toContainText("作業中");
    aiStage = true;
    await expect(page.getByText("Provisional calculator answer", { exact: true })).toBeVisible();
    await expect(history).toHaveCount(1);
    saved = true; release();
    await expect(page.getByText("Canonical calculator answer 3", { exact: true })).toBeVisible();
    await expect(page.getByText("Provisional calculator answer", { exact: true })).toHaveCount(0);
    await expect(history).toHaveCount(1);
    await history.getByRole("button", { name: /作業状況を開く/ }).click();
    await expect(history).toContainText("3");
    expect(starts).toBe(1);
  } finally { release(); }
});


for (const tabsEnabled of [true, false]) {
  test(`Kanban and Desktops sidebar routes stay synchronized with tabs enabled=${tabsEnabled}`, async ({ page }) => {
    await installDefaultspackApiMocks(page, { applicationChat: true,
      initialSettingsValues: { general: { workspace_tabs_enabled: tabsEnabled } } });
    const requests: string[] = [];
    const desktops = frontendFixtureBinding("desktopsList");
    const providers = frontendFixtureBinding("runtimeProviders");
    const templates = frontendFixtureBinding("sandboxTemplates");
    const bootstrap = frontendFixtureBinding("kanbanCreate");
    await page.route("**/api/contracts/defaultspack/**", async (route) => {
      const target = frontendFixtureRequest(route.request().url(), route.request().method());
      if (target?.contractId === "tobkiri.resource.kanban.v1" || target?.contractId === "tobkiri.action.kanban.v1") {
        requests.push(target.contributionId);
        return route.fulfill({ status: 404, json: { status: "error", error: { code: "CONTRACT_OPERATION_UNKNOWN", message: "Kanban endpoint unavailable" } } });
      }
      if (matchesFrontendFixtureBinding(target, desktops)) {
        requests.push(desktops.contributionId);
        return fulfill(route, { desktops: [] });
      }
      if (matchesFrontendFixtureBinding(target, providers)) return fulfill(route, { providers: [] });
      if (matchesFrontendFixtureBinding(target, templates)) return fulfill(route, { templates: [] });
      return route.fallback();
    });
    await page.goto("/p/defaults/chat");
    await page.getByRole("button", { name: "Kanban", exact: true }).click();
    await expect(page).toHaveURL(/\/p\/defaults\/kanban$/);
    await expect(page.getByText("Kanban is unavailable", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Desktops", exact: true }).click();
    await expect(page).toHaveURL(/\/p\/defaults\/desktops$/);
    await expect(page.getByRole("region", { name: "Desktops workspace" })).toBeVisible();
    await expect(page.getByText("Kanban is unavailable", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Kanban", exact: true }).click();
    await page.getByRole("button", { name: "Desktops", exact: true }).click();
    await expect(page).toHaveURL(/\/p\/defaults\/desktops$/);
    if (tabsEnabled) {
      await expect(page.getByRole("tab", { name: "Desktops", exact: true })).toHaveAttribute("aria-selected", "true");
      await expect(page.getByRole("tab", { name: "Desktops", exact: true })).toHaveCount(1);
    }
    await page.goBack();
    await expect(page).toHaveURL(/\/p\/defaults\/kanban$/);
    await expect(page.getByText("Kanban is unavailable", { exact: true })).toBeVisible();
    await page.goForward();
    await expect(page).toHaveURL(/\/p\/defaults\/desktops$/);
    await expect(page.getByRole("region", { name: "Desktops workspace" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("region", { name: "Desktops workspace" })).toBeVisible();
    expect(requests).toContain(desktops.contributionId);
    expect(requests).not.toContain(bootstrap.contributionId);
  });
}

for (const status of [401, 404, 503]) {
  test(`Kanban distinguishes endpoint ${status} from empty data and recovers on Retry`, async ({ page }) => {
    await installDefaultspackApiMocks(page, { applicationChat: true });
    const list = frontendFixtureBinding("kanbanList");
    const bootstrap = frontendFixtureBinding("kanbanCreate");
    let available = false;
    let bootstrapCalls = 0;
    const board = { revision: 1,
      board: { board_id: "local-board", title: "All Tobkiri Runs", scope_type: "global", scope_id: "default" },
      columns: [{ column_id: "todo", board_id: "local-board", title: "Backlog", position: 0 }], cards: [], events: [] };
    await page.route("**/api/contracts/defaultspack/**", async (route) => {
      const target = frontendFixtureRequest(route.request().url(), route.request().method());
      if (matchesFrontendFixtureBinding(target, list)) return available
        ? fulfill(route, { revision: 0, boards: [] })
        : route.fulfill({ status, json: { status: "error", error: { message: `Kanban HTTP ${status}` } } });
      if (matchesFrontendFixtureBinding(target, bootstrap)) {
        bootstrapCalls += 1;
        expect(route.request().postDataJSON()).toMatchObject({ expected_revision: 0, scope_type: "global", scope_id: "default" });
        return fulfill(route, board);
      }
      return route.fallback();
    });
    await page.goto("/p/defaults/kanban");
    await expect(page.getByText("Kanban is unavailable", { exact: true })).toBeVisible();
    expect(bootstrapCalls).toBe(0);
    available = true;
    await page.getByRole("button", { name: "Retry", exact: true }).click();
    await expect(page.getByRole("listitem", { name: "Backlog, 0 cards" })).toBeVisible();
    expect(bootstrapCalls).toBe(1);
    await page.reload();
    await expect(page.getByRole("listitem", { name: "Backlog, 0 cards" })).toBeVisible();
  });
}
