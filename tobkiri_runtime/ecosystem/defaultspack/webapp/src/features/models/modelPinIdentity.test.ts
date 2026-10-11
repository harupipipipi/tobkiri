import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { ModelProfile, ModelSearchItem } from "../../lib/api";
import {
  enrichModelSelectOptions,
  mergeRegisteredModelProfileOptions,
  modelSearchItemToModelSelectOption,
  type ModelSelectOption,
} from "./modelSelect";
import {
  modelPinIdentityForOption,
  modelPinIdentityForProfile,
  modelPinIdentityForSearchItem,
} from "./modelPinIdentity";

const route: ModelProfile = {
  profile_id: "opaque.route", display_name: "Same name", route_configured: true,
  provider_id: "provider.openrouter.work", model_id: "google/gemini",
};
const catalog: ModelSearchItem = {
  profile_id: "openrouter/google/gemini", display_name: "Same name",
  provider_id: "openrouter", model_id: "google/gemini",
};

describe("model pin identities", () => {
  it("joins a registered route and a bound catalog despite family/instance provider differences", () => {
    const bound = { ...catalog, connection_id: route.provider_id };
    const option = mergeRegisteredModelProfileOptions([], [route])[0];
    assert.equal(option.connection_id, route.provider_id);
    assert.equal(option.value, route.profile_id);
    assert.equal(option.registered_profile_id, undefined);
    const expected = JSON.stringify(["connection", route.provider_id, route.model_id]);
    assert.equal(modelPinIdentityForProfile(route), expected);
    assert.equal(modelPinIdentityForSearchItem(bound), expected);
    assert.equal(modelPinIdentityForOption(option), expected);
    assert.equal(modelPinIdentityForOption(modelSearchItemToModelSelectOption(bound)), expected);
  });

  it("uses one model pin for multiple saved Profiles on the same connection and native model", () => {
    const second = { ...route, profile_id: "different.route", display_name: "Another label", defaults: { temperature: 0.2 } };
    assert.equal(modelPinIdentityForProfile(route), modelPinIdentityForProfile(second));
    assert.equal(route.profile_id, "opaque.route");
    assert.equal(second.profile_id, "different.route");
  });

  it("joins the actual saved search response without connection_id to an exact loaded Profile", () => {
    const item: ModelSearchItem = {
      profile_id: route.profile_id, display_name: route.display_name,
      provider_id: route.provider_id, model_id: route.model_id,
      metadata: { provider_connection_id: "ignored-from-response", api_key: "secret" },
    };
    assert.equal(item.connection_id, undefined);
    assert.equal(modelPinIdentityForSearchItem(item, [route]), modelPinIdentityForProfile(route));
    assert.equal(modelPinIdentityForSearchItem(item), JSON.stringify(["catalog", route.provider_id, route.model_id]));
    const option = enrichModelSelectOptions([modelSearchItemToModelSelectOption(item)], [route])[0];
    assert.equal(option.value, route.profile_id);
    assert.equal(option.connection_id, route.provider_id);
    assert.equal(modelPinIdentityForOption(option), modelPinIdentityForProfile(route));
    assert.equal(option.registered_profile_id, undefined);
  });

  it("never binds public catalog ID collisions, qualified aliases or stale provider/model rows", () => {
    const collision = { ...catalog, profile_id: route.profile_id };
    assert.equal(modelPinIdentityForSearchItem(collision, [route]), modelPinIdentityForSearchItem(catalog));
    for (const item of [
      collision,
      { ...catalog, provider_id: route.provider_id, qualified_model_id: route.profile_id },
      { ...catalog, profile_id: route.profile_id, provider_id: route.provider_id, model_id: "other-model" },
    ]) {
      const option = enrichModelSelectOptions([modelSearchItemToModelSelectOption(item)], [route])[0];
      assert.equal(option.connection_id, undefined);
      assert.notEqual(modelPinIdentityForSearchItem(item, [route]), modelPinIdentityForProfile(route));
    }
    assert.equal(modelPinIdentityForSearchItem(catalog, []), JSON.stringify(["catalog", "openrouter", "google/gemini"]));
  });

  it("fails an ambiguous exact join rather than picking the last saved connection", () => {
    const first = { ...route, metadata: { provider_connection_id: "opaque.first" } };
    const second = { ...route, metadata: { provider_connection_id: "opaque.second" } };
    const item = { ...catalog, profile_id: route.profile_id, provider_id: route.provider_id };
    assert.equal(modelPinIdentityForSearchItem(item, [first, second]), null);
    const option = enrichModelSelectOptions([modelSearchItemToModelSelectOption(item)], [first, second])[0];
    assert.equal(option.connection_id, undefined);
    assert.equal(modelPinIdentityForSearchItem(item, [first, { ...first }]), modelPinIdentityForProfile(first));
  });

  it("keeps an explicit catalog connection rather than overwriting it through a saved-profile join", () => {
    const item = { ...catalog, profile_id: route.profile_id, provider_id: route.provider_id, connection_id: "opaque.explicit" };
    assert.equal(modelPinIdentityForSearchItem(item, [route]), JSON.stringify(["connection", "opaque.explicit", route.model_id]));
    const option = enrichModelSelectOptions([modelSearchItemToModelSelectOption(item)], [route])[0];
    assert.equal(option.connection_id, "opaque.explicit");
  });

  it("separates same-name provider and API connection variants", () => {
    assert.notEqual(modelPinIdentityForSearchItem(catalog), modelPinIdentityForSearchItem({ ...catalog, provider_id: "google" }));
    assert.notEqual(modelPinIdentityForProfile(route), modelPinIdentityForProfile({ ...route, provider_id: "provider.openrouter.personal" }));
    assert.notEqual(modelPinIdentityForSearchItem(catalog), modelPinIdentityForSearchItem({ ...catalog, connection_id: route.provider_id }));
  });

  it("keeps local instances and unbound local catalog choices distinct", () => {
    const local = { ...catalog, provider_id: "ollama", model_id: "llama:latest", local: true };
    assert.equal(modelPinIdentityForSearchItem(local), JSON.stringify(["catalog", "ollama", "llama:latest"]));
    assert.notEqual(modelPinIdentityForSearchItem({ ...local, connection_id: "local.work" }), modelPinIdentityForSearchItem({ ...local, connection_id: "local.personal" }));
    assert.notEqual(modelPinIdentityForSearchItem(local), modelPinIdentityForSearchItem({ ...local, connection_id: "local.work" }));
  });

  it("preserves opaque connections, case, maker namespaces, suffixes and tuple boundaries", () => {
    const item = { ...catalog, connection_id: "接続/opaque:Work", model_id: "Google/Gemini:free" };
    assert.equal(modelPinIdentityForSearchItem(item), JSON.stringify(["connection", "接続/opaque:Work", "Google/Gemini:free"]));
    assert.notEqual(modelPinIdentityForSearchItem(item), modelPinIdentityForSearchItem({ ...item, model_id: "google/gemini:free" }));
    assert.notEqual(modelPinIdentityForSearchItem(item), modelPinIdentityForSearchItem({ ...item, model_id: "Google/Gemini" }));
    assert.notEqual(modelPinIdentityForSearchItem({ ...item, connection_id: "a/b", model_id: "c" }), modelPinIdentityForSearchItem({ ...item, connection_id: "a", model_id: "b/c" }));
  });

  it("does not derive identity from names, values, qualified aliases, configuration or secrets", () => {
    const option: ModelSelectOption = {
      value: "openrouter/google/gemini", label: "Same name", qualified_model_id: "openrouter/google/gemini",
      configured: true, provider_id: "openrouter",
    };
    assert.equal(modelPinIdentityForOption(option), null);
    assert.equal(modelPinIdentityForSearchItem({ ...catalog, model_id: undefined }), null);
    assert.equal(modelPinIdentityForProfile({ ...route, model_id: undefined }), null);
    assert.equal(modelPinIdentityForOption({ ...option, model_id: "m", provider_id: undefined }), null);
    assert.equal(modelPinIdentityForSearchItem({ ...catalog, provider_id: undefined }), null);
    assert.equal(modelPinIdentityForOption({ ...option, model_id: "m", registered_profile_id: "opaque.route" }), null);
    assert.equal(modelPinIdentityForSearchItem({ ...catalog, api_key: "credential-secret", metadata: { api_key: "nested-secret" } }), modelPinIdentityForSearchItem(catalog));
  });

  it("reads explicit catalog family metadata without interpreting a provider-looking connection name", () => {
    const option: ModelSelectOption = {
      value: "opaque.catalog", label: "Model", provider_id: "maker",
      catalog_provider_id: "openrouter", model_id: "google/gemini",
    };
    assert.equal(modelPinIdentityForOption(option), JSON.stringify(["catalog", "openrouter", "google/gemini"]));
    assert.equal(modelPinIdentityForOption({ ...option, connection_id: "arbitrary-opaque-id" }), JSON.stringify(["connection", "arbitrary-opaque-id", "google/gemini"]));
  });

  it("keeps identities when credentials, reachability or route availability change", () => {
    assert.equal(modelPinIdentityForProfile({ ...route, route_configured: false, availability: { status: "unavailable" } }), modelPinIdentityForProfile(route));
    assert.equal(modelPinIdentityForSearchItem({ ...catalog, configured: false, reachability: "unknown" }), modelPinIdentityForSearchItem(catalog));
  });
});


it("waits for the saved-profile snapshot before pinning an omitted connection", () => {
  const raw = { profile_id: "saved", display_name: "Same", provider_id: "opaque", model_id: "native" };
  assert.equal(modelPinIdentityForSearchItem(raw, [], false), null);
  assert.equal(modelPinIdentityForSearchItem(raw, [{...raw, route_configured:true}], true), JSON.stringify(["connection", "opaque", "native"]));
  assert.equal(modelPinIdentityForSearchItem({...raw,connection_id:"explicit"}, [], false), JSON.stringify(["connection", "explicit", "native"]));
});

it("invalid opaque identifiers never expose an enabled no-op pin", () => {
  assert.equal(modelPinIdentityForSearchItem({profile_id:"saved",display_name:"Same",provider_id:"provider",model_id:"bad\u0000model"}), null);
  assert.equal(modelPinIdentityForSearchItem({profile_id:"saved",display_name:"Same",provider_id:"provider",model_id:"m",connection_id:"a".repeat(513)}), null);
});
