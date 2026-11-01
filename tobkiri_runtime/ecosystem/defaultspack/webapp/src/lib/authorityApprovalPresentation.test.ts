import assert from "node:assert/strict";
import test from "node:test";

import type { InteractiveApprovalRequest } from "./api";
import {
  approvalAuthorityDetails,
  AUTHORITY_DETAIL_UNAVAILABLE,
} from "./authorityApprovalPresentation";

const request = (overrides: Partial<InteractiveApprovalRequest> = {}): InteractiveApprovalRequest => ({
  request_id: "approval-1",
  request_snapshot_digest: "a".repeat(64),
  state: "pending",
  expires_at: 123_456,
  typed_confirmation_required: false,
  typed_confirmation_digest: null,
  redacted_metadata: {},
  ...overrides,
});

test("shows exact authority target, scope, and one-shot usage", () => {
  const details = approvalAuthorityDetails(request({
    target_principal_id: "principal-sha256",
    base_scope: {
      capability: "terminal.execute",
      semantics_digest: "b".repeat(64),
      dimensions: { command: ["true"] },
      quotas: {},
      exact_request_digest: "c".repeat(64),
      opaque: true,
    },
    max_uses: 1,
    remaining_uses: 1,
  }));

  assert.equal(details.targetPrincipal, "principal-sha256");
  assert.match(details.baseScope, /"capability": "terminal\.execute"/);
  assert.match(details.baseScope, /"exact_request_digest"/);
  assert.equal(details.maxUses, "1");
  assert.equal(details.remainingUses, "1");
});

test("marks absent or invalid authority details unavailable", () => {
  const details = approvalAuthorityDetails(request({
    target_principal_id: " ",
    base_scope: null,
    max_uses: -1,
    remaining_uses: 0.5,
  }));

  assert.deepEqual(details, {
    targetPrincipal: AUTHORITY_DETAIL_UNAVAILABLE,
    baseScope: AUTHORITY_DETAIL_UNAVAILABLE,
    maxUses: AUTHORITY_DETAIL_UNAVAILABLE,
    remainingUses: AUTHORITY_DETAIL_UNAVAILABLE,
  });
});
