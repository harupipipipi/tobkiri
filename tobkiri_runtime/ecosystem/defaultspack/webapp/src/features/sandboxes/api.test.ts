import test from "node:test";
import assert from "node:assert/strict";

import { sandboxesApi } from "./api";

function routeKey(path: string): string {
  return `/${path}`;
}

function requestTarget(input: RequestInfo | URL): string {
  const raw = String(input);
  const marker = "/api/contracts/defaultspack/";
  const markerIndex = raw.indexOf(marker);
  if (markerIndex < 0) return raw;
  const operation = decodeURIComponent(raw.slice(markerIndex + marker.length));
  const separator = operation.indexOf(" ");
  return separator < 0 ? operation : operation.slice(separator + 1);
}

function desktopResponse(status: "running" | "stopped") {
  return {
    seat_id: "seat-1",
    sandbox_id: "seat-1",
    name: "Ubuntu Desktop",
    status,
    provider_id: "fake-runtime",
    template_id: "desktop.ubuntu",
    resolution: { width: 800, height: 600 },
  };
}

test("ensureRuntime uses Defaultspack local auth and CSRF headers", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  const previousSessionStorage = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const values = new Map<string, string>([
    ["rumi-defaultspack-local-auth", "local-token-1"],
    ["rumi-panel-csrf", "panel-csrf-1"],
  ]);

  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        operation_id: "op-1",
        provider_id: "windows_wsl",
        status: "running",
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.ensureRuntime("windows_wsl");
    assert.equal(result.provider_id, "windows_wsl");
  } finally {
    globalThis.fetch = originalFetch;
    if (previousSessionStorage) Object.defineProperty(globalThis, "sessionStorage", previousSessionStorage);
    else Reflect.deleteProperty(globalThis, "sessionStorage");
  }

  const headers = new Headers(requestInit?.headers);
  const body = JSON.parse(String(requestInit?.body));
  assert.equal(requestUrl, routeKey("api/runtime/ensure"));
  assert.equal(requestInit?.method, "POST");
  assert.equal(headers.get("Authorization"), "Bearer local-token-1");
  assert.equal(headers.get("X-Rumi-CSRF"), "panel-csrf-1");
  assert.equal(body.provider_id, "windows_wsl");
  assert.match(body.request_id, /^ensure-/);
});

test("createDesktop does not accept client-supplied owner authority", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: desktopResponse("running"),
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.createDesktop({
      name: "Owner desktop",
      template_id: "desktop.ubuntu",
      resolution: { width: 1280, height: 800 },
      workspace_access: "none",
      access: { mode: "request_required" },
    });
    assert.equal(result.status, "running");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requestUrl, routeKey("api/desktops"));
  assert.equal(requestInit?.method, "POST");
  const body = JSON.parse(String(requestInit?.body));
  assert.equal(body.owner_id, undefined);
  assert.equal(body.access.mode, "request_required");
  assert.equal(body.access.owner_id, undefined);
  assert.equal(body.starter, undefined);
  assert.match(body.request_id, /^desktop-create-/);
});

test("createDesktop preserves generated shared-link access token from backend", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      ...desktopResponse("running"),
      access_key: "generated-link-token",
      access_key_hint: "ends:oken",
      access_policy: {
        mode: "shared_link",
        link_enabled: true,
        key_hint: "ends:oken",
      },
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await sandboxesApi.createDesktop({
      name: "Shared desktop",
      template_id: "desktop.ubuntu",
      resolution: { width: 1280, height: 800 },
      workspace_access: "none",
      access: { mode: "shared_link" },
    });

    assert.equal(result.access_key, "generated-link-token");
    assert.equal(result.access_policy?.mode, "shared_link");
    assert.equal(result.access_policy?.link_enabled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("requestDesktopAccess lets the backend derive requester identity", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        seat_id: "seat-1",
        request_id: "dreq-1",
        reason: "Need access",
        status: "pending",
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.requestDesktopAccess("seat-1", "Need access");
    assert.equal(result.status, "pending");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requestUrl, routeKey("api/desktops/seat-1/access-requests"));
  assert.equal(requestInit?.method, "POST");
  const body = JSON.parse(String(requestInit?.body));
  assert.equal(body.requester_id, undefined);
  assert.equal(body.owner_id, undefined);
  assert.equal(body.reason, "Need access");
  assert.match(body.request_id, /^desktop-access-/);
});

