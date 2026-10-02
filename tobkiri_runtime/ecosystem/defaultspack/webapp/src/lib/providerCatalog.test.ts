import assert from "node:assert/strict";
import test from "node:test";
import {
  catalogModelProfileId, connectionCatalogProviderId,
  providerCatalogModels, providerConnectionName,
} from "./providerCatalog";

test("complete OpenRouter choices retain slash IDs and free variants", () => {
  const models = providerCatalogModels("openrouter");
  assert.ok(models.length > 400);
  assert.equal(new Set(models.map((model) => model.model_id)).size, models.length);
  assert.ok(models.some((model) => model.model_id.endsWith(":free")));
  assert.ok(models.every((model) => model.model_id.includes("/")));
  assert.deepEqual(providerCatalogModels("unknown"), []);
});

test("catalog identity can be supplied independently of the opaque connection ID", () => {
  const connection = {
    provider_instance_id: "opaque.connection-7", display_name: "仕事用",
    credential_status: "configured" as const, health_status: "unverified" as const,
    reachability: "unknown" as const, observed_at: null,
  };
  assert.equal(connectionCatalogProviderId({ ...connection, catalog_provider_id: "openrouter" }), "openrouter");
  assert.equal(connectionCatalogProviderId(connection), "");
  assert.equal(connectionCatalogProviderId({ ...connection, provider_instance_id: "provider.openrouter.main" }), "openrouter");
});

test("connection names support Unicode without including keys or unsafe path characters", async () => {
  assert.equal(await providerConnectionName("openrouter", "main"), "openrouter.main");
  const japanese = await providerConnectionName("openrouter", "仕事用 API");
  assert.match(japanese, /^openrouter\.key-[a-f0-9]{24}$/);
  assert.equal(await providerConnectionName("openrouter", "仕事用 API"), japanese);
  assert.notEqual(await providerConnectionName("openrouter", "個人用 API"), japanese);
});

test("route IDs bind the raw model to the exact saved API connection", async () => {
  const first = await catalogModelProfileId("provider.openrouter.work", "vendor/model:free");
  assert.match(first, /^model\.[a-f0-9]{64}$/);
  assert.equal(await catalogModelProfileId("provider.openrouter.work", "vendor/model:free"), first);
  assert.notEqual(await catalogModelProfileId("provider.openrouter.personal", "vendor/model:free"), first);
  assert.notEqual(await catalogModelProfileId("provider.openrouter.work", "vendor/model"), first);
});
