import assert from "node:assert/strict";
import test from "node:test";

import {
  bridgeFailureStatus,
  buildSafeDiagnostics,
  nextScheduledAttempt,
  safeServerOrigin,
  sanitizeConnectionStatus
} from "../../ecosystem/defaultspack/browser_extensions/rumi_browser_companion/status_contract.mjs";

test("status sanitization drops raw errors, envelopes, and credentials", () => {
  const status = sanitizeConnectionStatus({
    ok: false,
    state: "bridge_error",
    message: "Authorization: Bearer super-secret",
    error: { request: { pairing_token: "super-secret" } },
    serverUrl: "http://user:password@127.0.0.1:8766/private",
    pairingToken: "super-secret"
  });

  assert.deepEqual(status, {
    ok: false,
    state: "error",
    code: "BRIDGE_UNAVAILABLE",
    action: "retry"
  });
  assert.doesNotMatch(JSON.stringify(status), /secret|Authorization|private/);
});

test("server origin removes credentials, paths, and unsupported schemes", () => {
  assert.equal(safeServerOrigin("http://user:token@127.0.0.1:8766/private?q=secret"), "http://127.0.0.1:8766");
  assert.equal(safeServerOrigin("file:///tmp/bridge"), "");
  assert.equal(
    sanitizeConnectionStatus({ state: "connected", serverOrigin: "https://user:token@example.test/a" }).serverOrigin,
    "https://example.test"
  );
});

test("bridge failures map to stable actionable states without returning error text", () => {
  assert.deepEqual(bridgeFailureStatus({ responseStatus: 401 }), {
    state: "unauthorized",
    code: "PAIRING_REQUIRED",
    action: "re_pair"
  });
  assert.deepEqual(bridgeFailureStatus({ error: new Error("Permission denied") }), {
    state: "permission_blocked",
    code: "BROWSER_PERMISSION_BLOCKED",
    action: "open_permissions"
  });
  assert.deepEqual(bridgeFailureStatus({ error: new Error("Failed to fetch") }), {
    state: "offline",
    code: "NETWORK_UNAVAILABLE",
    action: "retry"
  });
});

test("safe diagnostics contain only operational state and timestamps", () => {
  const diagnostics = buildSafeDiagnostics({
    state: "unauthorized",
    code: "PAIRING_REQUIRED",
    action: "re_pair",
    pairingToken: "top-secret",
    clientLabel: "Personal profile",
    serverOrigin: "http://127.0.0.1:8766",
    lastAttemptAt: "2026-08-27T00:00:00.000Z",
    pollIntervalMinutes: 5
  });

  assert.doesNotMatch(diagnostics, /top-secret|Personal profile|127\.0\.0\.1/);
  assert.deepEqual(JSON.parse(diagnostics), {
    state: "unauthorized",
    code: "PAIRING_REQUIRED",
    updated_at: null,
    last_attempt_at: "2026-08-27T00:00:00.000Z",
    last_success_at: null,
    polling_minutes: 5,
    companion_version: "1"
  });
});

test("next scheduled attempt derives from the last attempt and interval", () => {
  assert.equal(
    nextScheduledAttempt({
      state: "connected",
      lastAttemptAt: "2026-08-27T00:00:00.000Z",
      pollIntervalMinutes: 3
    }),
    "2026-08-27T00:03:00.000Z"
  );
  assert.equal(nextScheduledAttempt({ state: "paused", pollIntervalMinutes: 3 }), "");
});
