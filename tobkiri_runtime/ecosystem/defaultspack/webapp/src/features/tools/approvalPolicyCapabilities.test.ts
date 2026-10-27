import assert from "node:assert/strict";
import test from "node:test";
import { loadApprovalPolicyCapabilities, parseApprovalPolicyCapabilities } from "./approvalPolicyCapabilities";

const target = { profile_id: "profile-1", conversation_id: "chat-1", workspace_id: "workspace-1", activation_id: "activation-1" };
const projection = () => ({ ...target, available_modes: ["ask", "agent", "full"], active_mode: "ask",
  reason: "", activation_id: "activation-1", capture_digest: `sha256:${"a".repeat(64)}`,
  expires_at: Date.now() / 1000 + 60 });

test("valid Host presentation projection is captured and cannot be mutated", () => {
  const input = projection();
  const result = parseApprovalPolicyCapabilities(input, target);
  input.available_modes.length = 0;
  assert.deepEqual(result.available_modes, ["ask", "agent", "full"]);
  assert.ok(Object.isFrozen(result));
  assert.ok(Object.isFrozen(result.available_modes));
});

test("expired, rebound or forged fields never advertise modes", () => {
  for (const change of [{ expires_at: 0 }, { expires_at: Date.now() / 1000 + 901 },
    { profile_id: "other" }, { conversation_id: "other" }, { workspace_id: "other" },
    { activation_id: "" }, { activation_id: "different-activation" }, { available_modes: ["ask", "full", "full"] },
    { available_modes: ["full"] }, { available_modes: ["ask", "trusted"] },
    { approved: true }, { capture_digest: "sha256:wrong" }, { reason: null }]) {
    assert.throws(() => parseApprovalPolicyCapabilities({ ...projection(), ...change }, target));
  }
});

test("projection retains revoked active mode alongside unavailable support", () => {
  const result = parseApprovalPolicyCapabilities({ ...projection(), active_mode: "full",
    available_modes: ["ask"], reason: "許可を取り消しました。" }, target);
  assert.equal(result.active_mode, "full");
  assert.deepEqual(result.available_modes, ["ask"]);
});

test("request uses original applicability when mutable draft changes while awaiting", async () => {
  const mutable = { ...target };
  let release!: (value: unknown) => void;
  const pending = loadApprovalPolicyCapabilities(mutable, (captured) => {
    assert.deepEqual(captured, target);
    assert.ok(Object.isFrozen(captured));
    return new Promise((resolve) => { release = resolve; });
  }, () => {});
  mutable.workspace_id = "different-draft";
  release(projection());
  assert.equal((await pending).workspace_id, target.workspace_id);
});

test("changed view and cancellation reject late results", async () => {
  for (const cancelled of [false, true]) {
    let release!: (value: unknown) => void;
    const controller = new AbortController();
    let current = true;
    const pending = loadApprovalPolicyCapabilities(target, () => new Promise((resolve) => {
      release = resolve;
    }), () => { if (!current) throw new Error("view changed"); }, controller.signal);
    if (cancelled) controller.abort(); else current = false;
    release(projection());
    await assert.rejects(pending);
  }
});


test("new draft support has no active elevated policy", () => {
  const draft = { ...target, conversation_id: null };
  const result = parseApprovalPolicyCapabilities({ ...projection(), conversation_id: null }, draft);
  assert.deepEqual(result.available_modes, ["ask", "agent", "full"]);
  assert.equal(result.active_mode, "ask");
  assert.throws(() => parseApprovalPolicyCapabilities({ ...projection(), conversation_id: null,
    active_mode: "full" }, draft));
  assert.throws(() => parseApprovalPolicyCapabilities({ ...projection(), conversation_id: "" }, draft));
});
