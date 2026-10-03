import { expect, test, type Page, type Route } from "@playwright/test";
import {
  DEFAULTSPACK_CONTRACT_ENDPOINT,
  defaultspackContractRoute,
  defaultspackContractUrl,
} from "../src/lib/api";

const contractRoutes = {
  providerRead: {
    method: "GET",
    path: defaultspackContractUrl(defaultspackContractRoute("api/connections/status")),
  },
  modelRead: {
    method: "GET",
    path: defaultspackContractUrl(defaultspackContractRoute("api/ai/profiles")),
  },
  modelCreate: {
    method: "POST",
    path: defaultspackContractUrl(defaultspackContractRoute("api/ai/profiles"), "POST"),
  },
};
type ContractOperation = keyof typeof contractRoutes;

const providerId = "connection/openai:main";
const provider = {
  provider_instance_id: providerId, display_name: "OpenAI main", enabled: true,
  credential_status: "configured", health_status: "unverified",
  reachability: "unknown", observed_at: null,
};

function contractOperation(route: Route): ContractOperation {
  const request = route.request();
  const pathname = new URL(request.url()).pathname;
  for (const [operation, identity] of Object.entries(contractRoutes)) {
    if (request.method() === identity.method && pathname === identity.path) {
      return operation as ContractOperation;
    }
  }
  throw new Error(`Unexpected fixture contract request: ${request.method()} ${pathname}`);
}

async function reply(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status, contentType: "application/json",
    body: JSON.stringify(status === 200 ? { status: "ok", data } : {
      status: "error", error: "model route provider connection changed",
    }),
  });
}

async function mount(page: Page, handle: (route: Route, operation: ContractOperation) => Promise<void>) {
  await page.route(`**${DEFAULTSPACK_CONTRACT_ENDPOINT}**`, (route) => handle(route, contractOperation(route)));
  await page.route("**/model-route-test", (route) => route.fulfill({
    contentType: "text/html",
    body: `<div id="model-route-root"></div><script type="module">
      import RefreshRuntime from "/@react-refresh";
      RefreshRuntime.injectIntoGlobalHook(window);
      window.$RefreshReg$ = () => {};
      window.$RefreshSig$ = () => (type) => type;
      window.__vite_plugin_react_preamble_installed__ = true;
      const { mountModelRouteSetup } = await import("/e2e/model-route-setup.fixture.tsx");
      mountModelRouteSetup();
    </script>`,
  }));
  await page.goto("/model-route-test");
  await expect(page.getByPlaceholder("daily")).toBeVisible();
}

async function fill(page: Page) {
  await page.getByPlaceholder("daily").fill("daily");
  await page.getByPlaceholder("ProviderのモデルID").fill("provider/model");
  await page.getByLabel("Provider connection ID").selectOption(providerId);
}

async function changed(page: Page) {
  await page.evaluate(() => window.dispatchEvent(new Event("tobkiri-provider-connections-changed")));
}

