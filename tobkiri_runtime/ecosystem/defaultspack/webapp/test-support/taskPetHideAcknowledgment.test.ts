import assert from "node:assert/strict";
import test from "node:test";
import { taskPetHideAcknowledgment, type TaskPetHideEvidence } from "./taskPetHideAcknowledgment";
const fulfilled = { status: "fulfilled", value: undefined } as const;
const evidence: TaskPetHideEvidence = { targetIsHide: true, trustedPrimaryClick: true,
  matchingHideMessage: true, childClosed: true, parentOpen: true, persistedHidden: true };
const closed = { status: "rejected", reason: new Error("locator.click: Target page, context or browser has been closed\nCall log:\n  performing click action") } as const;

test("a normal acknowledged click and its exact verified close race both prove Hide", () => {
  assert.equal(taskPetHideAcknowledgment(fulfilled, fulfilled, evidence), "click-acknowledged");
  assert.equal(taskPetHideAcknowledgment(closed, fulfilled, evidence), "target-closed-after-verified-hide");
});

test("wrong target, untrusted click, missing owner hide, open child, closed parent or missing persistence never acknowledge", () => {
  for (const key of Object.keys(evidence) as Array<keyof TaskPetHideEvidence>) {
    for (const click of [fulfilled, closed]) {
      assert.throws(() => taskPetHideAcknowledgment(click, fulfilled, { ...evidence, [key]: false }), new RegExp(key));
    }
  }
});

test("unrelated click errors are rethrown unchanged even when the child closed", () => {
  for (const reason of [new Error("locator.click: Timeout 30000ms exceeded"), new Error("locator.click: intercepted by overlay"),
    new Error("page.evaluate: Target page, context or browser has been closed"),
    new Error("Other failure mentioning Target page, context or browser has been closed"), "Target page, context or browser has been closed"]) {
    assert.throws(() => taskPetHideAcknowledgment({ status: "rejected", reason }, fulfilled, evidence), (error) => error === reason);
  }
});

test("the prearmed child close event must complete, even when the click acknowledged", () => {
  const reason = new Error("Close event was not observed");
  for (const click of [fulfilled, closed]) {
    assert.throws(() => taskPetHideAcknowledgment(click, { status: "rejected", reason }, evidence), (error) => error === reason);
  }
});
