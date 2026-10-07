import assert from "node:assert/strict";
import test from "node:test";
import type { ModelProfile } from "../../lib/api";
import { modelSearchItemToModelSelectOption, type ModelSelectOption } from "./modelSelect";
import { modelProfileConnectionId, savedModelProfileForOption } from "./modelSelectionIdentity";

const profile: ModelProfile = {
  profile_id: "openrouter/google/gemma-4", display_name: "Gemma saved elsewhere",
  provider_id: "provider.openai.work", model_id: "gpt-4.1",
};
const option: ModelSelectOption = {
  value: profile.profile_id, label: profile.display_name,
  registered_profile_id: profile.profile_id, provider_id: profile.provider_id,
  model_id: profile.model_id, connection_id: "provider.openai.work",
};

test("a public catalogue ID collision cannot switch a saved route", () => {
  const publicChoice = modelSearchItemToModelSelectOption({
    profile_id: profile.profile_id, display_name: "Gemma 4",
    provider_id: "openrouter", model_id: "google/gemma-4",
  });
  assert.equal(savedModelProfileForOption([profile], publicChoice), null);
  assert.equal(publicChoice.registered_profile_id, undefined);
});

test("an exact local route retains its opaque saved identity", () => {
  assert.equal(savedModelProfileForOption([profile], option), profile);
  assert.equal(modelProfileConnectionId(profile), "provider.openai.work");
});

test("stale provider, model and connection bindings fail closed", () => {
  for (const changed of [
    { provider_id: "openrouter" }, { model_id: "google/gemma-4" },
    { connection_id: "provider.openai.personal" }, { registered_profile_id: "other" },
  ]) assert.equal(savedModelProfileForOption([profile], { ...option, ...changed }), null);
  assert.equal(savedModelProfileForOption([], option), null);
});

test("connection metadata is retained without treating a model publisher as its API", () => {
  const routed = { ...profile, provider_id: "openrouter", model_id: "google/gemma-4",
    metadata: { connection_id: "opaque.saved.openrouter" } };
  assert.equal(modelProfileConnectionId(routed), "opaque.saved.openrouter");
  assert.equal(savedModelProfileForOption([routed], { ...option,
    provider_id: "openrouter", model_id: "google/gemma-4", connection_id: "opaque.saved.openrouter" }), routed);
});

test("registry provider_connection_id alias preserves the exact opaque connection", () => {
  const routed = { ...profile, provider_id: "openrouter", model_id: "google/gemma-4",
    metadata: { provider_connection_id: "opaque.registry.connection" } };
  assert.equal(modelProfileConnectionId(routed), "opaque.registry.connection");
  assert.equal(savedModelProfileForOption([routed], { ...option,
    provider_id: "openrouter", model_id: "google/gemma-4", connection_id: "opaque.registry.connection" }), routed);
});

test("connection aliases have stable priority and skip empty or invalid values", () => {
  assert.equal(modelProfileConnectionId({ ...profile, metadata: {
    connection_id: "first", provider_instance_id: "second", provider_connection_id: "third",
  } }), "first");
  assert.equal(modelProfileConnectionId({ ...profile, metadata: {
    connection_id: " ", provider_instance_id: "second", provider_connection_id: "third",
  } }), "second");
  assert.equal(modelProfileConnectionId({ ...profile, metadata: {
    connection_id: 42, provider_instance_id: "", provider_connection_id: "third",
  } }), "third");
});
