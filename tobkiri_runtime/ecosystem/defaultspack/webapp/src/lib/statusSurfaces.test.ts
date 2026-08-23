import assert from "node:assert/strict";
import test from "node:test";

import type { UICatalog } from "./api";
import {
  STATUS_SURFACE_API_VERSION,
  readStatusSurfacePath,
  resolveStatusSurfaces,
  statusSurfacesForSlot,
} from "./statusSurfaces";

function catalogFixture(): UICatalog {
  return {
    sidebar: { filters: [], items: [] },
    settings: { sections: [], values: {} },
    chat_rendering: { renderers: [] },
    extension_points: [],
    commands: [
      {
        id: "review.pause",
        name: "review.pause",
        label: "Pause review",
        category: "coding",
        visibility: "default",
        risk: "low",
        execution: { type: "pack_block", qualified_name: "fixture.review:pause" },
      },
      {
        id: "review.cancel",
        name: "review.cancel",
        label: "Cancel review",
        category: "coding",
        visibility: "default",
        risk: "low",
        execution: { type: "pack_block", qualified_name: "fixture.review:cancel" },
      },
      {
        id: "build.stop",
        name: "build.stop",
        label: "Stop build",
        category: "coding",
        visibility: "default",
        risk: "low",
        execution: { type: "pack_block", qualified_name: "fixture.build:stop" },
      },
    ],
    data_sources: [
      {
        id: "review.active",
        data_source: "review.active",
        revision: "review-r7",
        snapshot: {
          status: "running",
          agent: { display_name: "Reviewer" },
          instruction: "Review the authentication boundary",
          started_at: "2026-07-16T00:00:00Z",
          iteration: 2,
          max_iterations: 5,
          paused: false,
        },
      },
      {
        id: "build.active",
        data_source: "build.active",
        snapshot: {
          status: "warning",
          name: "Web bundle",
          completed: 48,
          total: 100,
          detail: "Retrying chunk 4",
        },
      },
      {
        id: "upload.active",
        data_source: "upload.active",
        snapshot: { status: "complete", name: "Artifacts" },
      },
    ],
    status_surfaces: [
      {
        id: "review-gate",
        surface_id: "review-gate",
        api_version: STATUS_SURFACE_API_VERSION,
        slot: "above_composer",
        priority: 100,
        data_source: "review.active",
        visible_when: { status: ["running", "paused"] },
        icon: "clock",
        title: "Review",
        title_path: "agent.display_name",
        summary_path: "instruction",
        status_path: "status",
        timer_from_path: "started_at",
        progress: { current_path: "iteration", total_path: "max_iterations" },
        details: [{ label: "Instruction", path: "instruction" }],
        controls: [
          { id: "pause", type: "toggle_button", label: "Pause", action_id: "review.pause", value_path: "paused" },
          { id: "cancel", type: "button", label: "Cancel", action_id: "review.cancel" },
          { id: "details", type: "expand", label: "Details" },
        ],
        template_id: "fixture.review",
        trust_level: "local",
      },
      {
        id: "build-progress",
        surface_id: "build-progress",
        slot: "above_composer",
        priority: 50,
        data_source: "build.active",
        icon: "build",
        title_path: "name",
        summary_path: "detail",
        status_path: "status",
        progress: { current_path: "completed", total_path: "total" },
        controls: [{ id: "stop", type: "button", label: "Stop", action_id: "build.stop" }],
        template_id: "fixture.build",
      },
      {
        id: "completed-upload",
        surface_id: "completed-upload",
        slot: "above_composer",
        data_source: "upload.active",
        visible_when: { status: ["uploading", "failed"] },
        title_path: "name",
      },
    ],
  };
}

test("resolves two unrelated data-source-driven surfaces in deterministic priority order", () => {
  const result = resolveStatusSurfaces(catalogFixture());
  assert.deepEqual(result.surfaces.map((surface) => surface.id), ["review-gate", "build-progress"]);
  assert.equal(result.surfaces[0].title, "Reviewer");
  assert.equal(result.surfaces[0].summary, "Review the authentication boundary");
  assert.deepEqual(result.surfaces[0].progress, { current: 2, total: 5, label: undefined });
  assert.equal(result.surfaces[0].sourceRevision, "review-r7");
  assert.equal(result.surfaces[1].severity, "warning");
  assert.equal(result.diagnostics.length, 0);
});

test("only returns surfaces for an approved requested slot", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces?.push({
    id: "sidebar-build",
    slot: "sidebar",
    data_source: "review.active",
    title: "Sidebar build",
  });
  assert.deepEqual(statusSurfacesForSlot(catalog, "sidebar").map((surface) => surface.id), ["sidebar-build"]);
  assert.deepEqual(statusSurfacesForSlot(catalog, "chat_header"), []);
});

