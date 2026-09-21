import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { MobilePairingApproval } from "../../components/MobilePairingApproval";
import { PairingRequestGate, pairingDecisionReason, pairingErrorCode, pairingSettlement } from "./mobilePairingReview";

test("close copy distinguishes keep pending, reject, and cancel", () => {
  const never = new Promise<never>(() => undefined);
  const html = renderToStaticMarkup(createElement(MobilePairingApproval, {
    pairingId: "pair-safe",
    api: {
      getPairingStatus: () => never,
      getPairingReview: () => never,
      approvePairing: () => never,
      rejectPairing: () => never,
    },
  }));
  assert.match(html, /閉じるだけでは拒否されません/);
  assert.match(html, /要求を拒否/);
  assert.match(html, /aria-label="閉じ方を確認"/);
  assert.doesNotMatch(html, /pairing.*token|pickup_secret/i);
});

test("request gate single-submits and rejects late responses after invalidation", () => {
  const gate = new PairingRequestGate();
  const first = gate.begin();
  assert.equal(typeof first, "number");
  assert.equal(gate.begin(), null);
  gate.invalidate();
  assert.equal(gate.finish(first!), false);
  const second = gate.begin();
  assert.equal(gate.finish(second!), true);
  assert.equal(gate.busy, false);
});

test("authoritative settlements and protocol errors normalize durably", () => {
  for (const status of ["approved", "rejected", "expired", "revoked"]) {
    assert.equal(pairingSettlement(status), status);
  }
  assert.equal(pairingSettlement("claimed"), null);
  assert.equal(pairingErrorCode(new Error("PAIRING_EXPIRED")), "expired");
  assert.equal(pairingErrorCode(new Error("pairing is already settled")), "already-settled");
  assert.equal(pairingDecisionReason("reject"), "rejected by desktop reviewer");
  assert.equal(pairingDecisionReason("cancel"), "pairing cancelled by desktop reviewer");
});
