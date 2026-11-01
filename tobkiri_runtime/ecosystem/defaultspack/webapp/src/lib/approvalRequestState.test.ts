import assert from "node:assert/strict";
import test from "node:test";

import { approvalRequestExpired, approvalUnavailableMessage } from "./approvalRequestState";

test("expiry boundary disables a pending approval at its deadline", () => {
  assert.equal(approvalRequestExpired(1_800_000_000, 1_799_999_999_999), false);
  assert.equal(approvalRequestExpired(1_800_000_000, 1_800_000_000_000), true);
});

test("authoritative terminal states explain the outcome without an ID mismatch", () => {
  assert.match(approvalUnavailableMessage("expired"), /期限切れ/);
  assert.match(approvalUnavailableMessage("stale"), /古く/);
  assert.match(approvalUnavailableMessage("denied"), /拒否/);
  for (const state of ["expired", "stale", "denied"]) {
    assert.doesNotMatch(approvalUnavailableMessage(state), /一致しません/);
  }
});
