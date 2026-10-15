import assert from "node:assert/strict";
import test from "node:test";
import { matchesViewReference, viewsForSlot, viewOperationRequest } from "./catalogViewRegistry";
import { surfaceFixture } from "./surfaceTemplateFixture";
import { matchesSurfaceRendererCapture, resolveSurfaceRenderer } from "./surfaceRendererCapture";

test("actual catalog projection pins renderer API/artifact/build/descriptor and scope", () => {
  const fixture = surfaceFixture();
  const resolved = resolveSurfaceRenderer(fixture.catalog, fixture.template)!;
  assert.equal(resolved.capture.rendererArtifactHash, fixture.renderer.owner_pack_hash);
  assert.equal(resolved.capture.rendererApiVersion, "1.0.0");
  assert.equal(fixture.registered.reference.rendererDescriptorHash, fixture.renderer.descriptor_hash);
  assert.ok(matchesSurfaceRendererCapture(fixture.catalog, fixture.template, resolved.capture));
});

test("renderer update, disable, uninstall, collision, rollback, unknown API and expiry never fall back", () => {
  const fixture = surfaceFixture();
  const capture = resolveSurfaceRenderer(fixture.catalog, fixture.template)!.capture;
  for (const field of ["owner_pack_hash", "descriptor_hash", "build_identity", "resolved_profile_id", "resolved_profile_revision", "resolved_activation_id", "resolved_plan_hash"] as const) {
    const old = fixture.renderer[field]; fixture.renderer[field] = "changed";
    assert.equal(matchesSurfaceRendererCapture(fixture.catalog, fixture.template, capture), false);
    fixture.renderer[field] = old;
  }
  fixture.catalog.quarantined_pack_ids = [fixture.renderer.owner_pack_id];
  assert.equal(resolveSurfaceRenderer(fixture.catalog, fixture.template), null);
  fixture.catalog.quarantined_pack_ids = [];
  fixture.catalog.contributions.push({ ...fixture.renderer });
  assert.equal(resolveSurfaceRenderer(fixture.catalog, fixture.template), null);
  fixture.catalog.contributions.pop();
  fixture.renderer.resolved_expires_at_ms = Date.now() - 1;
  assert.equal(viewsForSlot(fixture.catalog, "workspace_tab", fixture.catalog.plan_hash).length, 0);
  const operation = fixture.template.nodes[0].intents![0].request.operation;
  assert.equal(viewOperationRequest(fixture.catalog, fixture.registered, operation, {}), null);
  fixture.catalog.contributions = fixture.catalog.contributions.filter((item) => item.kind !== "renderer");
  assert.equal(resolveSurfaceRenderer(fixture.catalog, fixture.template), null);
});

test("rollback reinstates artifact data but cannot rebind a capture from another activation", () => {
  const fixture = surfaceFixture();
  const capture = resolveSurfaceRenderer(fixture.catalog, fixture.template)!.capture;
  fixture.catalog.activation_id = "next";
  fixture.renderer.resolved_activation_id = "next";
  // The renderer pin alone is data; the complete view capture still rejects.
  assert.ok(matchesSurfaceRendererCapture(fixture.catalog, fixture.template, capture));
  assert.equal(viewOperationRequest(fixture.catalog, fixture.registered,
    fixture.template.nodes[0].intents![0].request.operation, {}), null);
});

test("catalog refresh preserves the original capture deadline without extending it", () => {
  const fixture = surfaceFixture();
  const capture = resolveSurfaceRenderer(fixture.catalog, fixture.template)!.capture;
  fixture.renderer.resolved_expires_at_ms! += 1000;
  assert.ok(matchesSurfaceRendererCapture(fixture.catalog, fixture.template, capture));
  const refreshed = viewsForSlot(fixture.catalog, "workspace_tab", fixture.catalog.plan_hash)[0];
  assert.ok(matchesViewReference(refreshed, fixture.registered.reference));
  assert.equal(fixture.registered.reference.rendererExpiresAtMs, capture.rendererExpiresAtMs);
  assert.equal(matchesSurfaceRendererCapture(fixture.catalog, fixture.template, capture, capture.rendererExpiresAtMs), false);
  assert.equal(matchesViewReference(refreshed, { ...fixture.registered.reference, rendererExpiresAtMs: 1 }), false);
});

test("unknown renderer API/implementation or incomplete pattern coverage stays unavailable", () => {
  for (const change of [{ api_version: "2.0.0" }, { implementation: "untrusted" }, { patterns: ["content"] }]) {
    const fixture = surfaceFixture(); fixture.renderer.view = { ...fixture.renderer.view, ...change };
    assert.equal(resolveSurfaceRenderer(fixture.catalog, fixture.template), null);
    assert.equal(viewsForSlot(fixture.catalog, "workspace_tab", fixture.catalog.plan_hash).length, 0);
  }
});