test("listDesktops unwraps standard desktop list envelopes", async () => {
  let requestUrl = "";
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    requestUrl = requestTarget(input);
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        desktops: [desktopResponse("running")],
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.listDesktops();
    assert.equal(requestUrl, routeKey("api/desktops"));
    assert.equal(result.desktops.length, 1);
    assert.equal(result.desktops[0].seat_id, "seat-1");
    assert.equal(result.desktops[0].status, "running");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops normalizes unknown provisioning status to the explicit fallback", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      desktops: [
        {
          ...desktopResponse("running"),
          provisioning: {
            apps: ["google-chrome-stable"],
            mcp_servers: ["playwright"],
            status: "provider-specific-status",
          },
        },
      ],
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await sandboxesApi.listDesktops();
    assert.equal(result.desktops[0].provisioning?.status, "unknown");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops accepts bare desktop list payloads and trims desktop status", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    desktops: [
      {
        ...desktopResponse("running"),
        status: " running ",
        startup: { starter: "browser_url", browser_url: "http://127.0.0.1:18766/chat" },
        desktop_spec: { enabled: true, width: 1440, height: 900, display_backend: "x11" },
      },
    ],
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await sandboxesApi.listDesktops();
    assert.equal(result.desktops[0].status, "running");
    assert.equal(result.desktops[0].startup?.browser_url, "http://127.0.0.1:18766/chat");
    assert.equal(result.desktops[0].desktop_spec?.enabled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops canonicalizes wrapped desktop records before rendering", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      desktops: [
        {
          id: "desktop-from-id",
          displayName: "Primary Desktop",
          running: true,
          provider_id: "fake-runtime",
          provisioning: {
            state: "installed",
          },
        },
        {
          seatId: "desktop-from-seat-id",
          sandboxId: "sandbox-alias",
          name: "Secondary Desktop",
          state: "ready",
          provider_id: "fake-runtime",
        },
      ],
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await sandboxesApi.listDesktops();
    assert.equal(result.desktops.length, 2);
    assert.equal(result.desktops[0].seat_id, "desktop-from-id");
    assert.equal(result.desktops[0].sandbox_id, "desktop-from-id");
    assert.equal(result.desktops[0].name, "Primary Desktop");
    assert.equal(result.desktops[0].status, "running");
    assert.equal(result.desktops[0].provisioning?.status, "installed");
    assert.equal(result.desktops[1].seat_id, "desktop-from-seat-id");
    assert.equal(result.desktops[1].sandbox_id, "sandbox-alias");
    assert.equal(result.desktops[1].status, "running");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops normalizes desktop state when status is missing", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      desktops: [
        {
          ...desktopResponse("stopped"),
          status: undefined,
          state: "ready",
        },
      ],
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await sandboxesApi.listDesktops();
    assert.equal(result.desktops[0].status, "running");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops reports desktop records with no usable id", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      desktops: [
        {
          name: "Running but unaddressable",
          status: "running",
        },
      ],
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    await assert.rejects(
      () => sandboxesApi.listDesktops(),
      /usable desktop id/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops reports malformed standard desktop list envelopes clearly", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: { desktops: "not-an-array" },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    await assert.rejects(
      () => sandboxesApi.listDesktops(),
      /desktops array/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("listDesktops reports missing desktop arrays clearly", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: { seats: [] },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    await assert.rejects(
      () => sandboxesApi.listDesktops(),
      /desktops array/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchDesktopFrame sends scoped credential without legacy authority headers", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(new Blob(["frame"], { type: "image/png" }), {
      status: 200,
      headers: {
        "Content-Type": "image/png",
        "X-Rumi-Frame-Seq": "7",
        "X-Rumi-Frame-Width": "800",
        "X-Rumi-Frame-Height": "600",
      },
    });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.fetchDesktopFrame("seat-1", { accessKey: "key-1" });
    assert.equal(result.status, "frame");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requestUrl, routeKey("api/desktops/seat-1/frame"));
  const headers = new Headers(requestInit?.headers);
  assert.equal(headers.get("X-Rumi-Desktop-Session-Credential"), "key-1");
  assert.equal(headers.get("X-Rumi-Desktop-Access-Key"), null);
  assert.equal(headers.get("X-Rumi-Desktop-Owner"), null);
});

