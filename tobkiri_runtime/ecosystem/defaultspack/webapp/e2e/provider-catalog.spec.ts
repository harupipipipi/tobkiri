import { expect, test } from "@playwright/test";
import { providerCatalogModels } from "../src/lib/providerCatalog";
import { MODEL_ACCESS_VERSION } from "../src/features/apiKeys/modelAccessPolicy";
import { frontendHostFixtureCatalog } from "../test-support/frontendHostFixture";
import {
  frontendFixtureBinding, frontendFixtureRequest, matchesFrontendFixtureBinding,
} from "../test-support/frontendContractFixture";

const setupBindings = {
  connections: frontendFixtureBinding("providerConnections"),
  configure: frontendFixtureBinding("providerConfigure"),
  approval: frontendFixtureBinding("interactiveApprovalGet"),
  profilesList: frontendFixtureBinding("modelProfilesList"),
  profilesSave: frontendFixtureBinding("modelProfilesSave"),
  search: frontendFixtureBinding("modelSearch"),
  accessRead: frontendFixtureBinding("modelAccessRead"),
  accessCatalog: frontendFixtureBinding("modelAccessCatalog"),
};

test("OpenRouter setup saves a catalog selection allowed for the captured Profile and exact connection", async ({ page }) => {
  page.on("pageerror", (error) => console.error("Setup page error:", error.message));
  page.on("console", (message) => { if (message.type() === "error") console.error(message.text()); });
  const writes: Record<string, unknown>[] = [];
  const accessReads: Record<string, unknown>[] = [];
  const catalog = frontendHostFixtureCatalog(true);
  const models = providerCatalogModels("openrouter").filter((model) => ["chat", "reasoning"].includes(model.type));
  // Catalog completeness is independent of the picker's bounded visible page.
  expect(models.length).toBeGreaterThan(400);
  const chosenModel = models.filter((model) => model.model_id.endsWith(":free")).at(-1)!;
  expect(chosenModel).toBeTruthy();
  const modelId = chosenModel.model_id;
  let connection: Record<string, unknown> | null = null;
  let configuration: Record<string, string> | null = null;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const request = route.request();
    const binding = frontendFixtureRequest(request.url(), request.method());
    const matches = (key: keyof typeof setupBindings) => matchesFrontendFixtureBinding(binding, setupBindings[key]);
    const body = route.request().postDataJSON() ?? {};
    let data: unknown;
    if (matches("connections")) {
      data = { revision: connection ? 1 : 0, providers: connection ? [connection] : [] };
    } else if (matches("approval")) {
      data = { request_id: "approval-fixture", state: "approved" };
    } else if (matches("configure")) {
      writes.push(body);
      if (body.phase === "prepare") configuration = body.request;
      if (body.phase === "resume") connection = {
        provider_instance_id: `provider.${configuration!.connection_name}`,
        display_name: configuration!.display_name,
        catalog_provider_id: configuration!.catalog_provider_id,
        enabled: true, credential_status: "configured", health_status: "unverified",
        reachability: "unknown", observed_at: null,
      };
      data = {
        effect_id: "effect-fixture", approval_request_id: "approval-fixture",
        state: body.phase === "resume" ? "succeeded" : "approval_pending",
      };
    } else if (matches("profilesList") || matches("profilesSave")) {
      if (matches("profilesSave")) {
        writes.push(body);
        data = { profiles: [{
          profile_id: body.model_profile_id, model_id: body.model_id,
          provider_id: body.provider_instance_id, display_name: body.display_name,
          route_configured: true,
        }], count: 1 };
      } else data = { profiles: [], count: 0, registry_revision: 0 };
    } else if (matches("accessRead") || matches("accessCatalog")) {
      expect(connection).not.toBeNull();
      expect(body).toEqual({ profile_id: catalog.profile_id, provider_instance_id: connection!.provider_instance_id });
      if (matches("accessRead")) {
        accessReads.push(body);
        data = { ...body, registry_revision: 1, native_capability: null,
          model_access: { version: MODEL_ACCESS_VERSION, mode: "explicit", model_ids: [modelId] } };
      } else data = { ...body, status: "live", models: [{ model_id: modelId, display_name: chosenModel.display_name }] };
    } else if (matches("search")) {
      expect(connection).not.toBeNull();
      expect(body).toMatchObject({ provider_id: "openrouter", connection_id: connection!.provider_instance_id });
      // The shipped advisory catalog supplies choices. The remote empty page
      // preserves the exact requested connection fence and grants no access.
      data = { models: [], total: 0, has_more: false, filters_applied: body };
    } else throw new Error(`Unexpected setup endpoint: ${request.method()} ${request.url()}`);
    await route.fulfill({ json: { status: "ok", data } });
  });
  await page.route("**/__provider-catalog-test", (route) => route.fulfill({
    contentType: "text/html",
    body: `<!doctype html><html lang="ja"><head><meta charset="utf-8"></head>
      <body style="background:#09090b;color:#fafafa;padding:32px"><div id="root"></div>
      <script type="module">
      import React from "/node_modules/.vite/deps/react.js";
      import ReactDOM from "/node_modules/.vite/deps/react-dom_client.js";
      import RefreshRuntime from "/@react-refresh";
      import "/src/index.css";
      RefreshRuntime.injectIntoGlobalHook(window);
      window.$RefreshReg$ = () => {};
      window.$RefreshSig$ = () => (type) => type;
      window.__vite_plugin_react_preamble_installed__ = true;
      const { BuiltinApiKeySetupRenderer } = await import("/src/renderers/settings/renderers/apiKeySetupField.tsx");
      const { VerifiedFrontendHostProvider } = await import("/src/host/VerifiedFrontendHostContext.tsx");
      const capturedCatalog = ${JSON.stringify(catalog)};
      const unexpectedCapability = async () => { throw new Error("Unexpected fixture capability invocation"); };
      ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(VerifiedFrontendHostProvider, {
        value: { catalog: capturedCatalog, activePlanHash: capturedCatalog.plan_hash,
          capabilities: { invokeAction: unexpectedCapability, readDataSource: unexpectedCapability } }
      }, React.createElement(BuiltinApiKeySetupRenderer, {
        sectionId: "api_keys", field: { id: "api_key_setup", type: "api_key_setup", label: "APIキー", provider_id: "openrouter", provider_scope: "llm" },
        value: null, sectionValues: {}, onChange: () => {}
      })));
      </script></body></html>`,
  }));
  await page.goto("/__provider-catalog-test");
  await expect(page.getByLabel("APIの名前")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Provider HTTPS base URL")).toHaveCount(0);
  await page.getByLabel("APIの名前").fill("仕事用 API");
  await page.getByLabel("APIキー", { exact: true }).fill("fixture-provider-key");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const picker = page.getByRole("combobox", { name: "モデルを検索", exact: true });
  await expect(picker).toBeVisible();
  await expect(page.getByLabel("使用するAPI", { exact: true })).toHaveValue(String(connection!.provider_instance_id));
  await expect(page.getByLabel("使用するAPI", { exact: true }).locator("option:checked")).toContainText("仕事用 API");
  await expect(page.getByLabel("APIキー", { exact: true })).toHaveValue("");
  await expect.poll(() => accessReads.length).toBeGreaterThan(0);
  // The explicit captured policy allows this model only, despite 400+ choices.
  const results = page.getByRole("listbox", { name: "検索結果", exact: true });
  await expect(results.getByRole("option")).toHaveCount(1);
  await picker.fill(modelId);
  await results.getByRole("option").filter({ hasText: chosenModel.display_name }).click();
  expect(writes.filter((write) => write.model_profile_id)).toEqual([]);
  await page.getByRole("button", { name: "このモデルを使う" }).click();
  await expect(page.getByRole("status").filter({ hasText: "モデル設定を保存しました" })).toBeVisible();
  const prepared = writes.find((write) => write.phase === "prepare")!.request as Record<string, string>;
  expect(prepared).toMatchObject({
    catalog_provider_id: "openrouter", display_name: "仕事用 API",
    endpoint: "https://openrouter.ai/api/v1", protocol: "openai-compatible",
  });
  const saved = writes.find((write) => write.model_profile_id)!;
  expect(saved.model_id).toBe(modelId);
  expect(saved.provider_instance_id).toBe(connection!.provider_instance_id);
  expect(saved.model_profile_id).toMatch(/^model\.[a-f0-9]{64}$/);
  expect(saved.provider_registry_revision).toBe(1);
  expect(saved.expected_revision).toBe(0);
  expect(accessReads).toContainEqual({ profile_id: catalog.profile_id, provider_instance_id: connection!.provider_instance_id });
  expect(JSON.stringify(saved)).not.toContain("fixture-provider-key");
});
