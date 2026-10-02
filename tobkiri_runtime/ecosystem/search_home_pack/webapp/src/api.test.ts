import assert from "node:assert/strict";
import test from "node:test";

import {
  SEARCH_HOME_CONTRACT_ENDPOINT,
  answerInput,
  loadModelSettings,
  loadModels,
  loadRouteState,
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

async function withFetch(response: Response | Error, run: () => Promise<unknown>): Promise<void> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    if (response instanceof Error) throw response;
    return response.clone();
  }) as typeof fetch;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("search home API map emits only canonical Host contract URLs", async () => {
  const originalFetch = globalThis.fetch;
  const requests: Array<{ target: string; method: string }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const target = requestTarget(input);
    requests.push({ target, method: String(init?.method ?? "GET") });
    const payloads: Record<string, unknown> = {
      [routeKey("api/route")]: {
        route_type: "ASK_AI",
        query: "hello",
        target_candidates: [],
      },
      [routeKey("api/answer")]: { status: "ok", answer: "hello" },
      [routeKey("api/models")]: { models: [] },
      [routeKey("api/settings")]: { models: {} },
      [routeKey("api/settings/model")]: { status: "ok" },
      [routeKey("api/route-state")]: {},
    };
    return new Response(JSON.stringify(payloads[target]), {
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
    await loadRouteState();
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests.map((request) => request.target), [
    routeKey("api/route"),
    routeKey("api/answer"),
    routeKey("api/models"),
    routeKey("api/settings"),
    routeKey("api/settings/model"),
    routeKey("api/route-state"),
  ]);
  assert.deepEqual(requests.map((request) => request.method), [
    "POST",
    "POST",
    "GET",
    "GET",
    "POST",
    "GET",
  ]);
});

test("search home route helper rejects recursion and traversal", () => {
  assert.throws(() => searchHomeContractRoute("api/../answer"));
  assert.throws(() => searchHomeContractRoute("/api/contracts/search_home_pack/other"));

  const route = searchHomeContractRoute("api/answer");
  assert.match(searchHomeContractUrl(route, "POST"), /^\/api\/contracts\/search_home_pack\//);
  assert.match(searchHomeContractUrl(route, "POST"), /POST%20%2Fapi%2Fanswer/);
});

test("surfaces backend causes and malformed JSON instead of false success", async () => {
  await withFetch(
    new Response(JSON.stringify({ error: { message: "Backend offline" } }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    }),
    async () => assert.rejects(loadModels(), /Backend offline/),
  );
  await withFetch(
    new Response("<html>bad gateway</html>", { status: 200 }),
    async () => assert.rejects(loadModels(), /Model catalog returned malformed JSON/),
  );
});

test("rejects malformed endpoint payloads with contextual messages", async () => {
  const cases: Array<[Response, () => Promise<unknown>, RegExp]> = [
    [Response.json({ models: null }), loadModels, /Model catalog returned malformed data/],
    [Response.json({ models: [] }), loadModelSettings, /Model settings returned malformed data/],
    [Response.json({ status: "error" }), () => setPreferredModel("demo/model"), /Model preference was not saved/],
    [Response.json({ query: "hello" }), () => routeInput("hello"), /Routing service returned malformed data/],
    [Response.json({ answer: "hello" }), () => answerInput("hello"), /Answer service returned malformed data/],
  ];
  for (const [response, run, expected] of cases) {
    await withFetch(response, async () => assert.rejects(run(), expected));
  }
});

test("accepts well-formed model, route, answer, and preference responses", async () => {
  await withFetch(Response.json({ models: [] }), async () => {
    assert.deepEqual(await loadModels(), { models: [] });
  });
  await withFetch(Response.json({ models: {} }), async () => {
    assert.deepEqual(await loadModelSettings(), { models: {} });
  });
  await withFetch(Response.json({ status: "ok" }), async () => {
    await setPreferredModel("demo/model");
  });
  await withFetch(
    Response.json({ route_type: "ASK_AI", query: "hello", target_candidates: [] }),
    async () => assert.equal((await routeInput("hello")).route_type, "ASK_AI"),
  );
  await withFetch(Response.json({ status: "ok", answer: "hello" }), async () => {
    assert.equal((await answerInput("hello")).answer, "hello");
  });
});
