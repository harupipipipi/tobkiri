import assert from "node:assert/strict";
import test from "node:test";

import { api } from "./api";
import {
  CLIENT_DIAGNOSTIC_MAX_PAYLOAD_BYTES,
  CLIENT_DIAGNOSTIC_PRIVACY_STORAGE_KEY,
  CLIENT_DIAGNOSTIC_SCHEMA_VERSION,
  diagnosticFingerprint,
  normalizeDiagnosticStack,
  prepareClientDiagnostic,
  readClientDiagnosticPrivacyMode,
  redactDiagnosticText,
  reportClientDiagnostic,
  reportClientDiagnosticResult,
  sanitizeDiagnosticDetail,
  writeClientDiagnosticPrivacyMode,
  type ClientDiagnosticPayloadV2,
} from "./clientDiagnostics";

class PreferenceStorage {
  private values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
}

const RAW_TOKEN = ["sk", "diagnostic", "supersecretvalue"].join("-");
const RAW_EMAIL = ["private.user", "example.test"].join("@");
const RAW_PATH = ["", "Users", "alice", "workspace", "secret.ts"].join("/");
const RAW_URL = `https://internal.example.test/private?access_token=${RAW_TOKEN}#fragment`;

function serialized(value: unknown): string {
  return JSON.stringify(value);
}

function assertSensitiveValuesAbsent(value: unknown): void {
  const text = serialized(value);
  for (const sensitive of [RAW_TOKEN, RAW_EMAIL, RAW_PATH, RAW_URL, "private prompt text", "tool output secret"]) {
    assert.equal(text.includes(sensitive), false, `unexpected sensitive value: ${sensitive}`);
  }
}

test("diagnosticFingerprint is stable, opaque, and excludes raw context", () => {
  const input = {
    source: "window.error",
    category: "window_error",
    message: `Renderer crashed for ${RAW_EMAIL}`,
    conversationId: "conversation-private-1",
  };
  const first = diagnosticFingerprint(input);
  const second = diagnosticFingerprint(input);

  assert.equal(first, second);
  assert.match(first, /^diag_[a-f0-9]{8}$/);
  assert.equal(first.includes("conversation-private-1"), false);
  assert.equal(first.includes(RAW_EMAIL), false);
});

test("redactDiagnosticText removes credentials, URLs, emails, paths, auth headers, and opaque blobs", () => {
  const redacted = redactDiagnosticText([
    `Authorization: Bearer ${RAW_TOKEN}`,
    `api_key=${RAW_TOKEN}`,
    RAW_URL,
    RAW_EMAIL,
    RAW_PATH,
    "a".repeat(100),
  ].join(" | "));

  assertSensitiveValuesAbsent(redacted);
  assert.match(redacted, /\[(?:auth-header|credential|url|email|path|opaque)\]/);
});

test("sanitizeDiagnosticDetail allowlists fields and ignores arbitrary nested user content", () => {
  const circular: Record<string, unknown> = {
    name: "TypeError",
    code: "E_RENDER",
    filename: `https://rumi.test/src/App.tsx?token=${RAW_TOKEN}`,
    lineno: 42,
    colno: 7,
    stack: [
      `TypeError: private prompt text ${RAW_TOKEN}`,
      `    at render (${RAW_PATH}:42:7)`,
      "    at App (https://rumi.test/src/App.tsx?access_token=hidden:42:7)",
      "    at dependency (https://cdn.example.test/node_modules/pkg/index.js:1:1)",
    ].join("\n"),
    prompt: "private prompt text",
    messages: [{ role: "user", content: "private prompt text" }],
    tool_args: { token: RAW_TOKEN },
    tool_result: "tool output secret",
    authorization: `Bearer ${RAW_TOKEN}`,
    provider_payload: { email: RAW_EMAIL },
  };
  circular.self = circular;

  const detail = sanitizeDiagnosticDetail(circular);

  assert.deepEqual(Object.keys(detail).sort(), [
    "column",
    "error_code",
    "error_name",
    "line",
    "route",
    "stack",
  ]);
  assert.equal(detail.route, "/src/App.tsx");
  assert.equal(detail.line, 42);
  assert.equal(detail.column, 7);
  assert.match(detail.stack ?? "", /App/);
  assert.doesNotMatch(detail.stack ?? "", /node_modules|private prompt|access_token|Users\/alice/);
  assertSensitiveValuesAbsent(detail);
});

