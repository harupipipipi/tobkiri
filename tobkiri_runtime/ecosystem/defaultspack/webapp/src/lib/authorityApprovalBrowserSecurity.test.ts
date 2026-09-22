import assert from "node:assert/strict";
import test from "node:test";

import { BrowserApprovalExchangeSession, type BrowserApprovalExchangeTransport } from "./authorityApprovalBrowserExchange";
import { browserAuthorityApprovalPath, safeSameOriginApprovalPath, scrubLegacyApprovalUrl } from "./authorityApprovalBrowserToken";

const ORIGIN = "https://rumi.invalid";
const FAKE_CODE = "fake-one-time-code-never-a-secret";

test("approval paths contain request routing only and omit unsafe return targets", () => {
  assert.equal(browserAuthorityApprovalPath("fake-request", "/ambient-debug?chat=c1"), "/approval?request_id=fake-request&return_to=%2Fambient-debug%3Fchat%3Dc1");
  assert.equal(browserAuthorityApprovalPath("fake-request", "https://external.invalid/collect"), "/approval?request_id=fake-request");
  assert.doesNotMatch(browserAuthorityApprovalPath("fake-request"), /token|code|nonce/i);
});

test("safe approval navigation rejects adversarial and credential-bearing targets", () => {
  const rejected = [
    "https://external.invalid/collect", "//external.invalid/collect", "\\\\external.invalid\\collect",
    "/\\external.invalid/collect", "javascript:alert(1)", "data:text/html,unsafe",
    "blob:https://rumi.invalid/fake", "https://user:pass@rumi.invalid/approval",
    "https://rumi.invalid:bad/approval", "/approval?browser_approval_token=fake-leaked-value",
    "/approval#/route?approval_browser_token=fake-leaked-value", "/approval\u0000?request_id=fake",
  ];
  for (const target of rejected) assert.equal(safeSameOriginApprovalPath(target, ORIGIN), null, target);
  assert.equal(safeSameOriginApprovalPath("https://rumi.invalid/approval?request_id=fake", ORIGIN), "/approval?request_id=fake");
});

test("legacy URL cleanup removes query and fragment aliases without retaining values", () => {
  const result = scrubLegacyApprovalUrl(`${ORIGIN}/approval?request_id=fake&browser_approval_token=fake-query#/route?x=1&browserApprovalToken=fake-hash`, ORIGIN);
  assert.deepEqual(result, { cleanedPath: "/approval?request_id=fake#/route?x=1", changed: true, rejected: false });
  assert.doesNotMatch(JSON.stringify(result), /fake-query|fake-hash/);
  assert.equal(scrubLegacyApprovalUrl("//external.invalid/path", ORIGIN).rejected, true);
  assert.equal(scrubLegacyApprovalUrl("/bad\\path", ORIGIN).rejected, true);
});

function fakeContext(requestId: string) {
  return {
    request_id: requestId,
    ui_operator: {
      version: 1, kind: "ui_operator" as const, origin: ORIGIN, window_label: "fake-window",
      request_id: requestId, issued_at: 1, expires_at: 2, nonce: "fake-operator-nonce", signature: "fake-signature",
    },
  };
}

test("exchange binds its audience and settles once without URL or storage transport", async () => {
  const bindings: Array<Record<string, string>> = [];
  let redeemCount = 0;
  const transport: BrowserApprovalExchangeTransport = {
    async issue(binding) { bindings.push(binding); return { request_id: binding.request_id, exchange_code: FAKE_CODE, exchange_id: "fake-exchange-id", expires_at: 1893456060 }; },
    async redeem(binding, code) { redeemCount += 1; assert.equal(code, FAKE_CODE); return fakeContext(binding.request_id); },
    async revoke() { return { revoked: true }; },
  };
  const session = new BrowserApprovalExchangeSession(transport, () => Date.parse("2030-01-01T00:00:00Z"), { deviceId: "fake-device", windowId: "fake-window", origin: ORIGIN });
  assert.equal((await session.context("fake-request")).request_id, "fake-request");
  assert.equal(session.state, "settled");
  assert.equal(redeemCount, 1);
  assert.deepEqual({ ...bindings[0], nonce: "redacted" }, { request_id: "fake-request", device_id: "fake-device", window_id: "fake-window", origin: ORIGIN, nonce: "redacted" });
  assert.match(bindings[0]?.nonce ?? "", /^nonce-/);
});

test("expired exchange never redeems", async () => {
  let redeemed = false;
  const transport: BrowserApprovalExchangeTransport = {
    async issue(binding) { return { request_id: binding.request_id, exchange_code: FAKE_CODE, exchange_id: "fake-exchange-id", expires_at: 1893455999 }; },
    async redeem() { redeemed = true; return fakeContext("fake-request"); },
    async revoke() { return { revoked: true }; },
  };
  const session = new BrowserApprovalExchangeSession(transport, () => Date.parse("2030-01-01T00:00:00Z"), { deviceId: "fake-device", windowId: "fake-window", origin: ORIGIN });
  await assert.rejects(session.context("fake-request"), /EXPIRED/);
  assert.equal(session.state, "expired");
  assert.equal(redeemed, false);
});

test("wrong-request response and concurrent replacement fail closed", async () => {
  const resolvers: Array<(value: { request_id: string; exchange_code: string; exchange_id: string; expires_at: number }) => void> = [];
  let redeemCount = 0;
  let revokeCount = 0;
  const transport: BrowserApprovalExchangeTransport = {
    issue() { return new Promise((resolve) => resolvers.push(resolve)); },
    async redeem(binding) { redeemCount += 1; return fakeContext(binding.request_id === "second" ? "wrong" : binding.request_id); },
    async revoke() { revokeCount += 1; return { revoked: true }; },
  };
  const session = new BrowserApprovalExchangeSession(transport, () => Date.parse("2030-01-01T00:00:00Z"), { deviceId: "fake-device", windowId: "fake-window", origin: ORIGIN });
  const first = session.context("first");
  const second = session.context("second");
  await new Promise((resolve) => setImmediate(resolve));
  resolvers[0]?.({ request_id: "first", exchange_code: "fake-first", exchange_id: "fake-first-id", expires_at: 1893456060 });
  resolvers[1]?.({ request_id: "second", exchange_code: "fake-second", exchange_id: "fake-second-id", expires_at: 1893456060 });
  await assert.rejects(first, /REVOKED/);
  await assert.rejects(second, /WRONG_REQUEST/);
  assert.equal(redeemCount, 1);
  assert.equal(revokeCount, 1);
});