// Let the fulfilled fetch and React updates settle before asserting that an
// older response did not mutate the latest snapshot or selection.
async function settledReply(page: Page, route: Route, data: unknown, status = 200) {
  const response = page.waitForResponse((value) => value.request() === route.request());
  await reply(route, data, status);
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

test("overlapping mount and event reads cannot overwrite the newest provider revision", async ({ page }) => {
  const reads: Route[] = [];
  const writes: Record<string, unknown>[] = [];
  await mount(page, async (route, operation) => {
    if (operation === "providerRead") { reads.push(route); return; }
    if (operation === "modelCreate") {
      writes.push(route.request().postDataJSON());
      await reply(route, { profiles: [{
        profile_id: "daily", model_id: "provider/model", provider_id: providerId,
        display_name: "daily",
      }], count: 1 });
    } else { await reply(route, { profiles: [], count: 0, registry_revision: 0 }); }
  });
  await expect.poll(() => reads.length).toBe(1);
  await changed(page);
  await expect.poll(() => reads.length).toBe(2);
  await settledReply(page, reads[1], { revision: 4, providers: [provider] });
  await fill(page);
  await settledReply(page, reads[0], { revision: 1, providers: [] });
  await expect(page.getByLabel("Provider connection ID")).toHaveValue(providerId);
  await page.getByRole("button", { name: "モデルルートを保存", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].provider_registry_revision).toBe(4);
});

test("an older failed read cannot erase a newer provider snapshot", async ({ page }) => {
  const reads: Route[] = [];
  await mount(page, async (route) => { reads.push(route); });
  await expect.poll(() => reads.length).toBe(1);
  await changed(page);
  await expect.poll(() => reads.length).toBe(2);
  await settledReply(page, reads[1], { revision: 4, providers: [provider] });
  await fill(page);
  await settledReply(page, reads[0], null, 503);
  await expect(page.getByLabel("Provider connection ID")).toHaveValue(providerId);
  await expect(page.getByRole("button", { name: "モデルルートを保存", exact: true })).toBeEnabled();
  await expect(page.locator('[data-error-notice="model-route-provider-connections"]')).toHaveCount(0);
});

for (const recovery of ["current", "removed", "disabled", "failed-refresh", "lost-response"] as const) {
  test(`stale save refreshes providers and requires confirmed replay (${recovery})`, async ({ page }) => {
    let reads = 0;
    const writes: Record<string, unknown>[] = [];
    await mount(page, async (route, operation) => {
      if (operation === "providerRead") {
        reads += 1;
        if (reads === 2 && recovery === "failed-refresh") { await reply(route, null, 503); return; }
        await reply(route, {
          revision: reads === 1 ? 1 : 5,
          providers: reads === 1 || reads > 2 || recovery === "current" || recovery === "lost-response" ? [provider]
            : recovery === "disabled" ? [{ ...provider, enabled: false }] : [],
        });
      } else if (operation === "modelCreate") {
        writes.push(route.request().postDataJSON());
        if (writes.length === 1 && recovery === "lost-response") {
          // The owner accepted the route, but its reply never reached the UI.
          await route.abort("failed");
          return;
        }
        await reply(route, { profiles: [{
          profile_id: "daily", model_id: "provider/model", provider_id: providerId,
          display_name: "daily",
        }], count: 1 }, writes.length === 1 ? 409 : 200);
      } else {
        const saved = recovery === "lost-response" && writes.length > 0;
        await reply(route, {
          profiles: saved ? [{
            profile_id: "daily", model_id: "provider/model", provider_id: providerId,
            display_name: "daily",
          }] : [], count: saved ? 1 : 0, registry_revision: saved ? 1 : 0,
        });
      }
    });
    await expect(page.getByLabel("Provider connection ID")).toBeEnabled();
    await fill(page);
    await page.getByRole("button", { name: "モデルルートを保存", exact: true }).click();
    await expect.poll(() => reads).toBe(2);
    await expect(page.getByPlaceholder("daily")).toBeEnabled();
    await expect(page.getByPlaceholder("daily")).toHaveValue("daily");
    await expect(page.getByPlaceholder("ProviderのモデルID")).toHaveValue("provider/model");
    const retry = page.getByRole("button", { name: "確認したモデルルートを再送", exact: true });
    const confirmation = page.getByRole("checkbox");
    await expect(retry).toBeDisabled();
    await expect(confirmation).not.toBeChecked();
    expect(writes).toHaveLength(1);
    if (recovery === "removed" || recovery === "disabled") {
      await expect(page.getByLabel("Provider connection ID")).toHaveValue("");
      await expect(page.getByText(/削除または無効化/)).toBeVisible();
      await expect(confirmation).toBeDisabled();
      return;
    }
    if (recovery === "failed-refresh") {
      await expect(confirmation).toBeDisabled();
      await page.getByRole("button", { name: "Provider接続一覧を更新", exact: true }).click();
      await expect.poll(() => reads).toBe(3);
    }
    await expect(page.getByLabel("Provider connection ID")).toHaveValue(providerId);
    await confirmation.check();
    await expect(retry).toBeEnabled();
    expect(writes).toHaveLength(1);
    await retry.click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes.map((write) => write.provider_registry_revision)).toEqual([1, 5]);
    expect(writes[1]).toMatchObject({ model_profile_id: "daily", model_id: "provider/model" });
    if (recovery === "lost-response") expect(writes[1].expected_revision).toBe(1);
    await expect(page.getByText(/モデル設定を保存しました/)).toBeVisible();
  });
}

test("an event refresh invalidates retry confirmation and supersedes save-recovery reads", async ({ page }) => {
  const reads: Route[] = [];
  let writes = 0;
  await mount(page, async (route, operation) => {
    if (operation === "providerRead") { reads.push(route); return; }
    if (operation === "modelCreate") { writes += 1; await reply(route, null, 409); }
    else { await reply(route, { profiles: [], count: 0, registry_revision: 0 }); }
  });
  await expect.poll(() => reads.length).toBe(1);
  await settledReply(page, reads[0], { revision: 1, providers: [provider] });
  await fill(page);
  await page.getByRole("button", { name: "モデルルートを保存", exact: true }).click();
  await expect.poll(() => reads.length).toBe(2);
  await changed(page);
  await expect.poll(() => reads.length).toBe(3);
  await settledReply(page, reads[2], { revision: 5, providers: [provider] });
  await settledReply(page, reads[1], { revision: 2, providers: [] });
  await expect(page.getByLabel("Provider connection ID")).toHaveValue(providerId);
  await page.getByRole("checkbox").check();
  await changed(page);
  await expect.poll(() => reads.length).toBe(4);
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(page.getByRole("button", { name: "確認したモデルルートを再送", exact: true })).toBeDisabled();
  await settledReply(page, reads[3], { revision: 6, providers: [provider] });
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  expect(writes).toBe(1);
});

test("repeated submit events cannot start two saves before the busy render", async ({ page }) => {
  const writes: Route[] = [];
  let modelReads = 0;
  await mount(page, async (route, operation) => {
    if (operation === "providerRead") {
      await reply(route, { revision: 1, providers: [provider] });
    } else if (operation === "modelCreate") { writes.push(route); }
    else { modelReads += 1; await reply(route, { profiles: [], count: 0, registry_revision: 0 }); }
  });
  await expect(page.getByLabel("Provider connection ID")).toBeEnabled();
  await fill(page);
  const save = page.getByRole("button", { name: "モデルルートを保存", exact: true });
  await save.evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect.poll(() => writes.length).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "保存結果を確認中", exact: true })).toBeDisabled();
  expect(modelReads).toBe(1);
  expect(writes).toHaveLength(1);
  await reply(writes[0], { profiles: [{
    profile_id: "daily", model_id: "provider/model", provider_id: providerId,
    display_name: "daily",
  }], count: 1 });
  await expect(page.getByText(/モデル設定を保存しました/)).toBeVisible();
});
