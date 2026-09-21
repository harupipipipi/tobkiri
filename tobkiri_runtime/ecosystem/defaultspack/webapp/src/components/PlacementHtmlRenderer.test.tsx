import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { PlacementHtmlRenderer } from "./PlacementHtmlRenderer";
import type { PlacementManifest } from "../lib/placement";

test("untrusted HTML renders an explicit extension boundary without an iframe", () => {
  const html = renderToStaticMarkup(
    createElement(PlacementHtmlRenderer, {
      manifest: {
        id: "html-test",
        label: "HTML test",
        source: { type: "custom", sourceId: "unsafe-extension" },
        renderer: { kind: "html", html: "<div>unsafe</div><script>alert(1)</script>" },
        placements: [{ surface: "right_sidebar", orientation: "vertical" }],
      },
    }),
  );

  assert.match(html, /role="status"/);
  assert.match(html, /Untrusted HTML blocked/);
  assert.match(html, /Source: custom:unsafe-extension/);
  assert.match(html, /verified component or declarative template/);
  assert.doesNotMatch(html, /<iframe/);
  assert.doesNotMatch(html, /srcdoc=/i);
  assert.doesNotMatch(html, /sandbox=/i);
  assert.doesNotMatch(html, /<script>alert\(1\)<\/script>/);
  assert.doesNotMatch(html, /<div>unsafe<\/div>/);
});

test("blocked boundary exposes manifest provenance for report and recovery", () => {
  const html = renderToStaticMarkup(
    createElement(PlacementHtmlRenderer, {
      manifest: {
        id: "pack-ext:evil-widget",
        label: "Evil widget",
        source: { type: "integration", sourceId: "evil-pack" },
        renderer: { kind: "html", trusted: true, html: "<p>trusted looking</p>" },
        placements: [{ surface: "settings", orientation: "vertical" }],
      },
    }),
  );

  assert.match(html, /data-placement-id="pack-ext:evil-widget"/);
  assert.match(html, /Placement: pack-ext:evil-widget/);
  assert.match(html, /bytes requested/);
  assert.match(html, /Source: integration:evil-pack/);
});

test("blocked boundary offers a Disable recovery action only when provided", () => {
  const manifest: PlacementManifest = {
    id: "html-disable",
    label: "Disable me",
    source: { type: "custom" },
    renderer: { kind: "html", html: "<p>x</p>" },
    placements: [{ surface: "settings", orientation: "vertical" }],
  };

  const withDisable = renderToStaticMarkup(
    createElement(PlacementHtmlRenderer, { manifest, onDisable: () => {} }),
  );
  assert.match(withDisable, /Disable placement/);
  assert.match(withDisable, /aria-label="Disable Disable me placement"/);

  const withoutDisable = renderToStaticMarkup(
    createElement(PlacementHtmlRenderer, { manifest }),
  );
  assert.doesNotMatch(withoutDisable, /Disable placement/);
});

test("component and template manifests render nothing in the HTML boundary", () => {
  for (const kind of ["component", "template"] as const) {
    const html = renderToStaticMarkup(
      createElement(PlacementHtmlRenderer, {
        manifest: {
          id: `non-html-${kind}`,
          label: kind,
          source: { type: "widget" },
          renderer: { kind },
          placements: [{ surface: "settings", orientation: "vertical" }],
        },
      }),
    );
    assert.equal(html, "");
  }
});
