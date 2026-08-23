import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  isHostResourceHandle,
  parseSurfaceSelector,
  projectSurfaceIntent,
  SurfaceTemplateRenderer,
  type SurfaceTemplate,
} from "./SurfaceTemplateRenderer";

const template: SurfaceTemplate = {
  surface_api_version: "io.tobkiri.surface-template.v1",
  template_id: "example.image.inspect.default",
  version: "1.0.0",
  input: {
    pattern: "resource_input",
    bind_to: "$.resource",
    resource_kind: "image",
    effect: "inspect",
  },
  outcomes: {
    success: { pattern: "content", data: "$.result" },
    error: { pattern: "problem", code: "$.error.code", message: "$.error.message" },
    progress: { pattern: "progress", current: "$.progress.current", total: "$.progress.total" },
  },
};

test("SurfaceTemplateRenderer projects the same semantic content shape as RecordingSurface", () => {
  const intent = projectSurfaceIntent(template, { kind: "success", result: { ok: true } });
  assert.deepEqual(intent, {
    template_id: "example.image.inspect.default",
    pattern: "content",
    payload: { data: { ok: true } },
  });
});

test("Surface selectors stay bounded and non-executable", () => {
  assert.deepEqual(parseSurfaceSelector("$.result.items[0]"), ["result", "items", 0]);
  assert.throws(() => parseSurfaceSelector("$.result[\"items\"]"));
  assert.throws(() => parseSurfaceSelector("$.constructor"));
});

test("React adapter accepts only Host resource handles", () => {
  assert.equal(isHostResourceHandle("handle:image/fixture-1"), true);
  assert.equal(isHostResourceHandle("C:\\tmp\\image.png"), false);
  assert.equal(isHostResourceHandle("file:///tmp/image.png"), false);
});

test("React adapter renders long untrusted content safely at narrow widths", () => {
  const html = renderToStaticMarkup(createElement(SurfaceTemplateRenderer, {
    template,
    event: { kind: "success", result: { text: `<img src=x onerror=alert(1)>${"x".repeat(4000)}` } },
  }));

  assert.match(html, /data-surface-pattern="content"/);
  assert.match(html, /min-w-0 max-w-full/);
  assert.match(html, /whitespace-pre-wrap break-words/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("React adapter blocks untrusted confirmation surfaces and sensitive controls", () => {
  const confirmation: SurfaceTemplate = {
    ...template,
    input: { pattern: "confirmation", label: "Approve" },
    outcomes: { success: { pattern: "confirmation", message: "$.result.message" } },
    actions: [
      {
        contract_id: "example.image.inspect.v1",
        operation_id: "inspect",
        payload_binding: { resource: "$.input.resource" },
        label: "Approve",
        sensitive: true,
      },
    ],
  };
  const event = { kind: "success", result: { message: "Approve elevated access" } };
  const untrusted = renderToStaticMarkup(createElement(SurfaceTemplateRenderer, {
    template: confirmation,
    event,
  }));
  const trusted = renderToStaticMarkup(createElement(SurfaceTemplateRenderer, {
    template: confirmation,
    event,
    trustedRenderer: true,
  }));

  assert.match(untrusted, /trusted decision surface is unavailable/);
  assert.doesNotMatch(untrusted, />Approve</);
  assert.match(trusted, /role="group"/);
  assert.match(trusted, /min-h-11/);
  assert.match(trusted, /focus-visible:outline/);
});
