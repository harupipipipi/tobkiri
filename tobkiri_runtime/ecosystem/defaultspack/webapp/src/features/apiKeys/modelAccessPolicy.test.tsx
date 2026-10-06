import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MODEL_ACCESS_VERSION, OPENROUTER_FILTER_REVISION, modelAllowed, normalizeModelAccess, toggleAllowedModel, type ModelAccessPolicy } from "./modelAccessPolicy";
import { createProviderModelAccessResources, type ModelAccessScope } from "./resources/providerModelAccessResources";
import { ProviderModelAccessSettings } from "./ProviderModelAccessSettings";
const scope = { profile_id: "default", provider_instance_id: "opaque.saved.connection" };
const policy = (mode: "all" | "explicit" = "explicit", model_ids: string[] = []): ModelAccessPolicy => ({ version: MODEL_ACCESS_VERSION, mode, model_ids });
const snapshot = (binding = scope, value = policy(), revision = 1) => ({ ...binding, model_access: value, native_capability: null, registry_revision: revision });
test("all includes future catalog IDs, explicit empty denies and multiple IDs stay exact", () => {
  assert.equal(modelAllowed(policy("all"), "future/new-v99"), true);
  assert.equal(modelAllowed(policy(), "future/new-v99"), false);
  assert.equal(modelAllowed(policy("explicit", ["a/model", "b/model"]), "b/model"), true);
  assert.equal(modelAllowed(policy("explicit", ["a/model"]), "other/model"), false);
  assert.throws(() => normalizeModelAccess(policy("all", ["a/model"]), null));
});
test("selection persists across catalog pages and deleted models; empty is never all", () => {
  const selected = toggleAllowedModel(policy("explicit", ["removed/model"]), "new/model");
  assert.deepEqual(selected.model_ids, ["new/model", "removed/model"]);
  const empty = toggleAllowedModel(toggleAllowedModel(selected, "new/model"), "removed/model");
  assert.deepEqual(empty, policy());
});
test("unknown policy fields and key material are rejected", () => {
  assert.throws(() => normalizeModelAccess({ ...policy(), key_value: "blocked" }, null));
  assert.throws(() => normalizeModelAccess({ ...policy(), version: "old" }, null));
});
test("official native fields require the exact verified capability", () => {
  const value = { ...policy("all"), native_filters: { revision: OPENROUTER_FILTER_REVISION, discovery: { output_modalities: ["text"] }, routing: { only: ["xiaomi"], max_price: { completion: .87 } } } };
  assert.throws(() => normalizeModelAccess(value, null));
  assert.throws(() => normalizeModelAccess(value, "old"));
  assert.deepEqual(normalizeModelAccess(value, OPENROUTER_FILTER_REVISION), value);
  assert.throws(() => normalizeModelAccess({ ...value, native_filters: { ...value.native_filters, routing: { invented: true } } }, OPENROUTER_FILTER_REVISION));
});
test("unsafe native prices and conflicting provider selections are rejected", () => {
  for (const routing of [{ max_price: { prompt: NaN } }, { max_price: { prompt: -1 } }, { only: ["xiaomi"], ignore: ["xiaomi"] }]) assert.throws(() => normalizeModelAccess({ ...policy("all"), native_filters: { revision: OPENROUTER_FILTER_REVISION, discovery: {}, routing } }, OPENROUTER_FILTER_REVISION));
});
test("resource persists exact profile/connection policy without any key values", async () => {
  let state = snapshot();
  const writes: unknown[] = [];
  const resource = createProviderModelAccessResources({
    async getModelAccess(binding) { assert.deepEqual(binding, scope); return state; },
    async setModelAccess(input) { writes.push(input); state = snapshot(scope, input.model_access, input.expected_revision + 1); return state; },
    async getModelAccessCatalog() { return { ...scope, models: [], status: "live" }; },
  });
  const before = await resource.load(scope);
  const after = await resource.save(before, policy("explicit", ["a/model", "b/model"]));
  assert.deepEqual(after, await resource.load(scope));
  assert.deepEqual(writes, [{ ...scope, expected_revision: 1, model_access: policy("explicit", ["a/model", "b/model"]) }]);
});
test("cross-profile and cross-connection responses are rejected", async () => {
  for (const binding of [{ ...scope, profile_id: "work" }, { ...scope, provider_instance_id: "other.connection" }]) {
    const resource = createProviderModelAccessResources({ async getModelAccess() { return snapshot(binding); }, async setModelAccess() { return snapshot(binding); }, async getModelAccessCatalog() { return { ...scope, models: [], status: "live" }; } });
    await assert.rejects(resource.load(scope));
  }
});
test("a returned key field and mismatched save confirmation are rejected", async () => {
  const resource = createProviderModelAccessResources({ async getModelAccess() { return { ...snapshot(), api_key: "blocked" }; }, async setModelAccess() { return snapshot(scope, policy("all"), 2); }, async getModelAccessCatalog() { return { ...scope, models: [], status: "live" }; } });
  await assert.rejects(resource.load(scope));
  await assert.rejects(resource.save(snapshot(), policy()));
});
test("read-only rendering starts pending without probes or key inputs", () => {
  let calls = 0;
  const resource = createProviderModelAccessResources({ async getModelAccess() { ++calls; return snapshot(); }, async setModelAccess() { ++calls; return snapshot(); }, async getModelAccessCatalog() { ++calls; return { ...scope, models: [], status: "live" }; } });
  const html = renderToStaticMarkup(createElement(ProviderModelAccessSettings, { scope, resources: resource }));
  assert.match(html, /このAPI接続で使えるモデル/);
  assert.match(html, /設定を読み込み中/);
  assert.doesNotMatch(html, /password|textarea|api_key|key_value/);
  assert.equal(calls, 0);
});

