import test from "node:test";
import assert from "node:assert/strict";

import {
  PLACEMENT_HTML_MAX_SOURCE_BYTES,
  buildBuiltinPlacementManifests,
  buildToggleablePlacementCandidates,
  filterPlacementCandidates,
  readPinnedPlacements,
  resolvePlacementHtmlRendering,
  togglePinnedPlacement,
  writePinnedPlacements,
  type PlacementManifest,
} from "./placement";

test("placement filtering respects surface orientation and settings_only", () => {
  const manifests: PlacementManifest[] = [
    {
      id: "horizontal",
      label: "Horizontal",
      source: { type: "custom" },
      renderer: { kind: "component" },
      placements: [{ surface: "top_bar", orientation: "horizontal" }],
    },
    {
      id: "vertical",
      label: "Vertical",
      source: { type: "custom" },
      renderer: { kind: "component" },
      placements: [{ surface: "right_sidebar", orientation: "vertical" }],
      constraints: { settings_only: true },
    },
    {
      id: "both",
      label: "Both",
      source: { type: "custom" },
      renderer: { kind: "component" },
      placements: [{ surface: "right_sidebar", orientation: "both" }],
    },
  ];

  assert.deepEqual(
    filterPlacementCandidates(manifests, { surface: "top_bar", orientation: "horizontal" }).map((item) => item.id),
    ["horizontal"],
  );
  assert.deepEqual(
    filterPlacementCandidates(manifests, { surface: "right_sidebar", orientation: "vertical" }).map((item) => item.id),
    ["both"],
  );
  assert.deepEqual(
    filterPlacementCandidates(manifests, { surface: "settings", orientation: "vertical" }).map((item) => item.id),
    [],
  );
});

test("pinned placements persist through storage helpers", () => {
  const store = new Map<string, string>();
  const storage = {
    getItem(key: string) {
      return store.get(key) ?? null;
    },
    setItem(key: string, value: string) {
      store.set(key, value);
    },
  };
  const next = togglePinnedPlacement([], { id: "yolo-switch", surface: "right_sidebar" });
  writePinnedPlacements(storage, next);

  assert.deepEqual(readPinnedPlacements(storage), [{ id: "yolo-switch", surface: "right_sidebar" }]);
});

test("placement menus retain pinned candidates so selecting again can unpin", () => {
  const manifests = buildBuiltinPlacementManifests([]);
  const candidates = buildToggleablePlacementCandidates(
    manifests,
    [{ id: "tool-filter-log", surface: "right_sidebar" }],
    {
      surface: "right_sidebar",
      orientation: "vertical",
      configurableOnly: true,
    },
  );

  assert.equal(
    candidates.find(({ manifest }) => manifest.id === "tool-filter-log")?.pinned,
    true,
  );
  assert.equal(
    candidates.find(({ manifest }) => manifest.id === "runtime-status")?.pinned,
    false,
  );
});

test("builtin placements include yolo switch and model manager", () => {
  const ids = buildBuiltinPlacementManifests([{ id: "tools", label: "Tools", fields: [] }]).map((item) => item.id);
  assert.ok(ids.includes("yolo-switch"));
  assert.ok(ids.includes("model-manager"));
});

test("html placements fail closed instead of creating an origin-bearing iframe", () => {
  const rendering = resolvePlacementHtmlRendering({
    id: "html-test",
    label: "HTML",
    source: { type: "custom", sourceId: "example-extension" },
    renderer: { kind: "html", trusted: true, html: "<script>alert(1)</script>" },
    placements: [{ surface: "right_sidebar", orientation: "vertical" }],
  });

  assert.equal(rendering.kind, "blocked_html");
  if (rendering.kind !== "blocked_html") return;
  assert.equal(rendering.reason, "unverified_active_content");
  assert.equal(rendering.sourceLabel, "custom:example-extension");
  assert.match(rendering.message, /Arbitrary HTML placements are disabled/);
  assert.equal("sandbox" in rendering, false);
  assert.equal("html" in rendering, false);
});

test("active and network-capable HTML payloads are all blocked", () => {
  const payloads = [
    '<img src="https://tracker.example/pixel">',
    '<link rel="stylesheet" href="http://127.0.0.1/private.css">',
    '<meta http-equiv="refresh" content="0;url=https://example.test">',
    '<form><button>Approve access</button></form>',
    '<svg><image href="http://192.168.1.1/secret"></image></svg>',
    '<style>@import url("https://fonts.example/font.css")</style>',
  ];

  for (const html of payloads) {
    const rendering = resolvePlacementHtmlRendering({
      id: "adversarial-html",
      label: "Adversarial HTML",
      source: { type: "custom" },
      renderer: { kind: "html", html },
      placements: [{ surface: "settings", orientation: "vertical" }],
    });
    assert.equal(rendering.kind, "blocked_html");
    if (rendering.kind === "blocked_html") {
      assert.equal(rendering.reason, "unverified_active_content");
    }
  }
});

test("oversized and empty HTML placements expose bounded blocked states", () => {
  const oversized = resolvePlacementHtmlRendering({
    id: "oversized-html",
    label: "Oversized HTML",
    source: { type: "custom" },
    renderer: { kind: "html", html: "x".repeat(PLACEMENT_HTML_MAX_SOURCE_BYTES + 1) },
    placements: [{ surface: "settings", orientation: "vertical" }],
  });
  assert.equal(oversized.kind, "blocked_html");
  if (oversized.kind === "blocked_html") {
    assert.equal(oversized.reason, "oversized_html");
    assert.equal(oversized.byteLength, PLACEMENT_HTML_MAX_SOURCE_BYTES + 1);
  }

  const empty = resolvePlacementHtmlRendering({
    id: "empty-html",
    label: "Empty HTML",
    source: { type: "custom" },
    renderer: { kind: "html", html: "  " },
    placements: [{ surface: "settings", orientation: "vertical" }],
  });
  assert.equal(empty.kind, "blocked_html");
  if (empty.kind === "blocked_html") {
    assert.equal(empty.reason, "empty_html");
  }
});
