import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { ModelSearchItem, ModelSearchResponse } from "../../lib/api";
import {
  MODEL_CATALOG_SEARCH_FAILED,
  createModelCatalogSearchController,
  createModelCatalogSearchStore,
  modelCatalogSearchKey,
  sanitizeModelCatalogItem,
} from "./modelCatalogSearch";

const item = (provider: string, model: string, profile = `${provider}/${model}`): ModelSearchItem => ({
  provider_id: provider, model_id: model, profile_id: profile, display_name: model,
});
const response = (models: ModelSearchItem[], hasMore?: boolean): ModelSearchResponse => ({
  models, filters_applied: {}, ...(hasMore === undefined ? {} : { has_more: hasMore }),
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let count = 0; count < 12; count += 1) await Promise.resolve(); };
function timers() {
  let nextId = 0;
  const callbacks = new Map<number, () => void>();
  const delays: number[] = [];
  return {
    delays,
    schedule(callback: () => void, delay: number) {
      delays.push(delay);
      const id = ++nextId;
      callbacks.set(id, callback);
      return id as unknown as ReturnType<typeof setTimeout>;
    },
    cancel(timer: ReturnType<typeof setTimeout>) { callbacks.delete(timer as unknown as number); },
    run() {
      const pending = [...callbacks.values()];
      callbacks.clear();
      pending.forEach((callback) => callback());
    },
  };
}

