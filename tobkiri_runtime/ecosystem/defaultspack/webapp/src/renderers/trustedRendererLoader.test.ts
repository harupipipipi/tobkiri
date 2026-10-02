function routeKey(path: string): string {
  return `/${path}`;
}

import test from "node:test";
import assert from "node:assert/strict";

import type { ShellRenderer } from "../lib/api";
import {
  hasVerifiedBuiltinRendererProvenance,
  isRendererModuleQuarantined,
  isTrustedLocalRendererModule,
  loadTrustedRenderer,
  resetRendererQuarantineForTests,
} from "./trustedRendererLoader";

const originalWindow = globalThis.window;

function renderer(overrides: Record<string, unknown> = {}): ShellRenderer {
  return {
    id: "x",
    component: "X",
    module: "/static/renderers/custom.js",
    trust: "local",
    verified: true,
    provenance: {
      source: "builtin",
      content_hash: "a".repeat(64),
      build_id: "build-1",
    },
    ...overrides,
  } as unknown as ShellRenderer;
}

test("executable renderer modules are restricted to build-owned static paths", () => {
  Object.defineProperty(globalThis, "window", {
    value: { location: { origin: "http://127.0.0.1:8766" } },
    configurable: true,
  });

  assert.equal(isTrustedLocalRendererModule("/static/renderers/custom.js"), true);
  assert.equal(isTrustedLocalRendererModule("/static/assets/renderers/custom.js"), true);
  assert.equal(isTrustedLocalRendererModule("/static/user_renderers/custom.js"), false);
  assert.equal(isTrustedLocalRendererModule("/static/renderers/custom.js?swap=1"), false);
  assert.equal(isTrustedLocalRendererModule("/static/renderers/custom.js#other"), false);
  assert.equal(isTrustedLocalRendererModule("/static/renderers/%2e%2e/custom.js"), false);
  assert.equal(isTrustedLocalRendererModule(routeKey("api/ui/catalog")), false);
  assert.equal(isTrustedLocalRendererModule("https://example.com/custom.js"), false);

  Object.defineProperty(globalThis, "window", {
    value: originalWindow,
    configurable: true,
  });
});

test("catalog trust strings do not establish executable renderer provenance", () => {
  assert.equal(hasVerifiedBuiltinRendererProvenance(renderer({ verified: false })), false);
  assert.equal(hasVerifiedBuiltinRendererProvenance(renderer({ provenance: undefined })), false);
  assert.equal(hasVerifiedBuiltinRendererProvenance(renderer({
    provenance: { source: "user", content_hash: "a".repeat(64), build_id: "build-1" },
  })), false);
  assert.equal(hasVerifiedBuiltinRendererProvenance(renderer({
    provenance: { source: "builtin", content_hash: "short", build_id: "build-1" },
  })), false);
  assert.equal(hasVerifiedBuiltinRendererProvenance(renderer()), true);
});

test("loader falls back for self-declared or writable renderer sources", () => {
  Object.defineProperty(globalThis, "window", {
    value: { location: { origin: "http://127.0.0.1:8766" } },
    configurable: true,
  });
  function Fallback() {
    return null;
  }

  assert.equal(loadTrustedRenderer(renderer({ verified: false }), Fallback), Fallback);
  assert.equal(loadTrustedRenderer(renderer({ module: "/static/user_renderers/custom.js" }), Fallback), Fallback);
  assert.equal(loadTrustedRenderer(renderer({ module: routeKey("api/x") }), Fallback), Fallback);
  assert.notEqual(loadTrustedRenderer(renderer(), Fallback), Fallback);

  Object.defineProperty(globalThis, "window", {
    value: originalWindow,
    configurable: true,
  });
});

test("renderer quarantine is session-scoped and resettable for tests", () => {
  resetRendererQuarantineForTests();
  assert.equal(isRendererModuleQuarantined("/static/renderers/custom.js"), false);
});
