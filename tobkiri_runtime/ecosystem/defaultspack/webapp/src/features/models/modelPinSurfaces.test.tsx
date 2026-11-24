import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ModelProfile, ModelSearchItem } from "../../lib/api";
import { VerifiedFrontendHostProvider, type VerifiedFrontendHost } from "../../host/VerifiedFrontendHostContext";
import { registeredDropdownProfiles } from "../../renderers/ComposerRegisteredModelDropdown";
import { ModelPinButton } from "./ModelPinButton";
import { modelPinIdentityForOption, modelPinIdentityForProfile, modelPinIdentityForSearchItem } from "./modelPinIdentity";
import { createModelPinStore, orderPinnedModels } from "./modelPins";
import { buildVisibleModelOptions, mergeRegisteredModelProfileOptions, modelSearchItemToModelSelectOption } from "./modelSelect";
import { DEFAULT_MODEL_SELECTOR_SCHEMA } from "./modelSelectorSchema";
import { useModelPins } from "./useModelPins";

const profile: ModelProfile = {
  profile_id: "saved-personal", display_name: "Same display name", provider_id: "openrouter",
  model_id: "google/native-model:free", route_configured: true,
  metadata: { connection_id: "personal-connection" },
};
const searchItem: ModelSearchItem = {
  profile_id: "catalog/alias", display_name: "Same display name", provider_id: "openrouter",
  model_id: profile.model_id, connection_id: "personal-connection",
};

test("a pin from Spotlight reaches Settings and Composer through their real adapters without changing selection IDs", () => {
  const store = createModelPinStore();
  const settings = mergeRegisteredModelProfileOptions([], [profile])[0];
  const spotlight = modelSearchItemToModelSelectOption(searchItem);
  const identity = modelPinIdentityForSearchItem(searchItem)!;
  store.toggle("verified-profile", identity);
  for (const other of [modelPinIdentityForOption(settings), modelPinIdentityForOption(spotlight), modelPinIdentityForProfile(profile)]) {
    assert.ok(store.getSnapshot("verified-profile").includes(other!));
  }
  assert.equal(settings.value, profile.profile_id);
  assert.equal(spotlight.value, searchItem.profile_id);
  assert.equal(profile.profile_id, "saved-personal");
  assert.equal(store.getSnapshot("another-runtime-profile").length, 0);
  store.toggle("verified-profile", modelPinIdentityForOption(settings)!);
  assert.deepEqual(store.getSnapshot("verified-profile"), []);
});

test("same-name models on another connection, provider or native namespace never inherit a pin", () => {
  const store = createModelPinStore();
  store.toggle("scope", modelPinIdentityForSearchItem(searchItem)!);
  const variants = [
    { ...searchItem, connection_id: "work-connection" },
    { ...searchItem, connection_id: undefined, provider_id: "google" },
    { ...searchItem, connection_id: undefined },
    { ...searchItem, model_id: "native-model:free" },
  ];
  for (const variant of variants) {
    assert.equal(store.getSnapshot("scope").includes(modelPinIdentityForSearchItem(variant)!), false);
    assert.equal(store.getSnapshot("scope").includes(modelPinIdentityForOption(modelSearchItemToModelSelectOption(variant))!), false);
  }
});

test("Composer filters registered routes before pin ranking and never synthesizes absent pinned rows", () => {
  const work = { ...profile, profile_id: "saved-work", metadata: { connection_id: "work-connection" } };
  const hidden = { ...profile, profile_id: "excluded", model_id: "hidden-native" };
  const schema = { ...DEFAULT_MODEL_SELECTOR_SCHEMA, filters: { ...DEFAULT_MODEL_SELECTOR_SCHEMA.filters, exclude_model_ids: ["excluded"] } };
  const pins = [modelPinIdentityForProfile(hidden)!, modelPinIdentityForProfile(work)!, '["connection","absent","model"]'];
  const filtered = registeredDropdownProfiles([profile, hidden, work], schema, "work-connection", null);
  assert.deepEqual(orderPinnedModels(filtered, pins, modelPinIdentityForProfile), [work]);
  const allEligible = registeredDropdownProfiles([profile, hidden, work], schema, "", null);
  assert.deepEqual(orderPinnedModels(allEligible, pins, modelPinIdentityForProfile), [work, profile]);
});

test("Settings search excludes nonmatching pins before ranking loaded options", () => {
  const work = { ...profile, profile_id: "saved-work", display_name: "Work only", model_id: "work-native", metadata: { connection_id: "work-connection" } };
  const options = mergeRegisteredModelProfileOptions([], [profile, work]);
  const pins = [modelPinIdentityForProfile(profile)!, modelPinIdentityForProfile(work)!, '["catalog","openai","unloaded"]'];
  const filtered = buildVisibleModelOptions({ options, query: "Work only" });
  const ordered = orderPinnedModels(filtered, pins, modelPinIdentityForOption);
  assert.deepEqual(ordered.map(option => option.value), [work.profile_id]);
  assert.equal(ordered[0], options.find(option => option.value === work.profile_id));
});

