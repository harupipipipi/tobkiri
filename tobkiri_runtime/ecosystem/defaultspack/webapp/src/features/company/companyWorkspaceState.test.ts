import test from "node:test";
import assert from "node:assert/strict";

import {
  CompanyLoadGate,
  committedCompanyAction,
  discardSettingsDraft,
  editSettingsDraft,
  pendingCompanyAction,
  rejectedCompanyAction,
  shouldRunCompanyPoll,
  updateSettingsDraft,
} from "./companyWorkspaceState";

test("company loads ignore stale and cancelled generations", () => {
  const gate = new CompanyLoadGate();
  const slowCompany = gate.begin("company-a", "general");
  const fastCompany = gate.begin("company-b", "ops");

  assert.equal(slowCompany.signal.aborted, true);
  assert.equal(gate.isCurrent(slowCompany), false);
  assert.equal(gate.isCurrent(fastCompany), true);

  gate.cancel();
  assert.equal(gate.isCurrent(fastCompany), false);
});

test("settings poll preserves a dirty draft and flags remote conflict", () => {
  const initial = {
    baseline: { task_policy: "queued", mentions_create_tasks: true },
    draft: { task_policy: "queued", mentions_create_tasks: true },
    dirty: false,
    conflict: false,
  };
  const edited = editSettingsDraft(initial, "task_policy", "manual");
  const polled = updateSettingsDraft(edited, {
    task_policy: "queued",
    mentions_create_tasks: false,
  });

  assert.equal(polled.draft.task_policy, "manual");
  assert.equal(polled.dirty, true);
  assert.equal(polled.conflict, true);
  assert.deepEqual(discardSettingsDraft(polled, {
    task_policy: "queued",
    mentions_create_tasks: false,
  }).draft, {
    task_policy: "queued",
    mentions_create_tasks: false,
  });
});

test("background poll pauses for edits, offline state, and active mutations", () => {
  assert.equal(shouldRunCompanyPoll({ visible: true, online: true, editing: false, mutationPending: false }), true);
  assert.equal(shouldRunCompanyPoll({ visible: false, online: true, editing: false, mutationPending: false }), false);
  assert.equal(shouldRunCompanyPoll({ visible: true, online: false, editing: false, mutationPending: false }), false);
  assert.equal(shouldRunCompanyPoll({ visible: true, online: true, editing: true, mutationPending: false }), false);
  assert.equal(shouldRunCompanyPoll({ visible: true, online: true, editing: false, mutationPending: true }), false);
});

test("mutation action state distinguishes pending, committed, and ambiguous rejection", () => {
  assert.deepEqual(pendingCompanyAction("send-1"), {
    phase: "pending",
    operationId: "send-1",
    message: "Saving…",
  });
  assert.equal(committedCompanyAction("send-1", 42).updatedAt, 42);
  const rejected = rejectedCompanyAction("send-1", new Error("network timeout"));
  assert.equal(rejected.phase, "rejected");
  assert.equal(rejected.operationId, "send-1");
  assert.equal(rejected.retryable, true);
  assert.equal(rejected.ambiguous, true);
});
