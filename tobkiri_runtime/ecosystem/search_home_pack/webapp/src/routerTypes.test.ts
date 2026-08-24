import assert from "node:assert/strict";
import test from "node:test";

import {
  buildBrowserCompanionRouteMessage,
  buildRouteSessionState,
  cycleCandidateIndex,
  normalizeSelectedIndex,
  reviewRouteDestination,
  reviewRouteCandidate,
  sanitizeRouteDecisionForStorage,
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
  assert.equal(review.confirmationRequired, false);
  assert.deepEqual(review.warnings, []);
});

test("allows public HTTP only with an explicit warning", () => {
  const review = reviewRouteDestination("http://example.com/path");
  assert.equal(review.ok, true);
  if (!review.ok) return;
  assert.equal(review.confirmationRequired, true);
  assert.match(review.warnings.join(" "), /HTTP/);
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
    if (!review.ok) assert.equal(review.code, "unsafe_local_target", value);
  }
});

test("flags punycode and non-standard ports for explicit review", () => {
  const review = reviewRouteDestination("https://xn--pple-43d.example:8443/login");
  assert.equal(review.ok, true);
  if (!review.ok) return;
  assert.equal(review.warnings.length, 2);
  assert.equal(review.confirmationRequired, true);
  assert.match(review.warnings.join(" "), /Punycode/);
  assert.match(review.warnings.join(" "), /8443/);
});

test("candidate review validates both redirect endpoints and requires cross-origin confirmation", () => {
  const crossOrigin = reviewRouteCandidate({
    url: "https://example.com/start",
    final_url: "https://example.net/end",
    redirected: true,
  });
  assert.equal(crossOrigin.ok, true);
  if (crossOrigin.ok) {
    assert.equal(crossOrigin.confirmationRequired, true);
    assert.equal(crossOrigin.host, "example.net");
  }
  const unsafeStart = reviewRouteCandidate({
    url: "javascript:alert(1)",
    final_url: "https://example.com/end",
    redirected: true,
  });
  assert.equal(unsafeStart.ok, false);
});

test("session state excludes blocked candidates and ignores backend domain labels", () => {
  const unsafeDecision: RouteDecision = {
    ...decision,
    target_candidates: [
      decision.target_candidates[0],
      {
        url: "http://127.0.0.1/admin",
        title: "Internal admin",
        domain: "totally-safe.example",
      },
    ],
  };
  const state = buildRouteSessionState(unsafeDecision, 0);
  assert.equal(state.target_candidates.length, 1);
  assert.equal(state.target_candidates[0]?.domain, "example.com");
  assert.equal(state.target_candidates[0]?.final_url, "https://example.com/a");
  assert.equal(JSON.stringify(state).includes("127.0.0.1"), false);
  assert.equal(JSON.stringify(state).includes("totally-safe.example"), false);
});

test("stored decisions discard arbitrary metadata and unsafe URLs", () => {
  const sanitized = sanitizeRouteDecisionForStorage(
    {
      ...decision,
      target_url: "javascript:alert(1)",
      metadata: { secret: "do-not-store" },
    },
    0,
  );
  assert.deepEqual(sanitized.metadata, {});
  assert.equal(JSON.stringify(sanitized).includes("do-not-store"), false);
  assert.equal(JSON.stringify(sanitized).includes("javascript:"), false);
});

test("browser companion message is origin-bound, expiring, and secret-free", () => {
  const message = buildBrowserCompanionRouteMessage(decision, 2);
  assert.equal(message.type, "rumi:search-home:set-route-state");
  assert.equal(message.source, "rumi-search-home");
  assert.equal(message.payload.target_url, "https://example.com/c");
  assert.equal(message.payload.target_candidates.length, 3);
  assert.match(message.payload.state_id, /^[a-f0-9]{32}$/);
  assert.ok(Date.parse(message.payload.expires_at) > Date.parse(message.payload.issued_at));
});

test("session state never persists fragments or credential-like query values", () => {
  const secret = "fake-secret-do-not-store";
  const state = buildRouteSessionState({
    ...decision,
    query: `https://example.com/?access_token=${secret}`,
    target_url: `https://example.com/?access_token=${secret}`,
    fallback_url: "https://www.google.com/search?q=safe",
    target_candidates: [{ url: `https://example.com/path#${secret}` }],
  });
  assert.equal(JSON.stringify(state).includes(secret), false);
  assert.equal(state.target_candidates.length, 0);
  assert.equal(state.target_url, state.fallback_url);
});
