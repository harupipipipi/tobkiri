import assert from "node:assert/strict";
import test from "node:test";
import type { CapturedCapabilityInvocation } from "./frontendContracts";
import { surfaceFixture } from "./surfaceTemplateFixture";
import { normalizeSurfaceIntent, parseSurfaceOutcome } from "./surfaceTemplateContract";
import { dispatchSurfaceRequest } from "./surfaceTemplateDispatch";

test("confirmation intent cannot approve itself and uses the normal captured invoker", async () => {
  const fixture = surfaceFixture();
  const intent = normalizeSurfaceIntent(fixture.template, "confirmation", "apply", {})!;
  const request = fixture.template.nodes.find((node) => node.id === "confirmation")!.intents![0].request;
  const calls: CapturedCapabilityInvocation[] = [];
  const capabilities = {
    invokeAction: async (value: CapturedCapabilityInvocation) => { calls.push(value); return { state: "approval_required" }; },
    readDataSource: async () => { throw new Error("an explicit action cannot auto-read"); },
  };
  const result = await dispatchSurfaceRequest(fixture.catalog, fixture.registered, capabilities, request,
    {}, {}, { surface_intent: intent }, () => true);
  assert.deepEqual(result, { kind: "approval" });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].payload, { operation: "apply", surface_intent: intent });
  assert.equal(JSON.stringify(calls[0]).includes('"approved"'), false);
  assert.equal(parseSurfaceOutcome(result, intent), null);
});

test("both renderers return the same confirmed outcome only after actual admitted invocation", async () => {
  for (const implementation of ["semantic_standard", "semantic_compact"] as const) {
    const fixture = surfaceFixture(implementation);
    const intent = normalizeSurfaceIntent(fixture.template, "content", "apply", {})!;
    const { values: _values, ...identity } = intent;
    const outcome = { ...identity, version: "tobkiri.ui.surface-outcome.v1", status: "accepted" };
    const capabilities = { invokeAction: async () => outcome, readDataSource: async () => ({}) };
    const result = await dispatchSurfaceRequest(fixture.catalog, fixture.registered, capabilities,
      fixture.template.nodes[0].intents![0].request, {}, {}, { surface_intent: intent }, () => true);
    assert.equal(result?.kind, "completed");
    assert.ok(result?.kind === "completed" && parseSurfaceOutcome(result.result, intent));
  }
});

test("unbound resource and a renderer disabled during invocation never confirm completion", async () => {
  const fixture = surfaceFixture(); let calls = 0;
  const capabilities = { invokeAction: async () => { calls += 1; fixture.catalog.quarantined_pack_ids = [fixture.renderer.owner_pack_id]; return {}; },
    readDataSource: async () => ({}) };
  const acquire = fixture.template.nodes.find((node) => node.resource)!.resource!.acquire;
  assert.equal(await dispatchSurfaceRequest(fixture.catalog, fixture.registered, capabilities, acquire, {}, {}, {}, () => true), null);
  assert.equal(calls, 0);
  assert.equal(await dispatchSurfaceRequest(fixture.catalog, fixture.registered, capabilities,
    fixture.template.nodes[0].intents![0].request, {}, {}, {}, () => true), null);
  assert.equal(calls, 1);
});
