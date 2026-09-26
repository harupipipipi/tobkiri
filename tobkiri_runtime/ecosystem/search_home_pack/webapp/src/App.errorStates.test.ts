import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

test("route failure commits the query, action, and model to retryable inline state", () => {
  assert.match(appSource, /title: "Routing request failed"/);
  assert.match(appSource, /<section className="request-error" role="alert">/);
  assert.match(appSource, /void executeSearch\([\s\S]*searchRequestFailure\.action,[\s\S]*searchRequestFailure\.query/);
  assert.match(appSource, /searchRequestFailure\.model/);
  assert.doesNotMatch(appSource, /console\.warn\("Search Home route failed/);
});

test("answer failure commits the query and model to retryable inline state", () => {
  assert.match(appSource, /title: "Answer request failed"/);
  assert.match(appSource, /void runAnswer\([\s\S]*searchRequestFailure\.query/);
  assert.match(appSource, /searchRequestFailure\.model/);
  assert.doesNotMatch(appSource, /console\.warn\("Search Home answer failed/);
});

test("catalog failure remains distinguishable inside the retryable model control", () => {
  assert.match(appSource, /Promise\.allSettled\(\[loadModels\(\), loadModelSettings\(\)\]\)/);
  assert.match(appSource, /Model catalog failed:/);
  assert.match(appSource, /Model settings failed:/);
  assert.match(appSource, /Retry model load/);
  assert.match(appSource, /requestRevision !== modelLoadRequestRef\.current/);
  assert.match(appSource, /selectionRevision === modelSaveRequestRef\.current/);
  assert.doesNotMatch(appSource, /\.catch\(\(\) => undefined\)/);
});

test("settings-save failure restores the previous selection and exposes retry", () => {
  assert.match(appSource, /const previousModel = selectedModel/);
  assert.match(appSource, /setSelectedModel\(previousModel\)/);
  assert.match(appSource, /The previous selection was restored/);
  assert.match(appSource, /Retry save/);
  assert.match(appSource, /selectModel\(modelSaveFailure\.attemptedModel\)/);
});