test("catalog rejects unknown fields, malformed models and foreign scope", async () => {
  const valid = { ...scope, models: [{ model_id: "vendor/model", display_name: "Model" }], status: "live" };
  const invalid: unknown[] = [
    null, [], {}, { ...valid, credential_handle: "blocked" },
    { ...valid, endpoint: "https://private.invalid" },
    { ...valid, profile_id: "foreign" }, { ...valid, provider_instance_id: "other" },
    { ...valid, profile_id: "" }, { ...valid, status: "pending" },
    { ...valid, status: null }, { ...valid, models: null },
    { ...valid, models: [null] }, { ...valid, models: [[]] },
    { ...valid, models: [{ model_id: "vendor/model", display_name: "Model", api_key: "blocked" }] },
    { ...valid, models: [{ model_id: "", display_name: "Model" }] },
    { ...valid, models: [{ model_id: "vendor/model", display_name: " " }] },
    { ...valid, models: [{ model_id: "vendor/model" }] },
  ];
  for (const response of invalid) {
    const resource = createProviderModelAccessResources({
      async getModelAccess() { return snapshot(); },
      async setModelAccess() { return snapshot(); },
      async getModelAccessCatalog() { return response; },
    });
    await assert.rejects(resource.catalog(scope));
  }
});

test("catalog rebuilds a pure public projection and preserves valid empty states", async () => {
  const original = { ...scope, models: [{ model_id: "vendor/model", display_name: "Model" }], status: "live" };
  let response: unknown = original;
  const resource = createProviderModelAccessResources({
    async getModelAccess() { return snapshot(); },
    async setModelAccess() { return snapshot(); },
    async getModelAccessCatalog() { return response; },
  });
  const projected = await resource.catalog(scope);
  assert.deepEqual(projected, original);
  assert.notEqual(projected, original);
  assert.notEqual(projected.models, original.models);
  assert.notEqual(projected.models[0], original.models[0]);
  for (const status of ["live", "unavailable", "unsupported"]) {
    response = { ...scope, models: [], status };
    assert.deepEqual(await resource.catalog(scope), response);
  }
});
