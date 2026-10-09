import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ActionApprovalControl } from "./ActionApprovalControl";
import { api, type SavedTurnRequest } from "../../lib/api";
import { captureApprovalMode, readApprovalPreferences, availableApprovalModes, captureSupportedApprovalMode, requestApprovalPreferenceChange } from "./approvalPreferences";

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

test("preference changes reject unavailable and fixed modes without changing settings", async () => {
  for (const tools of [
    { action_approval_mode: "ask", ultra_yolo: true },
    { show_action_approval_control: false, action_approval_mode: "ask", fixed_action_approval_mode: "ask" },
  ]) {
    const before = structuredClone(tools);
    let message = "";
    assert.equal(await requestApprovalPreferenceChange(tools, "full", [],
      () => assert.fail("unsupported preferences must not be saved"),
      (error) => { message = error; }), false);
    assert.match(message, /利用できません|固定されています/);
    assert.deepEqual(tools, before);
    assert.equal(captureApprovalMode(tools), "ask");
  }
});

test("human approval remains selectable after elevated capabilities expire", async () => {
  const tools: Record<string, unknown> = { action_approval_mode: "full" };
  assert.throws(() => captureSupportedApprovalMode(tools, []), /利用できません/);
  assert.equal(await requestApprovalPreferenceChange(tools, "ask", [],
    (mode) => { tools.action_approval_mode = mode; },
    () => assert.fail("downgrading to human approval is always available")), true);
  assert.equal(captureSupportedApprovalMode(tools, []), "ask");
});

test("accepted full and ask preferences agree with the chip and actual saved-turn transport", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => { globalThis.fetch = originalFetch; });
  const tools: Record<string, unknown> = {};
  const requests: SavedTurnRequest[] = [];
  globalThis.fetch = async (_url, init) => {
    const request = JSON.parse(String(init?.body)).request as SavedTurnRequest;
    requests.push(request);
    return new Response(JSON.stringify({ success: true, data: {
      status: "reconciliation_required", turn: { id: request.turn_id, conversation_id: request.conversation_id },
    } }));
  };
  for (const mode of ["full", "ask"] as const) {
    assert.equal(await requestApprovalPreferenceChange(tools, mode, ["ask", "full"],
      (acceptedMode) => { tools.action_approval_mode = acceptedMode; },
      () => assert.fail("supported preference should be saved")), true);
    const preferences = readApprovalPreferences(tools);
    const chipMode = preferences.controlVisible ? preferences.selectedMode : preferences.fixedMode;
    const html = renderToStaticMarkup(createElement(ActionApprovalControl, {
      mode: chipMode, availableModes: ["ask", "full"], surfaceClassName: "", onModeChange: () => {},
    }));
    assert.ok(html.includes(mode === "full" ? "フル" : "承認"));
    await api.startSavedTurn({
      turn_id: `turn-yolo-${mode}`, conversation_id: "conversation-1", conversation_revision: 1,
      content: [{ type: "text", text: "Keep the submitted text" }],
      action_approval_mode: captureSupportedApprovalMode(tools, ["ask", "full"]),
    });
    assert.equal(requests.at(-1)?.action_approval_mode, chipMode);
    assert.deepEqual(Object.keys(requests.at(-1)!).sort(),
      ["turn_id", "conversation_id", "conversation_revision", "content", "action_approval_mode"].sort());
  }
});

test("failed persistence never publishes a full preference and keeps the command unsuccessful", async () => {
  const tools = { action_approval_mode: "ask" };
  let message = "";
  assert.equal(await requestApprovalPreferenceChange(tools, "full", ["full"],
    async () => { throw new Error("Settings write failed"); },
    (error) => { message = error; }), false);
  assert.match(message, /保存できません/);
  assert.equal(captureSupportedApprovalMode(tools, ["full"]), "ask");
});

test("pending persistence does not optimistically change the chip or captured turn preference", async () => {
  const tools = { action_approval_mode: "ask" };
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const change = requestApprovalPreferenceChange(tools, "full", ["full"], async (mode) => {
    await pending;
    tools.action_approval_mode = mode;
  }, () => assert.fail("supported preference should save"));
  assert.equal(captureApprovalMode(tools), "ask");
  release();
  assert.equal(await change, true);
  assert.equal(captureSupportedApprovalMode(tools, ["full"]), "full");
});

test("all ChatApp full-access entry points share canonical preference and guarded toggle wiring", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../../App.tsx", import.meta.url), "utf8");
  assert.match(source, /const handleToggleFullAccess = \(enabled\?: unknown\) => handleActionApprovalModeChange\(/);
  assert.match(source, /parseCommandBoolean\(enabled, captureApprovalMode\(settingsValuesRef\.current\.tools\) !== "full"\)/);
  assert.match(source, /case "toggle_yolo":[\s\S]*?case "toggle_ultra_yolo":[\s\S]*?return handleToggleFullAccess\(args.enabled\)/);
  assert.match(source, /yoloMode=\{actionApprovalMode === "full"\}/);
  assert.match(source, /onToggleYolo=\{\(\) => \{ void handleToggleFullAccess\(\); \}\}/);
  assert.match(source, /approval_preference=\$\{captureApprovalMode\(settingsValuesRef.current.tools\)\}/);
  const appBody = source.slice(source.indexOf("export function ChatApp()"));
  assert.doesNotMatch(appBody, /rumi-(?:ultra-)?yolo-mode|setFullAccessEnabled/);
  // Recognized commands keep their own confirmation path; normal messages must
  // still capture supported approval before the first composer mutation.
  const submit = source.slice(source.indexOf("  const handleSubmit ="));
  assert.ok(submit.indexOf("await executeComposerCommand") < submit.indexOf("captureSupportedApprovalMode("));
  assert.ok(submit.indexOf("captureSupportedApprovalMode(") < submit.indexOf('setInput("");', submit.indexOf("const trimmedInput")));
  assert.match(submit, /recoveryDraftStateRef.current.input === inputForSubmit/);
});
