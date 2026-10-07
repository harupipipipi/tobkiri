import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import type { ModelSearchItem } from "../../lib/api";
import {
  clearModelPropertiesRequest,
  getModelPropertiesRequest,
  requestModelProperties,
  subscribeModelPropertiesRequest,
} from "./modelPropertiesNavigation";

const model: ModelSearchItem = {
  profile_id: "catalog/google/model", provider_id: "google", model_id: "model",
  qualified_model_id: "google/model", display_name: "Model", supports_vision: true,
};

afterEach(() => {
  const pending = getModelPropertiesRequest();
  if (pending) clearModelPropertiesRequest(pending.requestId);
});

describe("model properties navigation", () => {
  it("queues properties metadata with no registration or active-model switch action", () => {
    const request = requestModelProperties(model, false)!;
    assert.equal(getModelPropertiesRequest(), request);
    assert.equal(request.intent, "properties");
    assert.equal(request.registered, false);
    assert.deepEqual(request.identity, {
      profileId: undefined, providerId: "google", modelId: "model",
      qualifiedModelId: "google/model", connectionId: undefined,
    });
    assert.deepEqual(Object.keys(request).sort(), ["identity", "intent", "model", "registered", "requestId", "scopeId"]);
    assert.equal(request.model.supports_vision, true);
  });

  it("retains registered profile and selected connection identities", () => {
    const request = requestModelProperties(model, true, "provider.work")!;
    assert.equal(request.identity.profileId, model.profile_id);
    assert.equal(request.identity.connectionId, "provider.work");
    assert.equal(request.identity.providerId, model.provider_id);
    assert.equal(request.identity.modelId, model.model_id);
  });

  it("uses a bound catalog connection and rejects a conflicting explicit selection", () => {
    const bound: ModelSearchItem = {
      ...model, connection_id: "provider.bound", provenance: "provider_public_catalog",
      reachability: "unverified",
    };
    const request = requestModelProperties(bound, false)!;
    assert.equal(request.identity.connectionId, "provider.bound");
    assert.equal(request.model.connection_id, "provider.bound");
    assert.equal(request.model.provenance, "provider_public_catalog");
    assert.equal(request.model.reachability, "unverified");
    assert.equal(requestModelProperties(bound, false, "provider.other"), null);
    assert.equal(getModelPropertiesRequest(), request);
  });

  it("rejects missing provider or model identity without replacing an existing request", () => {
    const first = requestModelProperties(model, false);
    assert.equal(requestModelProperties({ ...model, provider_id: undefined }, false), null);
    assert.equal(requestModelProperties({ ...model, model_id: "" }, false), null);
    assert.equal(getModelPropertiesRequest(), first);
  });

  it("does not retain unknown API secret fields or arbitrary metadata", () => {
    const request = requestModelProperties({
      ...model, api_key: "top-secret", token: "token-secret", endpoint: "endpoint-secret",
      metadata: { api_key: "nested-secret" }, availability: { credential: "availability-secret" },
      notes: "notes-secret", subtitle: "subtitle-secret",
    }, false)!;
    assert.equal(JSON.stringify(request).includes("secret"), false);
    assert.equal(request.model.metadata, undefined);
    assert.equal(request.model.api_key, undefined);
  });

  it("notifies subscribers and clears only the handled request rather than a newer one", () => {
    let notifications = 0;
    const unsubscribe = subscribeModelPropertiesRequest(() => { notifications += 1; });
    const first = requestModelProperties(model, false)!;
    const second = requestModelProperties({ ...model, model_id: "new" }, false)!;
    assert.ok(second.requestId > first.requestId);
    clearModelPropertiesRequest(first.requestId);
    assert.equal(getModelPropertiesRequest(), second);
    assert.equal(notifications, 2);
    clearModelPropertiesRequest(second.requestId);
    assert.equal(getModelPropertiesRequest(), null);
    assert.equal(notifications, 3);
    unsubscribe();
    requestModelProperties(model, false);
    assert.equal(notifications, 3);
  });
});

// Runtime Profile scope is a navigation boundary, separate from a model profile ID.
it("properties requests are visible only to their captured runtime Profile", () => {
  const scoped = requestModelProperties({ profile_id: "route", display_name: "Model", provider_id: "openrouter", model_id: "google/gemini" }, true, "openrouter-main", "profile-one");
  assert.ok(scoped);
  assert.equal(getModelPropertiesRequest("profile-two"), null);
  assert.equal(getModelPropertiesRequest("profile-one")?.requestId, scoped.requestId);
  clearModelPropertiesRequest(scoped.requestId);
});
