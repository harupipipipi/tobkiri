import assert from "node:assert/strict";
import test from "node:test";
import type { ComposerCommandItem } from "./api";
import { approvalCommandOwnsDraft, completeApprovalCommandDraft } from "./approvalCommandDraft";
const command = (action: string) => ({ execution: { type: "frontend", action } } as ComposerCommandItem);

test("only registered frontend approval actions defer renderer draft clearing", () => {
  assert.equal(approvalCommandOwnsDraft(command("toggle_yolo")), true);
  assert.equal(approvalCommandOwnsDraft(command("toggle_ultra_yolo")), true);
  assert.equal(approvalCommandOwnsDraft(command("open_model_picker")), false);
  assert.equal(approvalCommandOwnsDraft(undefined), false);
  assert.equal(approvalCommandOwnsDraft({ id: "yolo", execution: { type: "rumi_function" } } as ComposerCommandItem), false);
});
test("denied approval changes preserve the exact submitted draft", async () => {
  let input = "/yolo";
  await completeApprovalCommandDraft(async () => false, () => true, () => { input = ""; });
  assert.equal(input, "/yolo");
});
test("successful approval changes wait for acknowledgment before clearing", async () => {
  let input = "/yolo off";
  let settle!: (value: boolean) => void;
  const pending = new Promise<boolean>((resolve) => { settle = resolve; });
  const completion = completeApprovalCommandDraft(() => pending, () => true, () => { input = ""; });
  assert.equal(input, "/yolo off");
  settle(true);
  await completion;
  assert.equal(input, "");
});
test("a changed draft or conversation owner is never cleared by late approval completion", async () => {
  for (const current of [false, true]) {
    let clears = 0;
    await completeApprovalCommandDraft(async () => true, () => current, () => { clears++; });
    assert.equal(clears, current ? 1 : 0);
  }
});
test("failed persistence never clears the draft", async () => {
  let cleared = false;
  await assert.rejects(completeApprovalCommandDraft(async () => { throw new Error("save failed"); }, () => true, () => { cleared = true; }), /save failed/);
  assert.equal(cleared, false);
});

test("an unconfirmed or approval-pending command result cannot clear a draft", async () => {
  let cleared = false;
  await completeApprovalCommandDraft(async () => undefined, () => true, () => { cleared = true; });
  assert.equal(cleared, false);
});