test("normalizeDiagnosticStack keeps bounded application frames only", () => {
  const stack = normalizeDiagnosticStack([
    `Error: ${RAW_TOKEN}`,
    "    at App (https://rumi.test/src/App.tsx?token=secret:10:2)",
    "    at helper (https://rumi.test/assets/index.js#secret:20:3)",
    "    at dependency (https://cdn.test/node_modules/pkg/index.js:1:1)",
    `    at local (${RAW_PATH}:3:4)`,
  ].join("\n"));

  assert.match(stack ?? "", /\/src\/App\.tsx/);
  assert.match(stack ?? "", /\/assets\/index\.js/);
  assert.doesNotMatch(stack ?? "", /node_modules|token=|#secret|Users\/alice|supersecret/);
});

test("prepareClientDiagnostic emits only the versioned bounded public schema", () => {
  const detail: Record<string, unknown> = {
    name: "ProviderError",
    code: "HTTP_401",
    status: 401,
    route: RAW_URL,
    stack: `at App (https://rumi.test/src/App.tsx?token=${RAW_TOKEN}:1:2)`,
    response_body: `private prompt text ${RAW_TOKEN}`,
    headers: { authorization: `Bearer ${RAW_TOKEN}` },
    attachment: { name: "customer.pdf", email: RAW_EMAIL },
    huge: "x".repeat(100_000),
  };
  detail.circular = detail;

  const payload = prepareClientDiagnostic({
    source: "provider.error",
    category: "provider_failure",
    level: "error",
    message: `Provider failed at ${RAW_URL} for ${RAW_EMAIL} api_key=${RAW_TOKEN}`,
    conversationId: "conversation-private-1",
    detail,
  });

  assert.ok(payload);
  assert.equal(payload.schema_version, CLIENT_DIAGNOSTIC_SCHEMA_VERSION);
  assert.equal(payload.privacy_mode, "standard");
  assert.match(payload.event_id, /^event_/);
  assert.match(payload.session_id, /^session_/);
  assert.match(payload.fingerprint, /^diag_[a-f0-9]{8}$/);
  assert.match(payload.context_id ?? "", /^ctx_[a-f0-9]{8}$/);
  assert.deepEqual(Object.keys(payload).sort(), [
    "category",
    "context_id",
    "detail",
    "event_id",
    "fingerprint",
    "level",
    "message",
    "privacy_mode",
    "schema_version",
    "session_id",
    "source",
  ]);
  assert.ok(new TextEncoder().encode(serialized(payload)).byteLength <= CLIENT_DIAGNOSTIC_MAX_PAYLOAD_BYTES);
  assertSensitiveValuesAbsent(payload);
});

test("automatic crash categories never transmit raw exception messages", () => {
  const payload = prepareClientDiagnostic({
    source: "window.error",
    category: "window_error",
    message: `private prompt text ${RAW_TOKEN}`,
    detail: { name: "TypeError", stack: `at App (https://rumi.test/src/App.tsx:1:2)` },
  });

  assert.ok(payload);
  assert.equal(payload.message, "Unhandled window error");
  assertSensitiveValuesAbsent(payload);
});

test("private, local-only, disabled, and reporting-disabled diagnostics stay on device", () => {
  for (const privacyMode of ["private", "local_only", "disabled"] as const) {
    assert.equal(prepareClientDiagnostic({ message: "test", privacyMode }), null);
  }
  assert.equal(prepareClientDiagnostic({ message: "test", reportingEnabled: false }), null);
});

test("diagnostic reporting preference defaults local-only and requires explicit standard opt-in", () => {
  const storage = new PreferenceStorage();
  assert.equal(readClientDiagnosticPrivacyMode(storage), "local_only");

  assert.equal(writeClientDiagnosticPrivacyMode("standard", storage), "standard");
  assert.equal(storage.getItem(CLIENT_DIAGNOSTIC_PRIVACY_STORAGE_KEY), "standard");
  assert.equal(readClientDiagnosticPrivacyMode(storage), "standard");

  assert.equal(writeClientDiagnosticPrivacyMode("disabled", storage), "disabled");
  assert.equal(readClientDiagnosticPrivacyMode(storage), "disabled");
  assert.equal(writeClientDiagnosticPrivacyMode("private", storage), "local_only");
  assert.equal(readClientDiagnosticPrivacyMode(storage), "local_only");
});

test("diagnostic reporting preference fails local-only when storage is unavailable or corrupt", () => {
  assert.equal(readClientDiagnosticPrivacyMode(null), "local_only");
  assert.equal(readClientDiagnosticPrivacyMode({
    getItem: () => "unexpected",
    setItem: () => undefined,
  }), "local_only");
  assert.equal(readClientDiagnosticPrivacyMode({
    getItem: () => { throw new Error("storage locked"); },
    setItem: () => { throw new Error("storage locked"); },
  }), "local_only");
});

test("browser preference is authoritative for every diagnostic caller", () => {
  const storage = new PreferenceStorage();
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { localStorage: storage },
  });
  try {
    assert.equal(prepareClientDiagnostic({ message: "not opted in" }), null);
    writeClientDiagnosticPrivacyMode("standard", storage);
    assert.ok(prepareClientDiagnostic({ message: "opted in" }));
    writeClientDiagnosticPrivacyMode("disabled", storage);
    assert.equal(prepareClientDiagnostic({ message: "caller requested standard", privacyMode: "standard" }), null);
  } finally {
    delete (globalThis as { window?: unknown }).window;
  }
});

