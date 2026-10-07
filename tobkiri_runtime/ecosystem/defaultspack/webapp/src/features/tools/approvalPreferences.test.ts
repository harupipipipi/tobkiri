import assert from "node:assert/strict";
import test from "node:test";
import { captureApprovalMode, readApprovalPreferences, availableApprovalModes, captureSupportedApprovalMode } from "./approvalPreferences";

test("missing and malformed preferences show the control and require human approval", () => {
  assert.deepEqual(readApprovalPreferences(), { controlVisible: true, selectedMode: "ask", fixedMode: "ask" });
  assert.equal(captureApprovalMode({ show_action_approval_control: "false", action_approval_mode: "bad" }), "ask");
});

test("hidden control applies mandatory fixed preference and preserves selected preference", () => {
  const tools = { show_action_approval_control: false, action_approval_mode: "full", fixed_action_approval_mode: "agent", legacy: "preserved" };
  assert.equal(captureApprovalMode(tools), "agent");
  assert.equal(readApprovalPreferences(tools).selectedMode, "full");
  assert.equal(captureApprovalMode({ ...tools, show_action_approval_control: true }), "full");
  assert.equal(tools.legacy, "preserved");
});

test("captured mode survives subsequent edits and canonical serialization reload", () => {
  const tools = { show_action_approval_control: false, fixed_action_approval_mode: "agent" };
  const captured = captureApprovalMode(tools);
  tools.fixed_action_approval_mode = "ask";
  assert.equal(captured, "agent");
  assert.equal(captureApprovalMode(JSON.parse(JSON.stringify(tools))), "ask");
});

test("elevated preferences do not make unsupported Host modes available", () => {
  assert.deepEqual(availableApprovalModes(), ["ask"]);
  assert.deepEqual(availableApprovalModes(["full", "invalid", "full"]), ["ask", "full"]);
  assert.equal(captureApprovalMode({ action_approval_mode: "full" }), "full");
});

test("unsupported fixed mode stops submission without silently becoming human approval", () => {
  const tools = { show_action_approval_control: false, fixed_action_approval_mode: "full" };
  assert.throws(() => captureSupportedApprovalMode(tools), /現在利用できません/);
  assert.equal(captureSupportedApprovalMode({}), "ask");
  assert.equal(captureSupportedApprovalMode(tools, ["full"]), "full");
});
