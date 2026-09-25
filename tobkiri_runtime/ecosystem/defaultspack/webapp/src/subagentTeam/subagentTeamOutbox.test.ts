import test from "node:test";
import assert from "node:assert/strict";

import { api } from "../lib/api";

import {
  createOutboxItem,
  createLatestRequestGate,
  deliveryFailureState,
  outboxScopeKey,
  readOutbox,
  reconcileOutbox,
  transitionOutboxItem,
  writeOutbox,
  type OutboxStorage,
} from "./subagentTeamOutbox";

function memoryStorage(): OutboxStorage {
  const values = new Map<string, string>();
  return {
    getItem(key) {
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
    removeItem(key) {
      values.delete(key);
    },
  };
}

test("outbox persists exact scoped content and thread identity across reload", () => {
  const storage = memoryStorage();
  const scopeKey = outboxScopeKey("conversation-1", "company-1");
  const item = createOutboxItem({
    clientMessageId: "client-1",
    scopeKey,
    companyId: "company-1",
    conversationId: "conversation-1",
    thread: { type: "dm", id: "coder-kai" },
    channelId: "dm-coder-kai",
    content: "  exact draft @coder-kai  ",
    mentions: ["coder-kai"],
    state: "draft",
    now: "2026-08-28T00:00:00.000Z",
  });

  assert.equal(writeOutbox(storage, scopeKey, [item]), true);
  assert.deepEqual(readOutbox(storage, scopeKey), [item]);
  assert.deepEqual(readOutbox(storage, outboxScopeKey("conversation-2", null)), []);
});

test("outbox tracks sending, unknown, retry, failed, and cancelled per item", () => {
  const item = createOutboxItem({
    clientMessageId: "client-1",
    scopeKey: "scope",
    companyId: "company-1",
    conversationId: null,
    thread: { type: "channel", id: "ship-room" },
    channelId: "ship-room",
    content: "hello",
    mentions: [],
    state: "queued",
    now: "2026-08-28T00:00:00.000Z",
  });

  const sending = transitionOutboxItem(item, "sending", { now: "2026-08-28T00:00:01.000Z" });
  const unknown = transitionOutboxItem(sending, "unknown", { error: "timeout", now: "2026-08-28T00:00:02.000Z" });
  const retrying = transitionOutboxItem(unknown, "queued", { now: "2026-08-28T00:00:03.000Z" });
  const failed = transitionOutboxItem(retrying, "failed", { error: "offline", now: "2026-08-28T00:00:04.000Z" });
  const cancelled = transitionOutboxItem(failed, "cancelled", { now: "2026-08-28T00:00:05.000Z" });

  assert.equal(sending.state, "sending");
  assert.equal(unknown.state, "unknown");
  assert.equal(unknown.error, "timeout");
  assert.equal(retrying.attempts, 2);
  assert.equal(retrying.error, undefined);
  assert.equal(failed.state, "failed");
  assert.equal(cancelled.state, "cancelled");
});

test("server client id reconciliation removes duplicate optimistic items", () => {
  const item = createOutboxItem({
    clientMessageId: "client-1",
    scopeKey: "scope",
    companyId: "company-1",
    conversationId: null,
    thread: { type: "channel", id: "ship-room" },
    channelId: "ship-room",
    content: "hello",
    mentions: [],
    state: "unknown",
    now: "2026-08-28T00:00:00.000Z",
  });

  assert.deepEqual(reconcileOutbox([item], [{
    id: "server-1",
    company_id: "company-1",
    channel_id: "ship-room",
    sender_id: "user",
    content: "hello",
    metadata: { client_message_id: "client-1" },
  }]), []);
});

test("network and timeout failures remain unknown until status lookup", () => {
  assert.equal(deliveryFailureState(new Error("Failed to fetch")), "unknown");
  assert.equal(deliveryFailureState(new Error("request timed out")), "unknown");
  assert.equal(deliveryFailureState(new Error("TARGET_NOT_FOUND")), "failed");
});

test("latest request gate rejects a stale response after rapid switching", () => {
  const gate = createLatestRequestGate();
  const firstCompanyLoad = gate.begin();
  const secondCompanyLoad = gate.begin();

  assert.equal(gate.isCurrent(firstCompanyLoad), false);
  assert.equal(gate.isCurrent(secondCompanyLoad), true);
});

test("send and status lookup reuse one stable client message id", async () => {
  const originalFetch = globalThis.fetch;
  const bodies: Array<Record<string, unknown>> = [];
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
    const data = bodies.length === 1
      ? {
        message: {
          id: "server-1",
          company_id: "company-1",
          channel_id: "ship-room",
          sender_id: "user",
          content: "hello",
        },
      }
      : {
        client_message_id: "client-1",
        state: "committed",
        message: null,
      };
    return new Response(JSON.stringify({ status: "ok", data }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  try {
    await api.sendSubagentTeamMessage({
      companyId: "company-1",
      content: "hello",
      channel_id: "ship-room",
      client_message_id: "client-1",
    });
    await api.getSubagentTeamMessageStatus({
      companyId: "company-1",
      clientMessageId: "client-1",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(bodies.map((body) => body.action), ["send", "status"]);
  assert.deepEqual(
    bodies.map((body) => body.client_message_id),
    ["client-1", "client-1"],
  );
});
