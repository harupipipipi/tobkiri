import { expect, test } from "@playwright/test";

test("OpenRouter setup needs only an API name and key, then a catalog selection", async ({ page }) => {
  page.on("pageerror", (error) => console.error("Setup page error:", error.message));
  page.on("console", (message) => { if (message.type() === "error") console.error(message.text()); });
  const writes: Record<string, unknown>[] = [];
  let connection: Record<string, unknown> | null = null;
  let configuration: Record<string, string> | null = null;
  await page.route("**/api/contracts/defaultspack/**", async (route) => {
    const path = decodeURIComponent(new URL(route.request().url()).pathname);
    const body = route.request().postDataJSON() ?? {};
    let data: unknown;
    if (path.includes("api/connections/status")) {
      data = { revision: connection ? 1 : 0, providers: connection ? [connection] : [] };
    } else if (path.includes("interactive-approval")) {
      data = { request_id: "approval-fixture", state: "approved" };
    } else if (path.includes("api/ai/provider-key")) {
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
    } else if (path.includes("api/ai/profiles")) {
      if (route.request().method() === "POST") {
        writes.push(body);
        data = { profiles: [{
          profile_id: body.model_profile_id, model_id: body.model_id,
          provider_id: body.provider_instance_id, display_name: body.display_name,
          route_configured: true,
        }], count: 1 };
      } else data = { profiles: [], count: 0, registry_revision: 0 };
    } else throw new Error(`Unexpected setup endpoint: ${path}`);
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
      ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(BuiltinApiKeySetupRenderer, {
        sectionId: "api_keys", field: { id: "api_key_setup", type: "api_key_setup", label: "APIキー", provider_id: "openrouter", provider_scope: "llm" },
        value: null, sectionValues: {}, onChange: () => {}
      }));
      </script></body></html>`,
  }));
  await page.goto("/__provider-catalog-test");
  await expect(page.getByLabel("APIの名前")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Provider HTTPS base URL")).toHaveCount(0);
  await page.getByLabel("APIの名前").fill("仕事用 API");
  await page.getByLabel("APIキー", { exact: true }).fill("fixture-provider-key");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const picker = page.getByLabel("モデル一覧", { exact: true });
  await expect(picker).toBeVisible();
  expect(await picker.locator("option").count()).toBeGreaterThan(400);
  await expect(page.getByLabel("Provider connection ID")).toContainText("仕事用 API");
  await expect(page.getByLabel("APIキー", { exact: true })).toHaveValue("");
  const modelId = await picker.locator('option[value$=":free"]').last().getAttribute("value");
  expect(modelId).toBeTruthy();
  await picker.selectOption(modelId!);
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
  expect(JSON.stringify(saved)).not.toContain("fixture-provider-key");
});
