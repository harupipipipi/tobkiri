import test from "node:test";
import assert from "node:assert/strict";

import type { AuthorityRequest } from "./api";
import {
  authorityApprovalHintMessage,
  readStoredAuthorityApprovalSettlement,
  verifyAuthorityApprovalHint,
} from "./authorityApprovalEvents";

function request(overrides: Partial<AuthorityRequest> = {}): AuthorityRequest {
  return {
    request_id: "request-1",
    status: "approved",
    principal_id: "principal-1",
    permission_id: "permission-1",
    resource: {},
    reason: "test",
    risk_level: "medium",
    created_at: "2026-07-10T00:00:00Z",
    conversation_id: "conversation-1",
    ...overrides,
  };
}

test("approval notification messages carry only a wake-up hint", () => {
  const message = authorityApprovalHintMessage({
    requestId: "request-1",
    conversationId: "conversation-1",
  }, 1_000);
  const serialized = JSON.stringify(message);

  assert.match(serialized, /rumi-authority-approval-hint/);
  assert.match(serialized, /request-1/);
  assert.doesNotMatch(serialized, /approved|denied|"status"/);
  assert.equal(message.hint.emittedAt, 1_000);
  assert.ok(message.hint.nonce.length >= 8);
});

test("verification uses the authoritative request status", async () => {
  const message = authorityApprovalHintMessage({
    requestId: "request-1",
    conversationId: "conversation-1",
  }, 1_000);
  const settlement = await verifyAuthorityApprovalHint(
    message.hint,
    async () => request({ status: "denied" }),
    1_100,
  );

  assert.deepEqual(settlement, {
    requestId: "request-1",
    status: "denied",
    conversationId: "conversation-1",
  });
});

test("pending, mismatched, stale, and unavailable hints never settle UI", async () => {
  const message = authorityApprovalHintMessage({
    requestId: "request-1",
    conversationId: "conversation-1",
  }, 1_000);

  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => request({ status: "pending" }),
    1_100,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => request({ request_id: "request-other" }),
    1_100,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => request({ conversation_id: "conversation-other" }),
    1_100,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => request(),
    32_000,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => {
      throw new Error("backend unavailable");
    },
    1_100,
  ), null);
});

test("verification binds settlement to the pending request context and expiry", async () => {
  const message = authorityApprovalHintMessage({
    requestId: "request-1",
    conversationId: "conversation-1",
  }, 1_000);
  const expected = {
    requestId: "request-1",
    principalId: "principal-1",
    permissionId: "permission-1",
    conversationId: "conversation-1",
    resource: { provider_id: "local-provider", model_id: "local-model" },
  };
  const authoritative = request({
    resource: expected.resource,
    expires_at: "2030-01-01T00:00:00Z",
  });

  assert.ok(await verifyAuthorityApprovalHint(
    message.hint,
    async () => authoritative,
    1_100,
    expected,
  ));
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => ({ ...authoritative, principal_id: "principal-other" }),
    1_100,
    expected,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => ({ ...authoritative, permission_id: "permission-other" }),
    1_100,
    expected,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => ({ ...authoritative, resource: { provider_id: "other" } }),
    1_100,
    expected,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => ({ ...authoritative, expires_at: "1970-01-01T00:00:01Z" }),
    2_000,
    expected,
  ), null);
  assert.equal(await verifyAuthorityApprovalHint(
    message.hint,
    async () => ({ ...authoritative, expires_at: "not-a-date" }),
    1_100,
    expected,
  ), null);
});

test("legacy localStorage settlement records are deleted and never replayed", () => {
  const originalWindow = globalThis.window;
  let removedKey = "";
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: {
        removeItem(key: string) {
          removedKey = key;
        },
      },
    },
  });

  try {
    assert.equal(readStoredAuthorityApprovalSettlement({ requestId: "request-1" }), null);
    assert.equal(removedKey, "rumi.authority.approval.settlement");
  } finally {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: originalWindow,
    });
  }
});