test("local pin changes persist identities and notify subscribers without invoking host capabilities or network", () => {
  const writes: string[] = [];
  const store = createModelPinStore({ getItem: () => null, setItem: (_key, value) => { writes.push(value); } });
  let notifications = 0;
  const unsubscribe = store.subscribe("scope", () => { notifications += 1; });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("Pin preference must not access network"); };
  try {
    store.toggle("scope", modelPinIdentityForSearchItem(searchItem)!);
    store.toggle("scope", modelPinIdentityForProfile(profile)!);
  } finally { globalThis.fetch = originalFetch; unsubscribe(); }
  assert.equal(notifications, 2);
  assert.deepEqual(JSON.parse(writes[0]).pins, [modelPinIdentityForProfile(profile)]);
  assert.deepEqual(JSON.parse(writes[1]).pins, []);
  assert.equal(writes.some(value => /display_name|route_configured|metadata|profile_id/.test(value)), false);
});

const verifiedHost = (): VerifiedFrontendHost => ({
  catalog: { profile_id: "verified-scope", profile_revision: "r1", activation_id: "activation", plan_hash: "plan", catalog_hash: "catalog" } as VerifiedFrontendHost["catalog"],
  activePlanHash: "plan", capabilities: {} as VerifiedFrontendHost["capabilities"],
});
function PinScopeProbe() {
  const pins = useModelPins();
  return createElement(ModelPinButton, { label: "Native model", pinned: pins.isPinned(modelPinIdentityForProfile(profile)), disabled: !pins.enabled, onToggle: () => pins.toggle(modelPinIdentityForProfile(profile)) });
}

test("pin controls fail closed without capture, mismatched plan or incomplete verified identity", () => {
  assert.match(renderToStaticMarkup(createElement(PinScopeProbe)), /disabled=""/);
  const good = verifiedHost();
  const render = (host: VerifiedFrontendHost) => renderToStaticMarkup(createElement(VerifiedFrontendHostProvider, { value: host, children: createElement(PinScopeProbe) }));
  assert.doesNotMatch(render(good), /disabled=""/);
  for (const host of [
    { ...good, activePlanHash: "stale-plan" },
    { ...good, catalog: { ...good.catalog, profile_id: "" } },
    { ...good, catalog: { ...good.catalog, activation_id: "" } },
    { ...good, catalog: { ...good.catalog, catalog_hash: "" } },
    { ...good, catalog: { ...good.catalog, profile_revision: "" } },
  ]) assert.match(render(host), /disabled=""/);
});

test("canonical Composer remains registered-only and its pin action is a sibling of selection", () => {
  const source = readFileSync(new URL("../../renderers/ComposerRegisteredModelDropdown.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /ModelSearchPicker|SharedSearchTemplate|searchModels|useModelCatalogSearch/);
  assert.match(source, /<\/button><ModelPinButton/);
  const pinAction = source.slice(source.indexOf("<ModelPinButton"), source.indexOf("/>", source.indexOf("<ModelPinButton")));
  assert.doesNotMatch(pinAction, /onSelect|select\(profile\)|onClose|submit|invoke/);
  assert.match(pinAction, /onToggle=\{\(\) => togglePin\(profile\)\}/);
  const helper = source.slice(source.indexOf("const togglePin ="), source.indexOf("\n  return <>", source.indexOf("const togglePin =")));
  assert.match(helper, /modelPins\.toggle\(identity\)/);
  assert.doesNotMatch(helper, /onSelect|onClose|submit|invoke|searchModels|fetch\(/);
});

test("Spotlight ranking and pin actions resolve omitted connections against the same saved profile snapshot", () => {
  const source = readFileSync(new URL("../../components/ConversationSpotlight.tsx", import.meta.url), "utf8");
  assert.match(source, /modelPinIdentityForSearchItem\(model, modelProfiles, modelProfilesReady\)/);
  assert.equal((source.match(/pinIdentity\(result\.model\)/g) ?? []).length, 4);
  assert.doesNotMatch(source, /modelPinIdentityForSearchItem\(result\.model\)/);
  const saved = { ...profile, provider_id: "opaque-provider-instance" };
  const publicRow = { display_name: saved.display_name, profile_id: saved.profile_id, provider_id: saved.provider_id, model_id: saved.model_id };
  assert.equal(modelPinIdentityForSearchItem(publicRow, [saved]), modelPinIdentityForProfile(saved));
  assert.equal(modelPinIdentityForSearchItem(publicRow, [saved], false), null);
});
