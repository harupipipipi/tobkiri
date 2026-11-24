import assert from "node:assert/strict";
import test from "node:test";
import {
  createModelCatalogSearchController,
  createModelCatalogSearchStore,
} from "./modelCatalogSearch";
import {
  clearModelPropertiesRequest,
  getModelPropertiesRequest,
  invalidateModelPropertiesScope,
  requestModelProperties,
  subscribeModelPropertiesRequest,
} from "./modelPropertiesNavigation";
import { searchCaptureScope } from "./searchCaptureScope";

const capture = {
  profile_id: "defaults", profile_revision: "revision-1", activation_id: "activation-1",
  plan_hash: "plan-1", catalog_hash: "catalog-1", security_epoch: 1,
};
const model = {
  profile_id: "saved-model", display_name: "Saved model", provider_id: "openrouter",
  model_id: "maker/model", connection_id: "saved-connection",
};

test("each trusted capture field partitions an explicitly fixed Profile scope", () => {
  const original = searchCaptureScope("defaults", capture);
  for (const field of ["profile_id", "profile_revision", "activation_id", "plan_hash", "catalog_hash"] as const) {
    assert.notEqual(searchCaptureScope("defaults", { ...capture, [field]: "changed" }), original);
  }
  assert.notEqual(searchCaptureScope("defaults", { ...capture, security_epoch: 2 }), original);
  assert.notEqual(searchCaptureScope("defaults", capture, "changed-active-plan"), original);
  assert.equal(searchCaptureScope("defaults", { ...capture }), original);
});

test("scope copies only non-secret capture fields and supports fixtures without Host metadata", () => {
  const extra = { ...capture, authorization: "Bearer secret-marker", credential_handle: "secret-marker" };
  assert.equal(searchCaptureScope("defaults", extra), searchCaptureScope("defaults", capture));
  assert.doesNotMatch(searchCaptureScope("defaults", extra), /secret-marker|authorization|credential/);
  assert.notEqual(searchCaptureScope("profile-a"), searchCaptureScope("profile-b"));
  assert.equal(searchCaptureScope("defaults", { ...capture, security_epoch: NaN }),
    searchCaptureScope("defaults", { ...capture, security_epoch: undefined }));
});

test("same-Profile reactivation cannot reuse cached or inflight model metadata", async () => {
  let calls = 0;
  let resolveFirst!: (value: { models: typeof model[]; filters_applied: Record<string, unknown> }) => void;
  const firstResponse = new Promise<{ models: typeof model[]; filters_applied: Record<string, unknown> }>((resolve) => { resolveFirst = resolve; });
  const store = createModelCatalogSearchStore(async () => {
    calls += 1;
    return calls === 1 ? firstResponse : { models: [{ ...model, display_name: "New activation" }], filters_applied: {} };
  });
  const firstQuery = { text: "", providerIds: ["openrouter"], scopeId: searchCaptureScope("defaults", capture) };
  const secondQuery = { ...firstQuery, scopeId: searchCaptureScope("defaults", { ...capture, activation_id: "activation-2" }) };
  const first = store.search(firstQuery);
  await Promise.resolve();
  const second = await store.search(secondQuery);
  assert.equal(calls, 2);
  assert.equal(second.models[0].display_name, "New activation");
  resolveFirst({ models: [model], filters_applied: {} });
  await first;
  assert.equal(store.peek(secondQuery)?.models[0].display_name, "New activation");
  assert.equal(store.peek(firstQuery)?.models[0].display_name, "Saved model");
});

test("activation transition immediately hides and then invalidates pending properties", () => {
  const firstScope = searchCaptureScope("defaults", capture);
  const secondScope = searchCaptureScope("defaults", { ...capture, activation_id: "activation-2" });
  const pending = requestModelProperties(model, true, model.connection_id, firstScope)!;
  assert.equal(getModelPropertiesRequest(firstScope), pending);
  assert.equal(getModelPropertiesRequest(secondScope), null);
  let notifications = 0;
  const unsubscribe = subscribeModelPropertiesRequest(() => { notifications += 1; });
  invalidateModelPropertiesScope(firstScope);
  assert.equal(getModelPropertiesRequest(firstScope), pending);
  assert.equal(notifications, 0);
  invalidateModelPropertiesScope(secondScope);
  assert.equal(getModelPropertiesRequest(), null);
  assert.equal(notifications, 1);
  const current = requestModelProperties(model, true, model.connection_id, secondScope)!;
  clearModelPropertiesRequest(pending.requestId);
  assert.equal(getModelPropertiesRequest(secondScope), current);
  clearModelPropertiesRequest(current.requestId);
  unsubscribe();
});

test("a stale same-Profile activation result cannot replace the new controller snapshot", async () => {
  let resolveOld!: (value: { models: typeof model[]; filters_applied: Record<string, unknown> }) => void;
  const oldResponse = new Promise<{ models: typeof model[]; filters_applied: Record<string, unknown> }>((resolve) => { resolveOld = resolve; });
  let calls = 0;
  const store = createModelCatalogSearchStore(async () => {
    calls += 1;
    return calls === 1 ? oldResponse : { models: [{ ...model, display_name: "Current capture" }], filters_applied: {} };
  });
  const callbacks: Array<() => void> = [];
  const controller = createModelCatalogSearchController(store, {
    schedule: (callback) => { callbacks.push(callback); return 1 as unknown as ReturnType<typeof setTimeout>; },
    cancel: () => undefined,
  });
  controller.setQuery({ text: "", scopeId: searchCaptureScope("defaults", capture) }, true);
  callbacks.shift()!();
  await Promise.resolve();
  controller.setQuery({ text: "", scopeId: searchCaptureScope("defaults", { ...capture, catalog_hash: "new-catalog" }) }, true);
  assert.equal(controller.getSnapshot().loading, true);
  assert.deepEqual(controller.getSnapshot().models, []);
  callbacks.shift()!();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.getSnapshot().models[0].display_name, "Current capture");
  resolveOld({ models: [model], filters_applied: {} });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.getSnapshot().models[0].display_name, "Current capture");
});