test("reportClientDiagnostic retries after failure and sends only the sanitized schema", async () => {
  const originalReportClientEvent = api.reportClientEvent;
  const sent: ClientDiagnosticPayloadV2[] = [];
  let calls = 0;
  api.reportClientEvent = async (payload) => {
    calls += 1;
    sent.push(payload as ClientDiagnosticPayloadV2);
    if (calls === 1) throw new Error("backend unavailable");
    return { recorded: true, diagnostic_id: "diag_1" };
  };

  try {
    const input = {
      source: "webapp",
      category: "conversation_integrity",
      message: `Collapsed duplicate messages without sending ${RAW_TOKEN}`,
      fingerprint: "retry-redacted-diagnostic",
      detail: { prompt: "private prompt text", stack: "at App (https://rumi.test/src/App.tsx:1:2)" },
    };
    assert.equal(await reportClientDiagnostic(input), false);
    assert.equal(await reportClientDiagnostic(input), true);
    assert.equal(calls, 2);
    assertSensitiveValuesAbsent(sent);
    assert.ok(sent.every((payload) => payload.schema_version === CLIENT_DIAGNOSTIC_SCHEMA_VERSION));
  } finally {
    api.reportClientEvent = originalReportClientEvent;
  }
});

test("reportClientDiagnostic does not claim success without server acknowledgement", async () => {
  const originalReportClientEvent = api.reportClientEvent;
  api.reportClientEvent = async () => ({ recorded: false, diagnostic_id: "" });
  try {
    assert.equal(await reportClientDiagnostic({
      message: "acknowledgement test",
      fingerprint: "unacknowledged-diagnostic",
    }), false);
  } finally {
    api.reportClientEvent = originalReportClientEvent;
  }
});

test("detailed diagnostic result exposes only sanitized acknowledgement ids", async () => {
  const originalReportClientEvent = api.reportClientEvent;
  api.reportClientEvent = async () => ({ recorded: true, diagnostic_id: "diag-safe/reference" });
  try {
    const result = await reportClientDiagnosticResult({ message: "safe", fingerprint: `result-${Date.now()}` });
    assert.deepEqual(result, { recorded: true, diagnosticId: "diag-safe_reference" });
  } finally { api.reportClientEvent = originalReportClientEvent; }
});
