import assert from "node:assert/strict";
import test from "node:test";

import {
  cycleCandidateIndex,
  normalizeSelectedIndex,
  reviewRouteDestination,
  selectedCandidateUrl,
  type RouteDecision,
} from "./routerTypes";

const decision: RouteDecision = {
  query: "deepseek v4 semianalysis",
  target_url: "https://example.com/a",
  target_candidates: [
    {
      url: "https://example.com/a",
      final_url: "https://example.com/a",
      title: "Candidate A",
      domain: "untrusted-backend-label.test",
    },
    {
      url: "https://example.com/b",
      final_url: "https://example.com/b",
      title: "Candidate B",
      domain: "example.com",
    },
    {
      url: "https://example.com/c",
      final_url: "https://example.com/c",
      title: "Candidate C",
      domain: "example.com",
    },
  ],
  selected_index: 0,
  fallback_url: "https://www.google.com/search?q=deepseek+v4+semianalysis",
  resolution_reason: "heuristic:official_domain",
  used_ai_judge: false,
  used_visual_judge: false,
};

test("candidate cycling changes selection but never performs navigation", () => {
  assert.equal(cycleCandidateIndex(decision, 0, 1), 1);
  assert.equal(cycleCandidateIndex(decision, 0, -1), 2);
  assert.equal(selectedCandidateUrl(decision, 1), "https://example.com/b");
});

test("invalid selected indexes normalize to the first candidate", () => {
  assert.equal(normalizeSelectedIndex(decision, -1), 0);
  assert.equal(normalizeSelectedIndex(decision, 99), 0);
  assert.equal(selectedCandidateUrl(decision, 99), "https://example.com/a");
});

test("normalizes safe HTTPS destinations and derives the host from the URL", () => {
  const review = reviewRouteDestination("https://Example.com:443/a/../b?q=1#private-fragment");
  assert.equal(review.ok, true);
  if (!review.ok) return;
  assert.equal(review.url, "https://example.com/b?q=1");
  assert.equal(review.host, "example.com");
  assert.deepEqual(review.warnings, []);
});

test("allows public HTTP only with an explicit warning", () => {
  const review = reviewRouteDestination("http://example.com/path");
  assert.equal(review.ok, true);
  if (!review.ok) return;
  assert.deepEqual(review.warnings, ["暗号化されていないHTTP接続です"]);
});

test("blocks non-web, relative, credentialed, and malformed destinations", () => {
  for (const value of [
    "javascript:alert(1)",
    "data:text/html,hello",
    "file:///tmp/private",
    "//example.com/path",
    "/relative/path",
    "https://user:secret@example.com/",
    " https://example.com/",
    "https://example.com/\u0000bad",
    "https://example.com/%0d%0aheader",
    "https:\\example.com\\path",
  ]) {
    assert.equal(reviewRouteDestination(value).ok, false, value);
  }
});

test("blocks loopback, private IPv4, local names, and private IPv6", () => {
  for (const value of [
    "http://127.0.0.1:8766/chat",
    "http://10.0.0.2/",
    "http://172.16.10.2/",
    "http://192.168.1.4/",
    "http://169.254.1.1/",
    "http://service.local/",
    "http://localhost/",
    "http://[::1]/",
    "http://[fd00::1]/",
  ]) {
    const review = reviewRouteDestination(value);
    assert.equal(review.ok, false, value);
    if (!review.ok) assert.equal(review.code, "private_network", value);
  }
});

test("flags punycode and non-standard ports for explicit review", () => {
  const review = reviewRouteDestination("https://xn--pple-43d.example:8443/login");
  assert.equal(review.ok, true);
  if (!review.ok) return;
  assert.equal(review.warnings.length, 2);
  assert.match(review.warnings.join(" "), /Punycode/);
  assert.match(review.warnings.join(" "), /8443/);
});
