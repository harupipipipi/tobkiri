import assert from "node:assert/strict";
import test from "node:test";

import {
  clearDecisionSessionStorage,
  coerceRouteDecision,
  decisionFromSessionState,
  loadDecisionFromSessionStorage,
  ROUTE_DECISION_STORAGE_KEY,
  saveDecisionToSessionStorage,
} from "./routeState";
import { ROUTE_SESSION_STORAGE_KEY, type RouteDecision } from "./routerTypes";

class MemoryStorage implements Storage {
  #values = new Map<string, string>();
  get length(): number {
    return this.#values.size;
  }
  clear(): void {
    this.#values.clear();
  }
  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.#values.delete(key);
  }
  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }
}

const decision: RouteDecision = {
  route_type: "URL_NAVIGATION",
  query: "private query",
  target_url: "https://example.com/",
  target_candidates: [
    { url: "https://example.com/", title: "Public", domain: "forged.invalid" },
    { url: "http://127.0.0.1/admin", title: "Blocked", domain: "example.com" },
  ],
  selected_index: 0,
  fallback_url: "https://www.google.com/search?q=private+query",
  metadata: { secret: "do-not-store" },
};

test("coercion drops arbitrary metadata and bounds candidate arrays", () => {
  const parsed = coerceRouteDecision({
    ...decision,
    target_candidates: Array.from({ length: 80 }, (_, index) => ({ url: `https://example.com/${index}` })),
  });
  assert.ok(parsed);
  assert.equal(parsed.target_candidates.length, 50);
  assert.deepEqual(parsed.metadata, {});
});

test("saving persists only policy-approved URLs and no metadata secrets", () => {
  const storage = new MemoryStorage();
  const state = saveDecisionToSessionStorage(storage, decision, 0);
  assert.ok(state);
  assert.equal(state.target_candidates.length, 1);
  const saved = storage.getItem(ROUTE_DECISION_STORAGE_KEY) ?? "";
  assert.equal(saved.includes("127.0.0.1"), false);
  assert.equal(saved.includes("do-not-store"), false);
  assert.equal(saved.includes("forged.invalid"), false);
  assert.ok(storage.getItem(ROUTE_SESSION_STORAGE_KEY));
});

test("loading removes legacy state and restores only fresh policy-reviewed session state", () => {
  const storage = new MemoryStorage();
  storage.setItem(ROUTE_DECISION_STORAGE_KEY, "{broken");
  storage.setItem(
    ROUTE_SESSION_STORAGE_KEY,
    JSON.stringify({
      query: "restored",
      target_url: "https://example.com/",
      fallback_url: "https://google.com/",
      selected_index: 0,
      target_candidates: [{ url: "https://example.com/", final_url: "https://example.com/", title: "A", domain: "example.com" }],
      updated_at: new Date().toISOString(),
      state_id: "0123456789abcdef0123456789abcdef",
      issued_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 5 * 60 * 1000).toISOString(),
    }),
  );
  const restored = loadDecisionFromSessionStorage(storage);
  assert.ok(restored);
  assert.equal(restored.query, "restored");
  assert.equal(storage.getItem(ROUTE_DECISION_STORAGE_KEY), null);
});

test("loading rejects expired and tampered session records without trusting backend domains", () => {
  const storage = new MemoryStorage();
  storage.setItem(
    ROUTE_SESSION_STORAGE_KEY,
    JSON.stringify({
      query: "restored",
      target_url: "https://example.com/",
      fallback_url: "https://google.com/",
      selected_index: 0,
      target_candidates: [
        { url: "https://example.com/", final_url: "https://example.com/", domain: "forged.invalid" },
      ],
      state_id: "0123456789abcdef0123456789abcdef",
      issued_at: new Date(Date.now() - 10 * 60 * 1000).toISOString(),
      expires_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    }),
  );
  assert.equal(loadDecisionFromSessionStorage(storage), null);
  assert.equal(storage.getItem(ROUTE_SESSION_STORAGE_KEY), null);
});

test("backend restore accepts only fresh session envelopes and rederives candidate domains", () => {
  const fresh = {
    ...decision,
    state_id: "0123456789abcdef0123456789abcdef",
    issued_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 5 * 60 * 1000).toISOString(),
  };
  const restored = decisionFromSessionState(fresh);
  assert.ok(restored);
  assert.equal(restored.target_candidates[0]?.domain, "example.com");
  assert.equal(decisionFromSessionState({ ...fresh, state_id: "tampered value" }), null);
  assert.equal(
    decisionFromSessionState({
      ...fresh,
      expires_at: new Date(Date.now() - 1).toISOString(),
    }),
    null,
  );
});

test("clear removes every retained route record", () => {
  const storage = new MemoryStorage();
  saveDecisionToSessionStorage(storage, decision, 0);
  clearDecisionSessionStorage(storage);
  assert.equal(storage.length, 0);
});
