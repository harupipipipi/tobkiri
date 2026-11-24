import assert from "node:assert/strict";
import test from "node:test";
import { api } from "./api";

const target = { profile_id: "profile-1", conversation_id: "chat-1", workspace_id: "workspace-1", activation_id: "activation-1" };
const projection = () => ({ ...target, available_modes: ["ask", "agent", "full"], active_mode: "ask",
  reason: "", activation_id: "activation-1", capture_digest: `sha256:${"a".repeat(64)}`,
  expires_at: Date.now() / 1000 + 60 });
const envelope = (value: unknown) => new Response(JSON.stringify({ success: true, data: value }), {
  headers: { "Content-Type": "application/json" },
});

test("capabilities use canonical captured route, exact applicability and cancellation signal", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  const controller = new AbortController();
  globalThis.fetch = async (url, options) => {
    assert.equal(String(url), "/api/contracts/defaultspack/POST%20%2Fapi%2Fhost%2Faction-approval-policy%2Fcapabilities");
    assert.equal(options?.method, "POST");
    assert.equal(options?.cache, "no-store");
    assert.equal(options?.signal, controller.signal);
    assert.deepEqual(JSON.parse(String(options?.body)), { conversation_id: "chat-1", workspace_id: "workspace-1" });
    return envelope(projection());
  };
  const result = await api.actionApprovalPolicyCapabilities(target, { signal: controller.signal });
  assert.deepEqual(result.available_modes, ["ask", "agent", "full"]);
  assert.ok(Object.isFrozen(result));
});

test("response cannot rebind workspace or add an approved flag", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  for (const change of [{ workspace_id: "other" }, { approved: true }]) {
    globalThis.fetch = async () => envelope({ ...projection(), ...change });
    await assert.rejects(api.actionApprovalPolicyCapabilities(target));
  }
});

test("aborted and invalid applicability never fetch", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => { assert.fail("invalid applicability must not fetch"); };
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(api.actionApprovalPolicyCapabilities(target, { signal: controller.signal }));
  await assert.rejects(api.actionApprovalPolicyCapabilities({ ...target, workspace_id: "" }));
});


test("new conversation draft queries current accepted workspace without creating a conversation", async (context) => {
  const original = globalThis.fetch;
  context.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async (_url, options) => {
    assert.deepEqual(JSON.parse(String(options?.body)), { conversation_id: null, workspace_id: "workspace-1" });
    return envelope({ ...projection(), conversation_id: null });
  };
  assert.equal((await api.actionApprovalPolicyCapabilities({ ...target, conversation_id: null })).active_mode, "ask");
});
