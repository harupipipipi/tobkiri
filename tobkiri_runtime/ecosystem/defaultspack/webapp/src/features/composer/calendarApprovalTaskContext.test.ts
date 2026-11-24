import assert from "node:assert/strict";
import test from "node:test";
import { captureSupportedApprovalMode } from "../tools/approvalPreferences";
import { parseApprovalPolicyCapabilities } from "../tools/approvalPolicyCapabilities";
import { calendarApprovalPolicyTarget, captureCalendarApprovalTaskContext } from "./calendarApprovalTaskContext";

const NOW = 1_700_000_000;
const input = { profileId: "p", activationId: "a", workspaceId: "workspace-one", conversationId: "existing", useCurrentChat: false };
const target = calendarApprovalPolicyTarget(input)!;
const support = parseApprovalPolicyCapabilities({ ...target, active_mode: "ask", available_modes: ["ask", "agent", "full"],
  reason: "", capture_digest: `sha256:${"a".repeat(64)}`, expires_at: NOW + 30 }, target, NOW);

test("a new scheduled destination uses null rather than the visible active conversation", () => {
  assert.equal(target.conversation_id, null);
  assert.equal(calendarApprovalPolicyTarget({ ...input, useCurrentChat: true })?.conversation_id, "existing");
  assert.equal(calendarApprovalPolicyTarget({ ...input, workspaceId: null }), null);
});

test("mode and confirmed workspace are captured once and remain preferences after settings change", () => {
  let calls = 0;
  let preferences = { show_action_approval_control: false, fixed_action_approval_mode: "full" };
  const captured = captureCalendarApprovalTaskContext(target, support, (modes) => {
    calls += 1;
    return captureSupportedApprovalMode(preferences, modes);
  }, NOW);
  preferences = { show_action_approval_control: false, fixed_action_approval_mode: "ask" };
  assert.equal(calls, 1);
  assert.deepEqual(captured, { mode: "full", workspaceId: "workspace-one" });
  assert.equal(Object.isFrozen(captured), true);
  assert.deepEqual(Object.keys(captured).sort(), ["mode", "workspaceId"]);
});

test("expired or rebound Host support cannot enable a new elevated task", () => {
  for (const stale of [{ ...support, expires_at: NOW }, { ...support, profile_id: "other" },
    { ...support, activation_id: "other" }, { ...support, workspace_id: "other" },
    { ...support, conversation_id: "existing" }, null]) {
    assert.throws(() => captureCalendarApprovalTaskContext(target, stale,
      (modes) => captureSupportedApprovalMode({ action_approval_mode: "agent" }, modes), NOW), /現在利用できません/);
  }
});

test("ask works without a workspace while a hidden elevated fixed preference retains the draft", () => {
  assert.deepEqual(captureCalendarApprovalTaskContext(null, support,
    (modes) => captureSupportedApprovalMode({}, modes), NOW), { mode: "ask", workspaceId: null });
  assert.throws(() => captureCalendarApprovalTaskContext(null, null,
    (modes) => captureSupportedApprovalMode({ show_action_approval_control: false, fixed_action_approval_mode: "full" }, modes), NOW), /下書きは保持/);
  assert.throws(() => captureCalendarApprovalTaskContext(null, null, () => "full", NOW), /確認済みの作業領域/);
});
