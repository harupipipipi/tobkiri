import assert from "node:assert/strict";
import test from "node:test";

import { classifyChatLink, openChatLink } from "./chatLinkPolicy";

const ORIGIN = "http://127.0.0.1:38766";

test("internal Rumi routes are normalized and allowed", () => {
  assert.deepEqual(classifyChatLink("/settings?tab=tools#top", "Settings", ORIGIN), {
    kind: "internal", allowed: true, requiresStrongConfirmation: false,
    normalizedUrl: "/settings?tab=tools#top", host: "127.0.0.1", textMismatch: false,
  });
});

test("HTTPS destinations expose normalized host and mismatched visible target", () => {
  const safe = classifyChatLink("https://example.com/a", "Example", ORIGIN);
  assert.equal(safe.kind, "web");
  assert.equal(safe.host, "example.com");
  assert.equal(safe.requiresStrongConfirmation, false);
  const mismatch = classifyChatLink("https://evil.example/a", "https://example.com/a", ORIGIN);
  assert.equal(mismatch.textMismatch, true);
  assert.equal(mismatch.requiresStrongConfirmation, true);
});

test("credentials, custom and active schemes fail closed", () => {
  for (const target of ["https://user:pass@example.com", "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "myapp://open"]) {
    assert.equal(classifyChatLink(target, "open", ORIGIN).allowed, false, target);
  }
});

test("localhost, private IPv4, private IPv6, and IPv4-mapped IPv6 fail closed", () => {
  for (const target of ["http://localhost/a", "http://127.1.2.3/a", "http://10.2.3.4", "http://172.20.1.1", "http://192.168.1.2", "http://[::1]/", "http://[fd00::1]/", "http://[::ffff:127.0.0.1]/"]) {
    const decision = classifyChatLink(target, target, ORIGIN);
    assert.equal(decision.kind, "local", target);
    assert.equal(decision.allowed, false, target);
  }
});

test("punycode and downloads require strong review", () => {
  const idn = classifyChatLink("https://xn--pple-43d.com", "Apple", ORIGIN);
  assert.equal(idn.requiresStrongConfirmation, true);
  const download = classifyChatLink("https://downloads.example/app.pkg?build=1", "Installer", ORIGIN);
  assert.equal(download.kind, "download");
  assert.equal(download.requiresStrongConfirmation, true);
});

test("malformed, oversized and control-bearing targets fail closed", () => {
  assert.equal(classifyChatLink("http://[", "bad", ORIGIN).kind, "malformed");
  assert.equal(classifyChatLink(`https://example.com/${"x".repeat(9000)}`, "big", ORIGIN).allowed, false);
  assert.equal(classifyChatLink("https://example.com/\nnext", "bad", ORIGIN).allowed, false);
});

test("external opener uses a new isolated context and reports popup blocking", () => {
  const decision = classifyChatLink("https://example.com", "Example", ORIGIN);
  const calls: string[][] = [];
  assert.equal(openChatLink(decision, (...args) => { calls.push(args); return {} as Window; }), true);
  assert.deepEqual(calls, [["https://example.com/", "_blank", "noopener,noreferrer"]]);
  assert.equal(openChatLink(decision, () => null), false);
});
