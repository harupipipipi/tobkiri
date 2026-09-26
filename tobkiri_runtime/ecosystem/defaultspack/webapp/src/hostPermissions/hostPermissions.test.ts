import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { AuthorityRequest } from "../lib/api";
import type { DesktopSystemInfo } from "../lib/desktopSystemInfo";
import {
  buildHostPermissionRows,
  hostPermissionDefinitions,
  hostPermissionSummary,
  safeHostPermissionDiagnostic,
} from "./hostPermissions";
import { fetchHostPermissionsSnapshot } from "./hostPermissionsClient";
import {
  authorityUnavailableFeedback,
  HostPermissionsTable,
  StatusStrip,
} from "./HostPermissionsPage";

type CanonicalHostPermissionDefinition = {
  risk_level?: string;
  stream_allowed?: boolean;
};

const canonicalRegistryPath = fileURLToPath(new URL("../../../../../core_runtime/host_permissions/default_registry.json", import.meta.url));

function canonicalRegistry(): Record<string, CanonicalHostPermissionDefinition> {
  return JSON.parse(readFileSync(canonicalRegistryPath, "utf-8")) as Record<string, CanonicalHostPermissionDefinition>;
}

function desktopInfo(overrides: Partial<DesktopSystemInfo> = {}): DesktopSystemInfo {
  return {
    source: "viewer_tauri",
    reliable: true,
    app_name: "Tobkiri",
    display_version: "1.0.0",
    viewer_version: "1.0.0",
    build_channel: "dev",
    platform: "darwin",
    platform_release: "15.0",
    permissions: [],
    ...overrides,
  };
}

function authorityRequest(overrides: Partial<AuthorityRequest>): AuthorityRequest {
  return {
    request_id: "req-1",
    status: "pending",
    principal_id: "local:user",
    permission_id: "host.screen.capture",
    resource: {},
    reason: "",
    risk_level: "high",
    created_at: "2026-06-15T00:00:00.000Z",
    ...overrides,
  };
}

function setupWindowMock() {
  (globalThis as Record<string, unknown>).window = {};
}

