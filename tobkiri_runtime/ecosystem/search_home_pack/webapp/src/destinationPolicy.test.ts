import assert from "node:assert/strict";
import test from "node:test";

import { evaluateExplicitDestinationInput } from "./destinationPolicy";

test("explicit credential URLs are blocked before routing or search", () => {
  const result = evaluateExplicitDestinationInput("https://qa-user:qa-pass@example.com/private");
  assert.equal(result?.verdict, "block");
  assert.equal(result?.reason, "embedded_credentials");
  assert.equal(result?.normalized_url, "");
});

test("custom schemes and protocol-relative inputs fail closed before routing", () => {
  assert.equal(evaluateExplicitDestinationInput("javascript:alert(1)")?.reason, "unsupported_scheme");
  assert.equal(evaluateExplicitDestinationInput("//example.com/path")?.reason, "malformed_url");
});

test("ordinary search text is not reclassified as an explicit destination", () => {
  assert.equal(evaluateExplicitDestinationInput("how to secure a URL with credentials"), null);
  assert.equal(evaluateExplicitDestinationInput("example.com documentation"), null);
});
