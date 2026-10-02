import assert from "node:assert/strict";
import test from "node:test";

import { captureSearchRequest, requestErrorMessage, SEARCH_ACTIONS, searchActionIndexForKey } from "./searchRequest";

test("retry snapshots preserve the exact submitted text, model and action", () => {
  let input = "  東京の天気を教えて\n";
  let model = "local/qwen-1b";
  const request = captureSearchRequest(input, model, "answer");
  input = "次の質問";
  model = "other/model";
  assert.deepEqual(request, { input: "  東京の天気を教えて\n", model: "local/qwen-1b", action: "answer" });
  assert.equal(Object.isFrozen(request), true);
  assert.notEqual(request?.input, input);
  assert.notEqual(request?.model, model);
  assert.equal(captureSearchRequest(" \n ", model, "google"), null);
});

test("keyboard actions wrap and do not interfere with Japanese IME", () => {
  assert.equal(searchActionIndexForKey("ArrowUp", 0, false), SEARCH_ACTIONS.length - 1);
  assert.equal(searchActionIndexForKey("ArrowDown", SEARCH_ACTIONS.length - 1, false), 0);
  assert.equal(searchActionIndexForKey("ArrowDown", 0, true), null);
  assert.equal(searchActionIndexForKey("Enter", 0, false), null);
  assert.equal(SEARCH_ACTIONS[0].id, "google");
});

test("failed requests expose a useful service error and a safe fallback", () => {
  assert.equal(requestErrorMessage(new Error("model load failed"), "再試行してください"), "model load failed");
  assert.equal(requestErrorMessage(undefined, "再試行してください"), "再試行してください");
  assert.equal(requestErrorMessage(new Error("  "), "再試行してください"), "再試行してください");
});
