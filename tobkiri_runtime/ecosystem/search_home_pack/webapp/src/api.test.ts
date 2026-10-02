import test from "node:test";
import assert from "node:assert/strict";

import {
  bootstrapDefaultspackLocalAuth,
  defaultspackApiHeaders,
} from "../../../defaultspack/webapp/src/lib/apiAuth";
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

function requestTarget(input: RequestInfo | URL): string {
  const raw = String(input);
  assert.ok(raw.startsWith("/api/contracts/defaultspack/"));
  const operation = decodeURIComponent(raw.slice(SEARCH_HOME_CONTRACT_ENDPOINT.length));
  return operation.slice(operation.indexOf(" ") + 1);
}

function hostResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify({ success: true, data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function modelState(revision = 7): Record<string, unknown> {
  return { namespace: "model-state-owner", revision, values: { preferred_model: "local/model" } };
}

test("Search uses the captured Defaults catalog, state, and native answer routes", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ target: string; method: string; body: unknown }> = [];
  globalThis.fetch = async (input, init) => {
    const target = requestTarget(input);
    const method = String(init?.method ?? "GET");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ target, method, body });
    if (target === "/api/ai/models/search") {
      return hostResponse({ status: "ok", data: { models: [{ profile_id: "local/model" }] } });
    }
    if (target === "/api/search/answer") {
      return hostResponse({ status: "ok", answer: "READY", model: "local/model", used_tools: [] });
    }
    if (method === "PUT") {
      return hostResponse({
        ...body,
        namespace: "model-state-owner",
        revision: body.expected_revision + 1,
        receipt: `sha256:${"a".repeat(64)}`,
      });
    }
    return hostResponse(modelState());
  };

  assert.equal((await answerInput("hello", "local/model")).answer, "READY");
  assert.equal((await loadModels()).models[0].profile_id, "local/model");
  assert.equal((await loadModelSettings()).models?.preferred_model, "local/model");
  await setPreferredModel("other/model");
  assert.deepEqual(requests.map(({ target, method }) => ({ target, method })), [
    { target: "/api/search/answer", method: "POST" },
    { target: "/api/ai/models/search", method: "POST" },
    { target: "/api/ui/model-state", method: "GET" },
    { target: "/api/ui/model-state", method: "GET" },
    { target: "/api/ui/model-state", method: "PUT" },
  ]);
  assert.deepEqual(requests[0].body, { input: "hello", model: "local/model" });
  assert.deepEqual(requests[1].body, { max_results: 100, offset: 0 });
  const write = requests[4].body as Record<string, unknown>;
  assert.equal(write.kind, "preferred_model");
  assert.equal(write.value, "other/model");
  assert.equal(write.expected_revision, 7);
  assert.match(String(write.mutation_id), /^[0-9a-f-]{36}$/);
  assert.equal("profile_id" in write, false);
  assert.equal("approved" in write, false);
});

test("Search follows the current shared default without replacing it with a stub", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const requests: Array<{ target: string; body: unknown }> = [];
  globalThis.fetch = async (input, init) => {
    const target = requestTarget(input);
    requests.push({ target, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return target === "/api/ui/model-state"
      ? hostResponse(modelState())
      : hostResponse({ status: "ok", answer: "From Defaults", model: "local/model" });
  };
  assert.equal((await answerInput("hello")).model, "local/model");
  await setPreferredModel("");
  await setPreferredModel("local/model");
  assert.deepEqual(requests.map(({ target }) => target), [
    "/api/ui/model-state", "/api/search/answer", "/api/ui/model-state", "/api/ui/model-state",
  ]);
  assert.deepEqual(requests[1].body, { input: "hello", model: "local/model" });
});

test("Search does not write with a missing settings revision or resend a conflict", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return hostResponse({ namespace: "model-state-owner", values: {} });
  };
  await assert.rejects(setPreferredModel("other/model"), /settings revision/);
  assert.equal(requests, 1);

  requests = 0;
  globalThis.fetch = async (_input, init) => {
    requests += 1;
    return init?.method === "PUT"
      ? new Response(JSON.stringify({ success: false, error: "STALE_REVISION" }), { status: 409 })
      : hostResponse(modelState());
  };
  await assert.rejects(setPreferredModel("other/model"), /STALE_REVISION/);
  assert.equal(requests, 2);
});

test("Search requires the owner receipt to match the submitted model and mutation", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async (_input, init) => {
    if (init?.method !== "PUT") return hostResponse(modelState());
    const body = JSON.parse(String(init.body));
    return hostResponse({
      ...body,
      namespace: "model-state-owner",
      revision: 8,
      mutation_id: "wrong-mutation",
      receipt: `sha256:${"a".repeat(64)}`,
    });
  };
  await assert.rejects(setPreferredModel("other/model"), /再送せず/);
});

