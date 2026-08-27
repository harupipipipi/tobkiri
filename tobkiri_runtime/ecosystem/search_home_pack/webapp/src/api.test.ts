import test from "node:test";
import assert from "node:assert/strict";

import {
  SEARCH_HOME_CONTRACT_ENDPOINT,
  answerInput,
  loadModelSettings,
  loadModels,
  routeInput,
  searchHomeContractRoute,
  searchHomeContractUrl,
  setPreferredModel,
} from "./api";

function routeKey(path: string): string {
  return `/${path}`;
}

function requestTarget(input: RequestInfo | URL): string {
  const raw = String(input);
  assert.ok(raw.startsWith(SEARCH_HOME_CONTRACT_ENDPOINT));
  const operation = decodeURIComponent(raw.slice(SEARCH_HOME_CONTRACT_ENDPOINT.length));
  const separator = operation.indexOf(" ");
  return separator < 0 ? operation : operation.slice(separator + 1);
}

test("search home API map emits only canonical Host contract URLs", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ target: string; method: string }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ target: requestTarget(input), method: String(init?.method ?? "GET") });
    return new Response(JSON.stringify({ status: "ok", data: { models: [] } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  try {
    await routeInput("hello", "demo/model");
    await answerInput("hello", "demo/model");
    await loadModels();
    await loadModelSettings();
    await setPreferredModel("demo/model");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests.map((request) => request.target), [
    routeKey("api/route"),
    routeKey("api/answer"),
    routeKey("api/models"),
    routeKey("api/settings"),
    routeKey("api/settings/model"),
  ]);
  assert.deepEqual(requests.map((request) => request.method), [
    "POST",
    "POST",
    "GET",
    "GET",
    "POST",
  ]);
});

test("search home route helper rejects recursion and traversal", () => {
  assert.throws(() => searchHomeContractRoute("api/../answer"));
  assert.throws(() => searchHomeContractRoute("/api/contracts/search_home_pack/other"));

  const route = searchHomeContractRoute("api/answer");
  assert.match(searchHomeContractUrl(route, "POST"), /^\/api\/contracts\/search_home_pack\//);
  assert.match(searchHomeContractUrl(route, "POST"), /POST%20%2Fapi%2Fanswer/);
});
