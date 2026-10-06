import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import type { ModelProfile } from "../lib/api";
import { DEFAULT_MODEL_SELECTOR_SCHEMA } from "../features/models/modelSelectorSchema";
import { registeredDropdownKeyboardBlocked, registeredDropdownProfiles, registeredDropdownSelection } from "./ComposerRegisteredModelDropdown";

const personal: ModelProfile = { profile_id: "personal", display_name: "Personal", provider_id: "openai", model_id: "same-model", metadata: { connection_id: "personal-key" }, route_configured: true };
const work: ModelProfile = { ...personal, profile_id: "work", display_name: "Work", metadata: { connection_id: "work-key" } };

test("registered selection preserves same-model connections and rejects stale bindings", () => {
  assert.equal(registeredDropdownSelection([personal, work], work), work);
  assert.equal(registeredDropdownSelection([personal, work], { ...work, metadata: { connection_id: "personal-key" } }), null);
  assert.equal(registeredDropdownSelection([personal, work], { ...work, profile_id: "catalog-only" }), null);
});

test("generation and unavailable state block selection", () => {
  assert.equal(registeredDropdownSelection([work], work, true), null);
  const unavailable = { ...work, availability: { available: false } };
  assert.equal(registeredDropdownSelection([unavailable], unavailable), null);
  const offline = { ...work, availability: { status: "unavailable" } };
  assert.equal(registeredDropdownSelection([offline], offline), null);
  assert.equal(registeredDropdownSelection([offline], work), null);
});

test("provider queries and connection queries search registered routes without deduplicating models", () => {
  assert.deepEqual(registeredDropdownProfiles([personal, work], DEFAULT_MODEL_SELECTOR_SCHEMA, "@openai same-model", null), [personal, work]);
  assert.deepEqual(registeredDropdownProfiles([personal, work], DEFAULT_MODEL_SELECTOR_SCHEMA, "work-key", null), [work]);
  assert.deepEqual(registeredDropdownProfiles([personal, work], DEFAULT_MODEL_SELECTOR_SCHEMA, "@anthropic", null), []);
  assert.deepEqual(registeredDropdownProfiles([personal, work], DEFAULT_MODEL_SELECTOR_SCHEMA, "", work), [work, personal]);
});

test("schema hidden, unavailable and configured filters remain effective", () => {
  const schema = { ...DEFAULT_MODEL_SELECTOR_SCHEMA, filters: { ...DEFAULT_MODEL_SELECTOR_SCHEMA.filters, exclude_model_ids: ["personal"], hide_unavailable: true, hide_unconfigured: true } };
  const unconfigured = { ...personal, profile_id: "unconfigured", route_configured: false };
  const unavailable = { ...personal, profile_id: "unavailable", availability: { available: false } };
  assert.deepEqual(registeredDropdownProfiles([personal, work, unconfigured, unavailable], schema, "", null), [work]);
});

test("the standalone dropdown has no catalogue/search-template dependency and retains IME handling", () => {
  const source = readFileSync(new URL("./ComposerRegisteredModelDropdown.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /ModelSearchPicker|SharedSearchTemplate|searchModels|useModelCatalogSearch/);
  assert.match(source, /event\.nativeEvent\.isComposing/);
  assert.match(source, /onCompositionStart/);
});

test("IME composition and legacy composition keycodes block dropdown keyboard actions", () => {
  assert.equal(registeredDropdownKeyboardBlocked(true, false, 13), true);
  assert.equal(registeredDropdownKeyboardBlocked(false, true, 13), true);
  assert.equal(registeredDropdownKeyboardBlocked(false, false, 229), true);
  assert.equal(registeredDropdownKeyboardBlocked(false, false, 13), false);
});


test("duplicate saved IDs cannot resurrect schema-excluded routes or confuse selection", () => {
  const excluded = { ...personal, profile_id: "duplicate", provider_id: "excluded-provider" };
  const kept = { ...work, profile_id: "duplicate" };
  const schema = { ...DEFAULT_MODEL_SELECTOR_SCHEMA, filters: { ...DEFAULT_MODEL_SELECTOR_SCHEMA.filters, exclude_provider_ids: ["excluded-provider"] } };
  assert.deepEqual(registeredDropdownProfiles([excluded, kept], schema, "", excluded), [kept]);
  assert.equal(registeredDropdownSelection([excluded, kept], kept), kept);
  assert.deepEqual(registeredDropdownProfiles([excluded, kept], DEFAULT_MODEL_SELECTOR_SCHEMA, "", kept), [kept, excluded]);
});