test("stopDesktop confirms the destructive action after the UI confirmation flow", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: desktopResponse("stopped"),
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.stopDesktop("seat-1", "key-1");
    assert.equal(result.status, "stopped");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requestUrl, routeKey("api/desktops/seat-1/stop"));
  assert.equal(requestInit?.method, "POST");
  const body = JSON.parse(String(requestInit?.body));
  assert.equal(body.owner_id, undefined);
  assert.equal(body.desktop_session_credential, "key-1");
  assert.equal(body.access_key, undefined);
  assert.equal(body.confirm_destructive, true);
  assert.match(body.request_id, /^desktop-stop-/);
});

test("startDesktop and restartDesktop forward the scoped session credential", async () => {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: requestTarget(input), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({
      status: "ok",
      data: desktopResponse("running"),
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    await sandboxesApi.startDesktop("seat-1", "key-1");
    await sandboxesApi.restartDesktop("seat-1", "key-1");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls[0].url, routeKey("api/desktops/seat-1/start"));
  assert.equal(calls[0].body.desktop_session_credential, "key-1");
  assert.equal(calls[0].body.access_key, undefined);
  assert.equal(calls[1].url, routeKey("api/desktops/seat-1/restart"));
  assert.equal(calls[1].body.desktop_session_credential, "key-1");
});

test("deleteDesktop confirms the destructive action after the UI confirmation flow", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: { deleted: true, seat_id: "seat-1" },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.deleteDesktop("seat-1", "key-1");
    assert.deepEqual(result, { deleted: true, seat_id: "seat-1" });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.match(requestUrl, /^\/api\/desktops\/seat-1\?/);
  assert.equal(requestInit?.method, "DELETE");
  assert.equal(new Headers(requestInit?.headers).get("X-Rumi-Desktop-Session-Credential"), "key-1");
  assert.equal(new Headers(requestInit?.headers).get("X-Rumi-Desktop-Access-Key"), null);
  assert.equal(new Headers(requestInit?.headers).get("X-Rumi-Desktop-Owner"), null);
  assert.equal(requestInit?.body, undefined);
  const query = new URLSearchParams(requestUrl.split("?", 2)[1]);
  assert.equal(query.get("owner_id"), null);
  assert.equal(query.get("confirm_destructive"), "true");
  assert.match(query.get("request_id") || "", /^desktop-delete-/);
});

test("grantDesktopAccess sends owner approval to the request grant endpoint", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestUrl = requestTarget(input);
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        seat_id: "seat-1",
        request_id: "dreq-1",
        status: "approved",
        access_key: "secret-key",
        access_key_hint: "ends:-key",
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.grantDesktopAccess("seat-1", "dreq-1");
    assert.equal(result.access_key, "secret-key");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requestUrl, routeKey("api/desktops/seat-1/access-requests/dreq-1/grant"));
  assert.equal(requestInit?.method, "POST");
  const body = JSON.parse(String(requestInit?.body));
  assert.equal(body.owner_id, undefined);
  assert.equal(body.approved, true);
  assert.match(body.request_id, /^desktop-access-grant-/);
});

test("desktop control acquire normalizes epoch lease expiry to an ISO string", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "ok",
    data: {
      seat_id: "seat-1",
      lease_id: "lease-1",
      lease_token: "secret-token",
      expires_at: 1767225600,
    },
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;

  try {
    const result = await sandboxesApi.acquireDesktopControl("seat-1");
    assert.equal(result.expires_at, "2026-01-01T00:00:00.000Z");
    assert.equal(result.lease_token, "secret-token");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("desktop control renew normalizes expiry without requiring a lease token in the response", async () => {
  let requestInit: RequestInit | undefined;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    requestInit = init;
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        seat_id: "seat-1",
        lease_id: "lease-1",
        expires_at: 1767225610,
      },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;

  try {
    const result = await sandboxesApi.renewDesktopControl("seat-1", "secret-token");
    assert.equal(result.expires_at, "2026-01-01T00:00:10.000Z");
    assert.equal("lease_token" in result, false);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const body = JSON.parse(String(requestInit?.body));
  assert.equal(body.lease_token, "secret-token");
  assert.match(body.request_id, /^desktop-control-renew-/);
});