test("Search unwraps answer errors and rejects hidden Host and Pack failures", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const failedAnswer = { status: "error", error: { code: "MODEL_UNAVAILABLE", message: "Model unavailable" } };
  globalThis.fetch = async () => hostResponse(failedAnswer, 502);
  assert.deepEqual(await answerInput("hello", "local/model"), failedAnswer);

  globalThis.fetch = async () => new Response(JSON.stringify({ success: false, error: "Host denied" }), { status: 403 });
  await assert.rejects(answerInput("hello", "local/model"), /Host denied/);

  globalThis.fetch = async () => hostResponse({ status: "error", error: { message: "Catalog unavailable" } });
  await assert.rejects(loadModels(), /Catalog unavailable/);

  globalThis.fetch = async () => new Response(JSON.stringify({ status: "ok", models: [] }));
  await assert.rejects(loadModels(), /Host data envelope/);
  globalThis.fetch = async () => new Response(JSON.stringify(failedAnswer), { status: 502 });
  await assert.rejects(answerInput("hello", "local/model"), /Host data envelope/);
});

test("Search rejects malformed answer/catalog data and oversized input before dispatch", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return hostResponse({ status: "ok", answer: { injected: "value" } });
  };
  await assert.rejects(answerInput("hello", "local/model"), /answer payload/);
  await assert.rejects(answerInput("あ".repeat(21000), "local/model"), /60 KiB/);
  await assert.rejects(answerInput(" ", "local/model"), /60 KiB/);
  assert.equal(requests, 1);
  globalThis.fetch = async () => hostResponse({ models: [null] });
  await assert.rejects(loadModels(), /model catalog/);
});

test("URL and Google routing remain local, guarded, and truthful about lookup", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => { throw new Error("routing must not dispatch a legacy service"); };
  const direct = await routeInput("https://github.com/harupipipipi/tobkiri");
  assert.equal(direct.route_type, "URL_NAVIGATE");
  assert.equal(direct.target_candidates[0].source, "direct_url");
  assert.equal((await routeInput("github.com")).target_url, "https://github.com/");
  for (const input of ["http://127.0.0.1:18080", "javascript:alert(1)", "https://user:password@example.com", "example.local"]) {
    assert.equal((await routeInput(input)).route_type, "BLOCKED_DESTINATION_INPUT");
  }
  const google = await routeInput("!g 東京 天気");
  assert.equal(google.route_type, "GOOGLE_REDIRECT");
  assert.equal(new URL(google.target_url).searchParams.get("q"), "東京 天気");
  assert.equal(google.used_ai_judge, false);
  assert.equal((await routeInput("Tokyo weather", "", "open")).route_type, "GOOGLE_REDIRECT");
  assert.equal((await routeInput("1 + 1?", "local/model")).route_type, "ASK_AI");
});

test("Search route helpers reject recursive namespaces and path traversal", () => {
  for (const route of ["api/../answer", "api/./answer", "/api//answer", "/api/contracts/search_home_pack/other", "/api/contracts/defaultspack/other"]) {
    assert.throws(() => searchHomeContractRoute(route));
  }
  assert.equal(
    searchHomeContractUrl(searchHomeContractRoute("api/search/answer"), "POST"),
    `/api/contracts/defaultspack/${encodeURIComponent("POST /api/search/answer")}`,
  );
});

test("Search shares Defaults session/CSRF headers and reads the current token after rotation", async (context) => {
  const originalFetch = globalThis.fetch;
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const storage = new Map([ ["rumi-panel-csrf", "csrf-first"] ]);
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  } });
  context.after(() => {
    globalThis.fetch = originalFetch;
    if (previousStorage) Object.defineProperty(globalThis, "sessionStorage", previousStorage);
    else Reflect.deleteProperty(globalThis, "sessionStorage");
  });
  const headers: Headers[] = [];
  globalThis.fetch = async (_input, init) => {
    headers.push(new Headers(init?.headers));
    assert.equal(init?.credentials, "same-origin");
    return hostResponse({ models: [] });
  };
  await loadModels();
  assert.equal(headers[0].get("X-Rumi-CSRF"), defaultspackApiHeaders("POST").get("X-Rumi-CSRF"));
  storage.set("rumi-panel-csrf", "csrf-rotated");
  await loadModels();
  assert.deepEqual(headers.map((item) => item.get("X-Rumi-CSRF")), ["csrf-first", "csrf-rotated"]);
  assert.ok(headers.every((item) => /^[0-9a-f-]{36}$/.test(item.get("X-Tobkiri-Request-ID") ?? "")));
});

test("shared local auth consumes the launch fragment once without exposing it in the URL", (context) => {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const storage = new Map<string, string>();
  let cleanUrl = "";
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  } });
  Object.defineProperty(globalThis, "document", { configurable: true, value: { title: "Tobkiri Search" } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    location: { hash: "#rumi_local_auth=shared-launch-token&tab=search", pathname: "/search/", search: "" },
    history: { state: null, replaceState: (_state: unknown, _title: string, url: string) => { cleanUrl = url; } },
  } });
  context.after(() => {
    for (const [key, descriptor] of [["sessionStorage", previousStorage], ["window", previousWindow], ["document", previousDocument]] as const) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    }
  });
  assert.equal(bootstrapDefaultspackLocalAuth(), "shared-launch-token");
  assert.equal(cleanUrl, "/search/#tab=search");
  assert.equal(storage.get("rumi-defaultspack-local-auth"), "shared-launch-token");
  assert.equal(defaultspackApiHeaders("POST").get("Authorization"), "Bearer shared-launch-token");
});