describe("shared model catalog source", () => {
  it("normalizes payloads and deduplicates normalized provider filter/cache keys", async () => {
    const calls: Record<string, unknown>[] = [];
    const store = createModelCatalogSearchStore(async (filters) => {
      calls.push(filters);
      return response([], false);
    });
    await store.search({ text: "  reasoning  ", providerIds: [" google ", "google", ""] });
    await store.search({ text: "reasoning", providerIds: ["google"] });
    assert.deepEqual(calls, [{ query: "reasoning", provider_id: "google", max_results: 100 }]);
    assert.notEqual(modelCatalogSearchKey({ text: "x" }), modelCatalogSearchKey({ text: "x", connectionId: "provider.work" }));
  });

  it("shares inflight calls, then expires its bounded cache", async () => {
    let time = 100;
    let calls = 0;
    const pending = deferred<ModelSearchResponse>();
    const store = createModelCatalogSearchStore(() => { calls += 1; return pending.promise; }, {
      now: () => time, cacheTtlMs: 20, maxCacheEntries: 1,
    });
    const first = store.search({ text: "a" });
    const second = store.search({ text: "a" });
    assert.equal(first, second);
    await flush();
    assert.equal(calls, 1);
    pending.resolve(response([item("google", "a")], false));
    await first;
    assert.ok(store.peek({ text: "a" }));
    time = 121;
    assert.equal(store.peek({ text: "a" }), undefined);
    await store.search({ text: "a" });
    await store.search({ text: "b" });
    assert.equal(store.peek({ text: "a" }), undefined);
    assert.ok(store.peek({ text: "b" }));
    assert.equal(calls, 3);
  });

  it("partitions cached/inflight results by Runtime Profile scope without sending client authority", async () => {
    const pending: ReturnType<typeof deferred<ModelSearchResponse>>[] = [];
    const store = createModelCatalogSearchStore((filters) => {
      assert.equal("profile_id" in filters, false);
      assert.equal("scopeId" in filters, false);
      assert.equal("scope_id" in filters, false);
      const request = deferred<ModelSearchResponse>();
      pending.push(request);
      return request.promise;
    });
    const firstQuery = { text: "m", providerIds: ["google"], scopeId: "profile-one" };
    const secondQuery = { ...firstQuery, scopeId: "profile-two" };
    const first = store.search(firstQuery);
    const second = store.search(secondQuery);
    assert.notEqual(first, second);
    await flush();
    assert.equal(pending.length, 2);
    pending[0].resolve(response([item("google", "first-private")], false));
    pending[1].resolve(response([item("google", "second-private")], false));
    await Promise.all([first, second]);
    assert.equal(store.peek(firstQuery)?.models[0].model_id, "first-private");
    assert.equal(store.peek(secondQuery)?.models[0].model_id, "second-private");
    assert.equal((await store.search(secondQuery)).models[0].model_id, "second-private");
    assert.equal(pending.length, 2);
  });

  it("queries provider OR filters in parallel and preserves provider/profile identities", async () => {
    const requests = new Map<string, ReturnType<typeof deferred<ModelSearchResponse>>>();
    const store = createModelCatalogSearchStore((filters) => {
      const pending = deferred<ModelSearchResponse>();
      requests.set(String(filters.provider_id), pending);
      return pending.promise;
    });
    const result = store.search({ text: "same", providerIds: ["beta", "alpha"] });
    await flush();
    assert.deepEqual([...requests.keys()], ["alpha", "beta"]);
    requests.get("beta")!.resolve(response([item("beta", "same")], false));
    requests.get("alpha")!.resolve(response([
      item("alpha", "same"), item("alpha", "same"), item("alpha", "same", "custom-profile"),
    ], false));
    const state = await result;
    assert.deepEqual(state.models.map((model) => model.profile_id), ["alpha/same", "custom-profile", "beta/same"]);
    assert.equal(state.complete, true);
  });

  it("caps merged results and never infers an exhaustive catalog from omitted pagination", async () => {
    const store = createModelCatalogSearchStore(async (filters) => response(
      Array.from({ length: 80 }, (_, index) => item(String(filters.provider_id), `model-${index}`)), false,
    ));
    const merged = await store.search({ text: "", providerIds: ["a", "b"] });
    assert.equal(merged.models.length, 100);
    assert.equal(merged.complete, false);
    const unknown = createModelCatalogSearchStore(async () => response([]));
    assert.equal((await unknown.search({ text: "" })).complete, false);
    const more = createModelCatalogSearchStore(async () => response([], true));
    assert.equal((await more.search({ text: "" })).complete, false);
  });

  it("rejects models when a mandatory provider filter was ignored or identity omitted", async () => {
    const store = createModelCatalogSearchStore(async () => response([
      item("google", "matching"), item("other", "foreign"),
      { profile_id: "unbound", model_id: "unknown", display_name: "Unknown" },
    ], false));
    const query = { text: "", providerIds: ["google"] };
    const state = await store.search(query);
    assert.deepEqual(state.models.map((model) => model.model_id), ["matching"]);
    assert.equal(state.models[0].provider_id, "google");
    assert.equal(state.error, MODEL_CATALOG_SEARCH_FAILED);
    assert.equal(state.complete, false);
    assert.equal(store.peek(query), undefined);
  });

  it("keeps successful provider results on partial failure without caching the failure or secret", async () => {
    let fail = true;
    let calls = 0;
    const store = createModelCatalogSearchStore(async (filters) => {
      calls += 1;
      if (filters.provider_id === "b" && fail) throw new Error("credential-secret");
      return response([item(String(filters.provider_id), "m")], false);
    });
    const query = { text: "m", providerIds: ["a", "b"] };
    const failed = await store.search(query);
    assert.equal(failed.models.length, 1);
    assert.equal(failed.error, MODEL_CATALOG_SEARCH_FAILED);
    assert.equal(failed.complete, false);
    assert.equal(store.peek(query), undefined);
    assert.equal(JSON.stringify(failed).includes("credential-secret"), false);
    fail = false;
    const retried = await store.search(query, true);
    assert.equal(retried.models.length, 2);
    assert.equal(retried.error, null);
    assert.equal(calls, 4);
  });

  it("binds public catalog models to exact connections without claiming verified reachability", async () => {
    const calls: Record<string, unknown>[] = [];
    const store = createModelCatalogSearchStore(async (filters) => {
      calls.push(filters);
      const connectionId = String(filters.connection_id);
      return {
        ...response([{
          ...item("openrouter", "google/gemini"), connection_id: connectionId,
          provenance: "provider_public_catalog", reachability: "unverified",
        }], false),
        filters_applied: { connection_id: connectionId },
      };
    });
    const first = await store.search({ text: "gemini", providerIds: ["openrouter"], connectionId: " provider.personal " });
    const second = await store.search({ text: "gemini", providerIds: ["openrouter"], connectionId: "provider.work" });
    assert.equal(calls.length, 2);
    assert.equal(calls[0].connection_id, "provider.personal");
    assert.equal(first.models[0].provider_id, "openrouter");
    assert.equal(first.models[0].model_id, "google/gemini");
    assert.equal(first.models[0].connection_id, "provider.personal");
    assert.equal(second.models[0].connection_id, "provider.work");
    assert.equal(first.models[0].provenance, "provider_public_catalog");
    assert.equal(first.models[0].reachability, "unverified");
    assert.equal(first.models[0].health_status, "unverified");
    assert.equal(first.complete, true);
    assert.equal(first.error, null);
    const cached = await store.search({ text: "gemini", providerIds: ["openrouter"], connectionId: "provider.personal" });
    assert.equal(cached, first);
    assert.equal(calls.length, 2);
  });

  it("fails closed on ignored connection filters, cross-connection items and unknown provenance", async () => {
    const query = { text: "", providerIds: ["google"], connectionId: "provider.personal" };
    const matching = {
      ...item("google", "m"), connection_id: "provider.personal",
      provenance: "provider_public_catalog" as const, reachability: "unverified" as const,
    };
    for (const result of [
      response([matching], false),
      { ...response([{ ...matching, connection_id: "provider.work" }], false), filters_applied: { connection_id: query.connectionId } },
      { ...response([{ ...matching, provenance: undefined }], false), filters_applied: { connection_id: query.connectionId } },
      { ...response([{ ...matching, reachability: "unknown" as const }], false), filters_applied: { connection_id: query.connectionId } },
    ]) {
      const store = createModelCatalogSearchStore(async () => result);
      const state = await store.search(query);
      assert.equal(state.error, MODEL_CATALOG_SEARCH_FAILED);
      assert.equal(state.complete, false);
      assert.deepEqual(state.models, []);
      assert.equal(store.peek(query), undefined);
    }
  });

  it("ignores a stale connection response after selecting another connection", async () => {
    const clock = timers();
    const pending = new Map<string, ReturnType<typeof deferred<ModelSearchResponse>>>();
    const store = createModelCatalogSearchStore((filters) => {
      const request = deferred<ModelSearchResponse>();
      pending.set(String(filters.connection_id), request);
      return request.promise;
    });
    const controller = createModelCatalogSearchController(store, clock);
    controller.setQuery({ text: "m", connectionId: "provider.old" }, true);
    clock.run(); await flush();
    controller.setQuery({ text: "m", connectionId: "provider.new" }, true);
    clock.run(); await flush();
    const bound = (connectionId: string) => ({
      ...response([{
        ...item("google", "m"), connection_id: connectionId,
        provenance: "provider_public_catalog" as const, reachability: "unverified" as const,
      }], false), filters_applied: { connection_id: connectionId },
    });
    pending.get("provider.new")!.resolve(bound("provider.new")); await flush();
    pending.get("provider.old")!.resolve(bound("provider.old")); await flush();
    assert.equal(controller.getSnapshot().models[0].connection_id, "provider.new");
  });

  it("copies an explicit field allowlist and keeps health unverified despite configured metadata", () => {
    const input = {
      ...item("google", "m"), configured: true, notes: "notes-secret", subtitle: "subtitle-secret", api_key: "top-secret",
      metadata: { credential: "nested-secret", endpoint: "https://private.example" },
      availability: { configured: true, api_key: "other-secret", reachability: "available", health_status: "verified" },
      reachability: "available", health_status: "verified", capability_tags: ["vision", { key: "array-secret" }],
    };
    const clean = sanitizeModelCatalogItem(input)!;
    assert.equal(clean.configured, true);
    assert.equal(clean.reachability, "unknown");
    assert.equal(clean.health_status, "unverified");
    assert.equal(clean.metadata, undefined);
    assert.equal(clean.notes, undefined);
    assert.equal(clean.subtitle, undefined);
    assert.deepEqual(clean.capability_tags, ["vision"]);
    assert.deepEqual(clean.availability, { configured: true, reachability: "unknown", health_status: "unverified" });
    assert.equal(JSON.stringify(clean).includes("secret"), false);
    assert.equal(sanitizeModelCatalogItem({ display_name: "No identity" }), null);
  });
});

