import test from "node:test";
import assert from "node:assert/strict";

import { diagnosticsText, runtimeAvailability } from "./runtimeStatus";
import { normalizeDesktopStatus } from "./types";

test("runtimeAvailability does not treat available provider as ready without ready flag", () => {
  const availability = runtimeAvailability(
    {
      providers: [
        {
          provider_id: "linux_native",
          status: "available",
          available: true,
          installed: true,
          ready: false,
        },
      ],
      selected_provider_id: "linux_native",
    },
    null,
    null,
  );

  assert.equal(availability.status, "unavailable");
});

test("runtimeAvailability selects a ready provider when the preferred provider still needs setup", () => {
  const availability = runtimeAvailability(
    {
      providers: [
        {
          provider_id: "linux_native",
          status: "needs_setup",
          available: true,
          installed: false,
          ready: false,
        },
        {
          provider_id: "mac_lima",
          label: "Mac Lima",
          status: "ready",
          available: true,
          installed: true,
          ready: true,
          capabilities: [
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
          ],
        },
      ],
      selected_provider_id: "linux_native",
    },
    null,
    null,
  );

  assert.equal(availability.status, "ready");
  assert.equal(availability.selectedProvider?.provider_id, "mac_lima");
  assert.equal(availability.message, "Mac Lima is ready.");
});

test("runtimeAvailability treats whitespace capability strings as desktop capable", () => {
  const availability = runtimeAvailability(
    {
      providers: [
        {
          provider_id: "windows_wsl",
          label: "RumiUbuntu WSL2",
          status: "ready",
          available: true,
          installed: true,
          ready: true,
          selected: true,
          capabilities: "sandbox.desktop sandbox.desktop_input sandbox.exec sandbox.snapshot",
        },
      ],
      selected_provider_id: "windows_wsl",
    },
    null,
    null,
  );

  assert.equal(availability.status, "ready");
  assert.equal(availability.selectedProvider?.provider_id, "windows_wsl");
  assert.equal(availability.message, "RumiUbuntu WSL2 is ready.");
});

test("runtimeAvailability does not treat exec-only ready provider as desktop ready", () => {
  const availability = runtimeAvailability(
    {
      providers: [
        {
          provider_id: "linux_native",
          status: "needs_setup",
          available: true,
          installed: false,
          ready: false,
          capabilities: [
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
          ],
        },
        {
          provider_id: "docker",
          label: "Docker",
          status: "ready",
          available: true,
          installed: true,
          ready: true,
          capabilities: ["sandbox.exec", "sandbox.files"],
        },
      ],
      selected_provider_id: "linux_native",
    },
    null,
    null,
  );

  assert.equal(availability.status, "needs_setup");
  assert.equal(availability.selectedProvider?.provider_id, "linux_native");
});

test("normalizeDesktopStatus maps sandbox ready and busy states to running", () => {
  assert.equal(normalizeDesktopStatus("ready"), "running");
  assert.equal(normalizeDesktopStatus("busy"), "running");
  assert.equal(normalizeDesktopStatus("not-a-status"), "unknown");
});

test("diagnosticsText redacts secret-like diagnostic values", () => {
  const text = diagnosticsText({
    providersResponse: {
      providers: [
        {
          provider_id: "linux_native",
          status: "error",
          diagnostics: {
            api_key: "sk-test-secret",
            nested: { accessToken: "runtime-token" },
            safe: "visible",
          },
        },
      ],
    },
    doctor: {
      status: "error",
      diagnostics: {
        credential_ref: "credential-secret",
        command: "xvfb",
      },
    },
    error: "plain error",
  });

  assert.match(text, /"safe": "visible"/);
  assert.match(text, /"command": "xvfb"/);
  assert.match(text, /\[redacted\]/);
  assert.doesNotMatch(text, /sk-test-secret/);
  assert.doesNotMatch(text, /runtime-token/);
  assert.doesNotMatch(text, /credential-secret/);
});

test("explicit unprobed registration overrides forged ready and remains visible", () => {
  const provider = { provider_id: "mac_lima", status: "available", ready: true,
    host_platform_supported: true, diagnostics: { probe_status: "not_run" },
    capabilities: ["sandbox.desktop", "sandbox.desktop_input", "sandbox.snapshot"] } as const;
  const result = runtimeAvailability({ providers: [{ ...provider, capabilities: [...provider.capabilities] }],
    diagnostics: { probe_status: "not_run" } }, { status: "ready", diagnostics: { probe_status: "not_run" } }, null);
  assert.equal(result.status, "registered");
  assert.equal(result.providers.length, 1);
  assert.match(result.message, /未診断/);
});

test("unsupported operations reject before executing any request; legacy supported calls work", async () => {
  const { runSupportedRuntimeOperation, runtimeOperationAllowed } = await import("./runtimeStatus");
  const operations = ["create", "setup", "lifecycle", "delete", "access", "control", "frame", "doctor"] as const;
  let requests = 0;
  for (const operation of operations) {
    await assert.rejects(runSupportedRuntimeOperation({ [operation]: false }, operation, async () => {
      requests += 1;
    }), /まだ接続されていません/);
  }
  assert.equal(requests, 0);
  assert.equal(runtimeOperationAllowed(undefined, "create"), true);
  assert.equal(await runSupportedRuntimeOperation(undefined, "create", async () => { requests += 1; return "real-result"; }), "real-result");
  assert.equal(requests, 1);
});

test("unprobed metadata never masks a failed diagnostic request", () => {
  const result = runtimeAvailability({ providers: [{ provider_id: "mac_lima", status: "available", diagnostics: { probe_status: "not_run" } }], diagnostics: { probe_status: "not_run" } }, null, "HTTP 401");
  assert.equal(result.status, "error");
  assert.match(result.message, /HTTP 401/);
  assert.equal(result.providers.length, 1);
});

test("saved seats cannot trigger operations after metadata lookup fails, while known legacy metadata remains supported", async () => {
  const { runtimeOperationSupportForMetadata, runSupportedRuntimeOperation } = await import("./runtimeStatus");
  const savedSeat = { seat_id: "persisted-running-seat", status: "running" };
  let requests = 0;
  const unavailable = runtimeOperationSupportForMetadata(null, null);
  for (const operation of ["access", "frame", "control", "lifecycle", "delete"] as const) {
    await assert.rejects(runSupportedRuntimeOperation(unavailable, operation, async () => {
      requests += 1;
      return savedSeat;
    }));
  }
  assert.equal(requests, 0);
  const legacy = runtimeOperationSupportForMetadata({ providers: [] }, null);
  assert.deepEqual(await runSupportedRuntimeOperation(legacy, "frame", async () => savedSeat), savedSeat);
});
