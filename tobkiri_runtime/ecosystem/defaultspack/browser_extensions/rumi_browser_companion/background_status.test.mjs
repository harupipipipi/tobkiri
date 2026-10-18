import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const backgroundSource = await readFile(new URL("./background.js", import.meta.url), "utf8");
const helperStart = backgroundSource.indexOf("async function safeJson");
const helperEnd = backgroundSource.indexOf("function joinUrl");
assert.ok(helperStart >= 0 && helperEnd > helperStart);
const helperSource = backgroundSource.slice(helperStart, helperEnd);
const coordinatorStart = backgroundSource.indexOf("function pollBridge(trigger)");
const coordinatorEnd = backgroundSource.indexOf("async function runPollBridge");
assert.ok(coordinatorStart >= 0 && coordinatorEnd > coordinatorStart);
const coordinatorSource = backgroundSource.slice(coordinatorStart, coordinatorEnd);

function createHarness(fetchImplementation) {
  const context = vm.createContext({
    AbortController,
    Error,
    clearTimeout,
    fetch: fetchImplementation,
    setTimeout
  });
  vm.runInContext(
    `const BRIDGE_REQUEST_TIMEOUT_MS = 20;
    ${helperSource}
    globalThis.bridgeStatusTestApi = {
      fetchBridgeEnvelope,
      normalizeBridgeFailure
    };`,
    context,
    { filename: "background-status-helpers.js" }
  );
  return context.bridgeStatusTestApi;
}

test("HTTP rejection maps pairing, version, and offline states without response data", async () => {
  const cases = [
    [401, "pairing_rejected", "PAIRING_REJECTED"],
    [403, "pairing_rejected", "PAIRING_REJECTED"],
    [409, "version_incompatible", "VERSION_INCOMPATIBLE"],
    [426, "version_incompatible", "VERSION_INCOMPATIBLE"],
    [503, "bridge_offline", "BRIDGE_UNAVAILABLE"]
  ];

  for (const [status, expectedState, expectedCode] of cases) {
    const api = createHarness(async () => ({
      ok: false,
      status,
      text: async () => '{"pairing_token":"must-not-escape"}'
    }));
    await assert.rejects(
      api.fetchBridgeEnvelope("http://127.0.0.1", {}, "poll"),
      (error) => {
        const failure = api.normalizeBridgeFailure(error);
        assert.equal(failure.state, expectedState);
        assert.equal(failure.diagnostic.code, expectedCode);
        assert.equal(failure.diagnostic.httpStatus, status);
        assert.doesNotMatch(JSON.stringify(failure), /must-not-escape/);
        return true;
      }
    );
  }
});

test("malformed successful response is typed and does not retain raw content", async () => {
  const api = createHarness(async () => ({
    ok: true,
    status: 200,
    text: async () => "not-json pairing-token-must-not-escape"
  }));

  await assert.rejects(
    api.fetchBridgeEnvelope("http://127.0.0.1", {}, "poll"),
    (error) => {
      const failure = api.normalizeBridgeFailure(error);
      assert.equal(failure.state, "malformed_response");
      assert.equal(failure.diagnostic.code, "MALFORMED_BRIDGE_RESPONSE");
      assert.doesNotMatch(JSON.stringify(failure), /pairing-token-must-not-escape/);
      return true;
    }
  );
});

test("request timeout maps to a retryable timeout state", async () => {
  const api = createHarness((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    });
  }));

  await assert.rejects(
    api.fetchBridgeEnvelope("http://127.0.0.1", {}, "poll"),
    (error) => {
      const failure = api.normalizeBridgeFailure(error);
      assert.equal(failure.state, "timeout");
      assert.equal(failure.diagnostic.code, "BRIDGE_REQUEST_TIMEOUT");
      return true;
    }
  );
});

test("valid bridge envelope passes through unchanged", async () => {
  const envelope = { status: "ok", data: { accepted: true } };
  const api = createHarness(async () => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify(envelope)
  }));

  assert.equal(
    JSON.stringify(await api.fetchBridgeEnvelope("http://127.0.0.1", {}, "poll")),
    JSON.stringify(envelope)
  );
});

test("background coordinator coalesces concurrent polls and releases after completion", async () => {
  const context = vm.createContext({ Promise });
  vm.runInContext(
    `let pollInFlight = null;
    let pollCalls = 0;
    let resolvePoll;
    function runPollBridge(trigger) {
      pollCalls += 1;
      return new Promise((resolve) => {
        resolvePoll = () => resolve(trigger);
      });
    }
    ${coordinatorSource}
    globalThis.pollCoordinatorTestApi = {
      pollBridge,
      pollCalls: () => pollCalls,
      resolvePoll: () => resolvePoll()
    };`,
    context,
    { filename: "background-poll-coordinator.js" }
  );
  const api = context.pollCoordinatorTestApi;
  const first = api.pollBridge("manual");
  const duplicate = api.pollBridge("alarm");

  assert.equal(first, duplicate);
  assert.equal(api.pollCalls(), 1);
  api.resolvePoll();
  assert.equal(await first, "manual");
  await Promise.resolve();

  const next = api.pollBridge("settingsChanged");
  assert.equal(api.pollCalls(), 2);
  api.resolvePoll();
  assert.equal(await next, "settingsChanged");
});