test("unknown controls fail closed to visible provenance-rich fallback", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces = [{
    id: "unsafe-control",
    slot: "above_composer",
    data_source: "review.active",
    title: "Unsafe",
    controls: [{ type: "javascript", callback: "alert(1)" }],
    template_id: "fixture.unsafe",
    source_pack_id: "fixture-pack",
    trust_level: "untrusted",
  }];
  const [surface] = resolveStatusSurfaces(catalog).surfaces;
  assert.equal(surface.unsupported, true);
  assert.equal(surface.title, "Unsupported status surface");
  assert.equal(surface.diagnostics[0].code, "status_surface.unknown_control");
  assert.equal(surface.templateId, "fixture.unsafe");
  assert.equal(surface.sourcePackId, "fixture-pack");
  assert.equal(surface.trustLevel, "untrusted");
  assert.deepEqual(surface.controls, []);
});

test("unregistered data sources and actions never become executable controls", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces = [{
    id: "forged",
    slot: "above_composer",
    data_source: "https://evil.invalid/state",
    title: "Forged",
    controls: [{ type: "button", action_id: "https://evil.invalid/run", label: "Run" }],
  }];
  const [surface] = resolveStatusSurfaces(catalog).surfaces;
  assert.equal(surface.unsupported, true);
  assert.equal(surface.controls.length, 0);
  assert.ok(surface.diagnostics.some((item) => item.code === "status_surface.invalid_data_source"));
  assert.ok(surface.diagnostics.some((item) => item.code === "status_surface.unregistered_action"));
});

test("generic action metadata is not mistaken for a resolved command registration", () => {
  const catalog = catalogFixture();
  catalog.actions = [{
    id: "claimed.action",
    action_id: "claimed.action",
    execution: { type: "pack_block" },
  }];
  catalog.status_surfaces = [{
    id: "claimed",
    slot: "above_composer",
    data_source: "review.active",
    title: "Claimed action",
    controls: [{ type: "button", action_id: "claimed.action", label: "Run" }],
  }];
  const [surface] = resolveStatusSurfaces(catalog).surfaces;
  assert.equal(surface.unsupported, true);
  assert.equal(surface.controls.length, 0);
  assert.equal(surface.diagnostics[0].code, "status_surface.unregistered_action");
});

test("prototype and expression-like paths fail closed", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces = [{
    id: "bad-path",
    slot: "above_composer",
    data_source: "review.active",
    title_path: "constructor.prototype.polluted",
  }];
  const [surface] = resolveStatusSurfaces(catalog).surfaces;
  assert.equal(surface.unsupported, true);
  assert.equal(surface.diagnostics[0].code, "status_surface.invalid_path");
  assert.equal(readStatusSurfacePath({ safe: { value: 1 } }, "safe.value"), 1);
  assert.equal(readStatusSurfacePath({}, "__proto__.polluted"), undefined);
  assert.equal(readStatusSurfacePath({}, "items[0]"), undefined);
});

test("malformed IDs and oversized control collections fail closed", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces = [{
    id: "invalid surface id",
    slot: "above_composer",
    data_source: "review.active",
    controls: Array.from({ length: 21 }, (_, index) => ({
      id: `control-${index}`,
      type: "button",
      action_id: "review.pause",
    })),
  }];
  const [surface] = resolveStatusSurfaces(catalog).surfaces;
  assert.equal(surface.unsupported, true);
  assert.deepEqual(surface.controls, []);
  assert.ok(surface.diagnostics.some((item) => item.code === "status_surface.invalid_id"));
  assert.ok(surface.diagnostics.some((item) => item.code === "status_surface.too_many_controls"));
});

test("unsupported API versions render a deterministic fallback", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces = [{
    id: "future",
    slot: "above_composer",
    data_source: "review.active",
    api_version: "rumi.status_surface.v99",
    title: "Future",
  }];
  const [surface] = resolveStatusSurfaces(catalog).surfaces;
  assert.equal(surface.unsupported, true);
  assert.equal(surface.diagnostics[0].code, "status_surface.incompatible_version");
});

test("duplicate IDs retain the highest-priority declaration and emit diagnostics", () => {
  const catalog = catalogFixture();
  catalog.status_surfaces = [
    { id: "same", slot: "above_composer", priority: 1, data_source: "review.active", title: "Low" },
    { id: "same", slot: "above_composer", priority: 10, data_source: "review.active", title: "High" },
  ];
  const result = resolveStatusSurfaces(catalog);
  assert.equal(result.surfaces.length, 1);
  assert.equal(result.surfaces[0].title, "High");
  assert.ok(result.diagnostics.some((item) => item.code === "status_surface.duplicate_id"));
});

test("disabled template declarations disappear immediately", () => {
  const catalog = catalogFixture();
  if (catalog.status_surfaces) catalog.status_surfaces[0].enabled = false;
  assert.deepEqual(resolveStatusSurfaces(catalog).surfaces.map((surface) => surface.id), ["build-progress"]);
});
