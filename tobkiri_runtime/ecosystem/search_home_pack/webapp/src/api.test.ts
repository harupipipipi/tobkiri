import test from "node:test";
import assert from "node:assert/strict";

import {
  SEARCH_HOME_CONTRACT_ENDPOINT,
  answerInput,
  loadModelSettings,
  loadModels,
  loadRouteState,
  routeInput,
  SearchHomeRequestError,
  searchHomeRequestMessage,
  searchHomeContractRoute,
  searchHomeContractUrl,
  setPreferredModel,
} from "./api";
import type { SearchHomeAttachment } from "./attachments";

const attachment: SearchHomeAttachment = {
  id: "a1",
  name: "notes.txt",
  size: 5,
  type: "text/plain",
  content: "alpha",
};

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
  const requests: Array<{
    target: string;
    method: string;
    body?: Record<string, unknown>;
  }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({
      target: requestTarget(input),
      method: String(init?.method ?? "GET"),
      body: init?.body
        ? JSON.parse(String(init.body)) as Record<string, unknown>
        : undefined,
    });
    return new Response(JSON.stringify({ status: "ok", data: { models: [] } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  try {
    await routeInput("hello", "demo/model", [attachment]);
    await answerInput("hello", "demo/model", [attachment]);
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
  assert.deepEqual(requests[0].body?.attachments, [attachment]);
  assert.deepEqual(requests[1].body?.attachments, [attachment]);
  assert.equal(requests[1].body?.use_search, true);
});

test("search home route helper rejects recursion and traversal", () => {
  assert.throws(() => searchHomeContractRoute("api/../answer"));
  assert.throws(() => searchHomeContractRoute("/api/contracts/search_home_pack/other"));

  const route = searchHomeContractRoute("api/answer");
  assert.match(searchHomeContractUrl(route, "POST"), /^\/api\/contracts\/search_home_pack\//);
  assert.match(searchHomeContractUrl(route, "POST"), /POST%20%2Fapi%2Fanswer/);
});

test("request failures expose only fixed user-facing copy", () => {
  assert.equal(
    searchHomeRequestMessage(
      new SearchHomeRequestError("ATTACHMENT_MODEL_UNSUPPORTED"),
      "fallback",
    ),
    "The selected model does not advertise image input. Choose a vision-capable model or remove the image.",
  );
  assert.equal(
    searchHomeRequestMessage(new Error("secret provider traceback"), "Safe fallback"),
    "Safe fallback",
  );
});
