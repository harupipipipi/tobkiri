import assert from "node:assert/strict";
import test from "node:test";

import {
  evaluateDestination,
  evaluateExplicitDestinationInput,
  evaluateRedirectDestination,
  urlSafeForPersistence,
} from "./destinationPolicy";

test("destination policy covers supported, review-only, and blocked protocols", () => {
  assert.equal(evaluateDestination("https://example.com/path").verdict, "allow");
  assert.equal(evaluateDestination("http://example.com/path").verdict, "confirm");
  for (const value of [
    "javascript:alert(1)",
    "data:text/html,hello",
    "file:///tmp/private",
    "custom://example.com/path",
    "/relative/path",
  ]) {
    assert.equal(evaluateDestination(value).verdict, "block", value);
  }
});

test("destination policy blocks ambiguous credentials, controls, and local targets", () => {
  for (const value of [
    "https://user:password@example.com/",
    " https://example.com/",
    "https://example.com/%0d%0aheader",
    "https:\\example.com\\path",
    "http://127.0.0.1/",
    "http://2130706433/",
    "http://100.64.0.1/",
    "http://service.lan/",
    "http://service.home/",
    "http://[::ffff:127.0.0.1]/",
    "http://[ff02::1]/",
    "http://[fec0::1]/",
  ]) {
    assert.equal(evaluateDestination(value).verdict, "block", value);
  }
});

test("IDN and redirect chains require review and unsafe hops fail closed", () => {
  assert.equal(evaluateDestination("https://例え.テスト/path").verdict, "confirm");
  assert.equal(evaluateDestination("https://xn--r8jz45g.xn--zckzah/path").verdict, "confirm");
  assert.equal(
    evaluateRedirectDestination("https://example.com/start", "https://example.net/end", true).reason,
    "cross_origin_redirect",
  );
  assert.equal(
    evaluateRedirectDestination("http://example.com/start", "http://127.0.0.1/end", true).verdict,
    "block",
  );
  assert.equal(
    evaluateRedirectDestination("javascript:alert(1)", "https://example.com/end", true).verdict,
    "block",
  );
});

test("persistence excludes fragments and credential-like query parameters", () => {
  assert.equal(urlSafeForPersistence("https://example.com/path#private"), "");
  assert.equal(urlSafeForPersistence("https://example.com/?access_token=fake"), "");
  assert.equal(urlSafeForPersistence("https://example.com/?access_token"), "");
  assert.equal(urlSafeForPersistence("https://example.com/path"), "https://example.com/path");
});

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