function contractRequestTarget(input: RequestInfo | URL): string {
  const raw = String(input);
  const marker = "/api/contracts/defaultspack/";
  const markerIndex = raw.indexOf(marker);
  if (markerIndex < 0) return raw;
  const operation = decodeURIComponent(raw.slice(markerIndex + marker.length));
  const separator = operation.indexOf(" ");
  return separator < 0 ? operation : operation.slice(separator + 1);
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("host permissions", () => {
  it("derives frontend definitions from the canonical host permission registry", (context) => {
    if (!existsSync(canonicalRegistryPath)) {
      context.skip("Requires the sibling core_runtime canonical registry.");
      return;
    }
    const registry = canonicalRegistry();
    const definitions = hostPermissionDefinitions();

    assert.deepEqual(definitions.map((definition) => definition.id), Object.keys(registry));
    for (const definition of definitions) {
      const canonical = registry[definition.id];
      assert.equal(definition.riskLevel, canonical.risk_level ?? "medium", definition.id);
      assert.equal(definition.streamAllowed, Boolean(canonical.stream_allowed), definition.id);
    }
    assert.equal(definitions.some((definition) => definition.id === "host.clipboard.*"), false);
  });

  it("builds rows from desktop host permissions and OS permission aliases", () => {
    const rows = buildHostPermissionRows(desktopInfo({
      host_permissions: {
        "host.microphone.capture": {
          id: "host.microphone.capture",
          rumi_granted: true,
          os_status: "granted",
          stream_allowed: true,
          required_by_functions: ["ambient_monitor_start"],
        },
        "host.screen.capture": {
          id: "host.screen.capture",
          stream_allowed: true,
        },
      },
      permissions: [
        { id: "screen_recording", label: "Screen Recording", status: "missing", granted: false, detail: "", settings_hint: "" },
      ],
    }));

    const microphone = rows.find((row) => row.id === "host.microphone.capture");
    assert.equal(microphone?.rumiStatus, "approved");
    assert.equal(microphone?.osStatus, "approved");
    assert.deepEqual(microphone?.requiredByFunctions, ["ambient_monitor_start"]);

    const screen = rows.find((row) => row.id === "host.screen.capture");
    assert.equal(screen?.osStatus, "missing");
    assert.equal(screen?.streamAllowed, false);

    const audio = rows.find((row) => row.id === "host.audio.capture");
    assert.equal(audio?.streamAllowed, false);
  });

  it("uses latest matching authority request for Rumi approval status", () => {
    const rows = buildHostPermissionRows(
      desktopInfo(),
      [
        authorityRequest({ request_id: "old", status: "denied", created_at: "2026-06-14T00:00:00.000Z" }),
        authorityRequest({ request_id: "new", status: "approved", created_at: "2026-06-15T00:00:00.000Z" }),
      ],
    );

    const screen = rows.find((row) => row.id === "host.screen.capture");
    assert.equal(screen?.rumiStatus, "approved");
  });

  it("summarizes approvals and treats clipboard OS permission as not required", () => {
    const rows = buildHostPermissionRows(desktopInfo({
      host_permissions: [
        { id: "host.clipboard.*", rumi_status: "approved", stream_allowed: false },
      ],
    }));
    const clipboardRead = rows.find((row) => row.id === "host.clipboard.read");
    const clipboardWrite = rows.find((row) => row.id === "host.clipboard.write");
    assert.equal(clipboardRead?.rumiStatus, "approved");
    assert.equal(clipboardRead?.osStatus, "unsupported");
    assert.equal(clipboardWrite?.osStatus, "unsupported");

    const summary = hostPermissionSummary(rows);
    assert.equal(summary.total, hostPermissionDefinitions().length);
    assert.equal(summary.approved, 1);
    assert.equal(summary.osReady, 2);
  });

  it("renders permission relationships as a named table with explicit row actions", () => {
    const rows = buildHostPermissionRows(desktopInfo({
      host_permissions: [
        {
          id: "host.screen.capture",
          label: "Screen Capture",
          detail: "Read visible screen content without hiding a long description.",
          rumi_status: "approved",
          os_status: "missing",
          required_by_functions: ["computer_screenshot_with_a_long_function_identifier"],
        },
      ],
    }));
    const screen = rows.find((row) => row.id === "host.screen.capture");
    assert.ok(screen);

    const html = renderToStaticMarkup(createElement(HostPermissionsTable, {
      rows: [screen],
      loading: false,
      failed: false,
      tauriAvailable: true,
      openingPermissionId: null,
      settingsDestination: "macOS System Settings",
      onOpenSettings: () => undefined,
    }));

    assert.match(html, /<table/);
    assert.match(html, /<caption/);
    assert.match(html, /scope="col"/);
    assert.match(html, /scope="row"/);
    assert.match(html, /Overall status: Missing/);
    assert.match(html, /Tobkiri approval/);
    assert.match(html, /OS permission/);
    assert.match(html, /Source: Tobkiri Launcher/);
    assert.match(html, /aria-label="Open macOS System Settings for Screen Capture"/);
    assert.match(html, /min-h-11/);
    assert.doesNotMatch(html, /(?:line-clamp|\btruncate\b)/);

    const css = readFileSync(fileURLToPath(new URL("../index.css", import.meta.url)), "utf-8");
    assert.match(css, /forced-colors: active[\s\S]*host-permissions-page/);
    assert.match(css, /host-permissions-page[\s\S]*outline: 2px solid Highlight/);
  });

  it("marks loading summaries busy while retaining explicit text", () => {
    const html = renderToStaticMarkup(createElement(StatusStrip, {
      snapshot: null,
      loading: true,
    }));

    assert.match(html, /aria-busy="true"/);
    assert.match(html, /<dl/);
    assert.match(html, /Tobkiri approvals/);
    assert.match(html, />Loading</);
  });

  it("redacts credentials, URLs, and local paths from disclosed diagnostics", () => {
    const diagnostic = safeHostPermissionDiagnostic(new Error(
      "https://host.test/path?token=secret /Users/alice/private/file\nAuthorization: Bearer abcdefghijklmnop",
    ));

    assert.match(diagnostic, /\[auth-header\]/);
    assert.match(diagnostic, /\[url\]/);
    assert.match(diagnostic, /\[path\]/);
    assert.doesNotMatch(diagnostic, /abcdefghijklmnop|alice|host\.test/);
  });

  it("marks the authority lookup unavailable while preserving OS permission rows", async (context) => {
    setupWindowMock();
    const originalFetch = globalThis.fetch;
    context.after(() => {
      globalThis.fetch = originalFetch;
    });
    globalThis.fetch = async (input) => {
      const target = contractRequestTarget(input);
      if (target.startsWith("/api/desktop-system-info")) {
        return jsonResponse({
          status: "ok",
          data: desktopInfo({
            host_permissions: {
              "host.screen.capture": {
                id: "host.screen.capture",
                os_status: "granted",
                stream_allowed: true,
              },
            },
          }),
        });
      }
      if (target.startsWith("/api/authority/requests")) {
        throw new TypeError(
          "fetch failed https://authority.invalid/lookup?access_token=deadbeefsecret",
        );
      }
      return jsonResponse({}, 404);
    };

    const snapshot = await fetchHostPermissionsSnapshot();

    // The renamed contract replaces the base `authorityError` string with an
    // explicit unavailable flag plus a sanitized diagnostic.
    assert.equal(snapshot.authorityUnavailable, true);
    assert.match(snapshot.authorityDiagnostic ?? "", /fetch failed/);
    assert.doesNotMatch(snapshot.authorityDiagnostic ?? "", /deadbeefsecret|authority\.invalid/);
    assert.deepEqual(snapshot.authorityRequests, []);

    // OS permission rows remain visible even when the approval history is not.
    const screen = snapshot.rows.find((row) => row.id === "host.screen.capture");
    assert.equal(screen?.osStatus, "approved");
    assert.equal(snapshot.summary.total, hostPermissionDefinitions().length);
  });

  it("omits the authority divergence fields when the authority lookup succeeds", async (context) => {
    setupWindowMock();
    const originalFetch = globalThis.fetch;
    context.after(() => {
      globalThis.fetch = originalFetch;
    });
    globalThis.fetch = async (input) => {
      const target = contractRequestTarget(input);
      if (target.startsWith("/api/desktop-system-info")) {
        return jsonResponse({ status: "ok", data: desktopInfo() });
      }
      if (target.startsWith("/api/authority/requests")) {
        return jsonResponse({
          success: true,
          data: {
            requests: [authorityRequest({ status: "approved" })],
            pending: [],
            count: 1,
          },
        });
      }
      return jsonResponse({}, 404);
    };

    const snapshot = await fetchHostPermissionsSnapshot();

    assert.equal(snapshot.authorityUnavailable, undefined);
    assert.equal(snapshot.authorityDiagnostic, undefined);
    assert.equal(snapshot.authorityRequests.length, 1);
    const screen = snapshot.rows.find((row) => row.id === "host.screen.capture");
    assert.equal(screen?.rumiStatus, "approved");
  });

  it("locks the renamed authority-divergence contract as an error notice with a disclosed diagnostic", () => {
    const feedback = authorityUnavailableFeedback("TypeError: lookup failed");

    // The base `authorityError` path rendered a warning; the redesign uses an
    // error alert plus a separately disclosed diagnostic instead.
    assert.equal(feedback.notice.tone, "error");
    assert.match(feedback.notice.text, /Tobkiri approval history is temporarily unavailable/);
    assert.match(feedback.notice.text, /OS permission values remain visible/);
    assert.equal(feedback.diagnostic, "TypeError: lookup failed");

    // Missing diagnostics fall back to a generic label rather than leaking
    // raw internals into the disclosure block.
    assert.equal(authorityUnavailableFeedback(undefined).diagnostic, "Authority request lookup failed.");
    assert.equal(authorityUnavailableFeedback("").diagnostic, "Authority request lookup failed.");
  });
});
