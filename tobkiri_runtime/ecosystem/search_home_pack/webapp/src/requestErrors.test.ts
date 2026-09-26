import assert from "node:assert/strict";
import test from "node:test";

import { describeRequestError, joinRequestErrors } from "./requestErrors";

test("uses bounded human-readable transport causes", () => {
  assert.equal(describeRequestError(new Error("Backend offline"), "Request failed."), "Backend offline");
  assert.equal(describeRequestError({ error: { message: "Gateway unavailable" } }, "fallback"), "Gateway unavailable");
  assert.equal(describeRequestError(null, "Request failed."), "Request failed.");
  assert.equal(describeRequestError(new Error("x".repeat(400)), "fallback").length, 280);
});

test("redacts common credentials before displaying backend errors", () => {
  const cloudKey = ["AKIA", "EXAMPLEEXAMPLE12"].join("");
  const jwt = ["headerheaderheader", "payloadpayload", "signaturesignature"].join(".");
  const privateKey = ["-----BEGIN PRIVATE KEY-----", "sensitive-body", "-----END PRIVATE KEY-----"].join("\n");
  const message = describeRequestError(
    new Error(
      `Bearer secret-token api_key=sk-example123456789 token=xoxb-secret123456 `
      + `aws=${cloudKey} jwt=${jwt} https://user:password@example.test ${privateKey}`,
    ),
    "Request failed.",
  );
  assert.doesNotMatch(message, /secret-token|sk-example|xoxb-secret|AKIA|payloadpayload|password|sensitive-body/);
  assert.match(message, /redacted/);
});

test("combines independent catalog and settings errors without duplicates", () => {
  assert.equal(joinRequestErrors(["Catalog failed.", "Settings failed.", "Catalog failed."]), "Catalog failed. Settings failed.");
});
