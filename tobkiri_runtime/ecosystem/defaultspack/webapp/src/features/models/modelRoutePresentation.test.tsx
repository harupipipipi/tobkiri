import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { RegisteredProviderConnection } from "../../lib/api";
import { modelRouteConnectionLabel } from "./modelRoutePresentation";
import { CustomModelControls } from "./ModelRouteSetup";

const connection = (id: string, name = "main", provider = "openrouter"): RegisteredProviderConnection => ({
  provider_instance_id: id, display_name: name, catalog_provider_id: provider,
  credential_status: "configured", health_status: "unverified", reachability: "unknown", observed_at: null,
});

test("API labels identify the provider without changing opaque connection IDs", () => {
  const api = connection("opaque.saved-1");
  assert.equal(modelRouteConnectionLabel(api), "openrouter-main");
  assert.equal(api.provider_instance_id, "opaque.saved-1");
  assert.equal(modelRouteConnectionLabel(connection("id", "OpenRouter-main")), "OpenRouter-main");
  assert.equal(modelRouteConnectionLabel(connection("id", "openrouter/main")), "openrouter/main");
  assert.equal(modelRouteConnectionLabel(connection("id", "OpenRouter")), "OpenRouter");
  assert.equal(modelRouteConnectionLabel(connection("id", "OpenRouterPro")), "openrouter-OpenRouterPro");
});

test("same-named APIs have distinct labels even for the same provider", () => {
  const apis = [connection("first"), connection("second"), connection("third", "main", "openai")];
  assert.deepEqual(apis.map((api) => modelRouteConnectionLabel(api, apis)), [
    "openrouter-main (first)", "openrouter-main (second)", "openai-main",
  ]);
  assert.equal(modelRouteConnectionLabel(connection("opaque", "private", "")), "private");
});

test("standard hides the custom editor but retains selected custom model information", () => {
  const props = { manual: true, manualModel: true, hasCatalog: true, model: "custom/vendor:v2",
    onManualChange: () => {}, onChange: () => {} };
  const standard = renderToStaticMarkup(createElement(CustomModelControls, { ...props, displayMode: "standard" }));
  const advanced = renderToStaticMarkup(createElement(CustomModelControls, { ...props, displayMode: "advanced" }));
  assert.match(standard, /custom\/vendor:v2/);
  assert.match(standard, /Advanced/);
  assert.doesNotMatch(standard, /<input|<details|カスタムモデル・一覧にないモデル/);
  assert.match(advanced, /カスタムモデル・一覧にないモデル/);
  assert.match(advanced, /value="custom\/vendor:v2"/);
  assert.match(advanced, /checked/);
  assert.equal(renderToStaticMarkup(createElement(CustomModelControls, { ...props, manual: false, manualModel: false, displayMode: "standard" })), "");
});
