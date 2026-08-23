import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { ResolvedStatusSurface } from "../../lib/statusSurfaces";
import { STATUS_SURFACE_API_VERSION } from "../../lib/statusSurfaces";
import { StatusSurfaceHost } from "./StatusSurfaceHost";
import { ComposerRenderer } from "../ComposerRenderer";

function surface(id: string, overrides: Partial<ResolvedStatusSurface> = {}): ResolvedStatusSurface {
  return {
    id,
    apiVersion: STATUS_SURFACE_API_VERSION,
    slot: "above_composer",
    priority: 0,
    order: 0,
    title: id,
    severity: "neutral",
    details: [],
    controls: [],
    diagnostics: [],
    unsupported: false,
    ...overrides,
  };
}

test("renders accessible status, timer, progress, details control, and registered actions", () => {
  const html = renderToStaticMarkup(createElement(StatusSurfaceHost, {
    slot: "above_composer",
    surfaces: [surface("review", {
      title: "Security review",
      summary: "Reviewing authentication & authorization",
      status: "running",
      startedAt: "2026-07-16T00:00:00Z",
      progress: { current: 2, total: 5, label: "Review iterations" },
      details: [{ label: "Gate", value: "No critical findings" }],
      controls: [
        { id: "model", type: "model_select", label: "Model", actionId: "review.model", value: "model-a", disabled: false, options: [] },
        { id: "provider", type: "provider_select", label: "Provider", actionId: "review.provider", value: "provider-a", disabled: false, options: [] },
        { id: "thinking", type: "thinking_select", label: "Thinking", actionId: "review.thinking", value: "high", disabled: false, options: [] },
        { id: "details", type: "expand", label: "Details", disabled: false, options: [] },
      ],
    })],
    modelOptions: [{ value: "model-a", label: "Model A" }],
    providerOptions: [{ value: "provider-a", label: "Provider A" }],
    thinkingOptions: [{ value: "high", label: "High" }],
    onAction: () => undefined,
  }));
  assert.match(html, /aria-label="Active status surfaces: above composer"/);
  assert.match(html, /Security review/);
  assert.match(html, /Review iterations/);
  assert.match(html, /role="progressbar"/);
  assert.match(html, /aria-valuenow="2"/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /Model A/);
  assert.match(html, /Provider A/);
  assert.match(html, />High</);
  assert.match(html, /authentication &amp; authorization/);
});

test("bounds overflow and exposes a keyboard button to reveal remaining surfaces", () => {
  const html = renderToStaticMarkup(createElement(StatusSurfaceHost, {
    slot: "above_composer",
    maxVisible: 2,
    surfaces: [surface("one"), surface("two"), surface("three"), surface("four")],
  }));
  assert.match(html, /data-status-surface-id="four"/);
  assert.match(html, /data-status-surface-id="one"/);
  assert.doesNotMatch(html, /data-status-surface-id="two"/);
  assert.doesNotMatch(html, /data-status-surface-id="three"/);
  assert.match(html, /Show 2 more status surfaces/);
  assert.match(html, /aria-expanded="false"/);
});

test("renders invalid declarations as visible diagnostics without active controls", () => {
  const html = renderToStaticMarkup(createElement(StatusSurfaceHost, {
    slot: "above_composer",
    surfaces: [surface("unsafe", {
      title: "Unsupported status surface",
      summary: "unsupported control: javascript",
      severity: "error",
      unsupported: true,
      templateId: "fixture.unsafe",
      trustLevel: "untrusted",
      diagnostics: [{
        code: "status_surface.unknown_control",
        message: "unsupported control: javascript",
        surfaceId: "unsafe",
      }],
    })],
  }));
  assert.match(html, /data-status-surface-unsupported="true"/);
  assert.match(html, /status_surface\.unknown_control/);
  assert.match(html, /fixture\.unsafe/);
  assert.match(html, /untrusted/);
  assert.doesNotMatch(html, /javascript=/i);
});

test("does not render surfaces assigned to another slot", () => {
  const html = renderToStaticMarkup(createElement(StatusSurfaceHost, {
    slot: "sidebar",
    surfaces: [surface("composer-only")],
  }));
  assert.equal(html, "");
});

test("renders every approved shell slot through the generic host", () => {
  for (const slot of [
    "above_composer",
    "below_composer",
    "chat_header",
    "sidebar",
    "workspace_panel",
  ] as const) {
    const html = renderToStaticMarkup(createElement(StatusSurfaceHost, {
      slot,
      surfaces: [surface(slot, { slot })],
    }));
    assert.match(html, new RegExp(`data-status-surface-host="${slot}"`));
    assert.match(html, new RegExp(`data-status-surface-id="${slot}"`));
  }
});

test("the real Composer host renders a declarative above-composer surface", () => {
  const html = renderToStaticMarkup(createElement(ComposerRenderer, {
    input: "",
    placeholder: "Message",
    isGenerating: false,
    selectedProfile: {
      profile_id: "fixture/model",
      display_name: "Fixture Model",
      provider_id: "fixture",
      model_id: "model",
    },
    favoriteProfiles: [],
    modelProfiles: [],
    inlineExtensions: [],
    belowExtensions: [],
    thinkingLevel: null,
    contextUsage: { ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" },
    statusSurfaces: [surface("pack-build", {
      title: "Pack build",
      summary: "Compiled 8 of 10 modules",
      progress: { current: 8, total: 10 },
    })],
    onInputChange: () => undefined,
    onSubmit: () => undefined,
    onModelProfileSelect: () => undefined,
    onThinkingLevelChange: () => undefined,
  }));
  assert.match(html, /data-status-surface-host="above_composer"/);
  assert.match(html, /data-status-surface-id="pack-build"/);
  assert.match(html, /Compiled 8 of 10 modules/);
});