describe("model catalog controller", () => {
  it("ignores a stale response after Runtime Profile scope changes with otherwise identical filters", async () => {
    const clock = timers();
    const pending: ReturnType<typeof deferred<ModelSearchResponse>>[] = [];
    const store = createModelCatalogSearchStore(() => {
      const request = deferred<ModelSearchResponse>();
      pending.push(request);
      return request.promise;
    });
    const controller = createModelCatalogSearchController(store, clock);
    controller.setQuery({ text: "m", scopeId: "old-profile" }, true);
    clock.run(); await flush();
    controller.setQuery({ text: "m", scopeId: "new-profile" }, true);
    assert.deepEqual(controller.getSnapshot().models, []);
    clock.run(); await flush();
    pending[1].resolve(response([item("google", "new-private")], false)); await flush();
    pending[0].resolve(response([item("google", "old-private")], false)); await flush();
    assert.equal(controller.getSnapshot().models[0].model_id, "new-private");
  });

  it("debounces input, ignores old responses and clears results when disabled", async () => {
    const clock = timers();
    const requests = new Map<string, ReturnType<typeof deferred<ModelSearchResponse>>>();
    const store = createModelCatalogSearchStore((filters) => {
      const pending = deferred<ModelSearchResponse>();
      requests.set(String(filters.query), pending);
      return pending.promise;
    });
    const controller = createModelCatalogSearchController(store, clock);
    controller.setQuery({ text: "a" }, true);
    controller.setQuery({ text: "ab" }, true);
    assert.equal(controller.getSnapshot().loading, true);
    assert.deepEqual(clock.delays, [120, 120]);
    clock.run();
    await flush();
    assert.deepEqual([...requests.keys()], ["ab"]);
    controller.setQuery({ text: "new" }, true);
    clock.run();
    await flush();
    requests.get("new")!.resolve(response([item("google", "new")], false));
    await flush();
    requests.get("ab")!.resolve(response([item("google", "old")], false));
    await flush();
    assert.equal(controller.getSnapshot().models[0].model_id, "new");
    controller.setQuery({ text: "new" }, false);
    assert.deepEqual(controller.getSnapshot(), { models: [], loading: false, error: null, complete: false });
  });

  it("retries a failed request, bypasses a cached result and guards responses after cancel", async () => {
    const clock = timers();
    let calls = 0;
    let fail = true;
    const pending = deferred<ModelSearchResponse>();
    const store = createModelCatalogSearchStore(async () => {
      calls += 1;
      if (fail) throw new Error("secret-error");
      if (calls === 3) return pending.promise;
      return response([item("google", "success")], false);
    });
    const controller = createModelCatalogSearchController(store, clock);
    controller.setQuery({ text: "x" }, true);
    clock.run(); await flush();
    assert.equal(controller.getSnapshot().error, MODEL_CATALOG_SEARCH_FAILED);
    fail = false;
    controller.retry(); clock.run(); await flush();
    assert.equal(controller.getSnapshot().error, null);
    assert.equal(controller.getSnapshot().models[0].model_id, "success");
    controller.retry(); clock.run(); await flush();
    assert.equal(calls, 3);
    controller.cancel();
    pending.resolve(response([item("google", "late")], false)); await flush();
    assert.equal(controller.getSnapshot().models.length, 0);
    assert.equal(clock.delays.at(-1), 0);
  });
});
