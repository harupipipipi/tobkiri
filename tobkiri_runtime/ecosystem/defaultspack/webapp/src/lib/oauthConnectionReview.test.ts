import assert from "node:assert/strict";
import test from "node:test";

import { reviewConnectionDraft, reviewOAuthDestination } from "./oauthConnectionReview";

test("OAuth destination review permits only the selected provider authorization endpoint", () => {
  const review = reviewOAuthDestination("google", "https://accounts.google.com/o/oauth2/v2/auth?state=opaque-value");
  assert.deepEqual({ providerId: review.providerId, host: review.host, path: review.path }, {
    providerId: "google", host: "accounts.google.com", path: "/o/oauth2/v2/auth",
  });
  for (const unsafe of [
    "//accounts.google.com/o/oauth2/v2/auth", "http://accounts.google.com/o/oauth2/v2/auth",
    "https://user:pass@accounts.google.com/o/oauth2/v2/auth", "https://localhost/o/oauth2/v2/auth",
    "https://127.0.0.1/o/oauth2/v2/auth", "https://evil.example/o/oauth2/v2/auth",
    "https://accounts.google.com/not-oauth",
  ]) assert.throws(() => reviewOAuthDestination("google", unsafe), Error, unsafe);
});

test("credential review redacts secrets and does not retain secret values", () => {
  const secret = "access-token-value-must-not-appear";
  const review = reviewConnectionDraft(JSON.stringify({
    schema: "rumi.connection.credential_bundle.v1",
    access_token: secret,
    scopes: ["read", "write"],
    token_uri: "https://oauth.example.test/token",
    label: "work",
  }));
  assert.equal(review.kind, "connection_import");
  assert.equal(review.secretFieldCount, 1);
  assert.deepEqual(review.scopes, ["read", "write"]);
  assert.deepEqual(review.endpoints, ["oauth.example.test"]);
  assert.equal(JSON.stringify(review).includes(secret), false);
});

test("credential review rejects ambiguous, malformed, and non-object JSON", () => {
  for (const draft of ["not-json", "[]", "{}", JSON.stringify({ token_uri: "https://oauth.example.test/token" })]) {
    assert.throws(() => reviewConnectionDraft(draft), Error, draft);
  }
});
