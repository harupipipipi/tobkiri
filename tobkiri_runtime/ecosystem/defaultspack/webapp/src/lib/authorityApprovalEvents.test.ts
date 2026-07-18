import test from "node:test";
import assert from "node:assert/strict";

import type { AuthorityRequest } from "./api";
import {
  AUTHORITY_APPROVAL_RETURN_PARAM,
  AUTHORITY_APPROVAL_RETURN_STORAGE_KEY,
  authorityApprovalHintMessage,
  consumeAuthorityApprovalReturnHint,
  createAuthorityApprovalReturnPath,
  readStoredAuthorityApprovalSettlement,
  verifyAuthorityApprovalHint,
  verifyAuthorityApprovalRequest,
} from "./authorityApprovalEvents";

class MemoryStorage {
  values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

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

test("approval return paths carry a one-time opaque correlation instead of settlement state", () => {
  const storage = new MemoryStorage();
  const returnPath = createAuthorityApprovalReturnPath(
    "request-private-id",
    "https://app.example.test/ambient-debug?chat=conversation-1&authority_approved=1#panel",
    storage,
    1_000,
  );
  const url = new URL(returnPath, "https://app.example.test");
  const nonce = url.searchParams.get(AUTHORITY_APPROVAL_RETURN_PARAM);

  assert.ok(nonce);
  assert.equal(url.searchParams.get("authority_approved"), null);
  assert.equal(url.searchParams.get("chat"), "conversation-1");
  assert.equal(url.hash, "#panel");
  assert.doesNotMatch(returnPath, /request-private-id|approved|denied/);
  assert.match(storage.getItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY) ?? "", /request-private-id/);

  assert.equal(consumeAuthorityApprovalReturnHint(url.search, storage, 1_100), "request-private-id");
  assert.equal(storage.getItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY), null);
  assert.equal(consumeAuthorityApprovalReturnHint(url.search, storage, 1_200), null);
});

test("forged, mismatched, stale, corrupt, and storage-less return hints fail closed", () => {
  const forged = new MemoryStorage();
  createAuthorityApprovalReturnPath("request-1", "/ambient-debug", forged, 1_000);
  assert.equal(consumeAuthorityApprovalReturnHint("?authority_return=forged", forged, 1_100), null);
  assert.equal(forged.getItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY), null);

  const stale = new MemoryStorage();
  const stalePath = createAuthorityApprovalReturnPath("request-1", "/ambient-debug", stale, 1_000);
  assert.equal(consumeAuthorityApprovalReturnHint(stalePath.split("?")[1] ?? "", stale, 301_001), null);

  const corrupt = new MemoryStorage();
  corrupt.setItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY, "not-json");
  assert.equal(consumeAuthorityApprovalReturnHint("?authority_return=value", corrupt, 1_100), null);
  assert.equal(consumeAuthorityApprovalReturnHint("?authority_return=value", null, 1_100), null);

  assert.equal(
    createAuthorityApprovalReturnPath("request-1", "/ambient-debug?authority_approved=1", null, 1_000),
    "/ambient-debug",
  );
});

test("authoritative request verification rejects pending, expired, and wrong request responses", async () => {
  assert.equal(await verifyAuthorityApprovalRequest(
    "request-1",
    "conversation-1",
    async () => request({ status: "pending" }),
    Date.parse("2026-07-10T00:00:01Z"),
  ), null);
  assert.equal(await verifyAuthorityApprovalRequest(
    "request-1",
    "conversation-1",
    async () => request({ expires_at: "2026-07-10T00:00:00Z" }),
    Date.parse("2026-07-10T00:00:01Z"),
  ), null);
  assert.equal(await verifyAuthorityApprovalRequest(
    "request-1",
    "conversation-1",
    async () => request({ request_id: "request-2" }),
    Date.parse("2026-07-10T00:00:01Z"),
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
