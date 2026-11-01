import assert from "node:assert/strict";
import test from "node:test";
import { viewOperationRequest, viewsForSlot } from "./catalogViewRegistry";
import { surfaceFixture, tenPatternTemplate } from "./surfaceTemplateFixture";
import {
  SURFACE_EVENTS, normalizeSurfaceIntent, parseSurfaceOutcome, parseSurfaceResource,
  parseSurfaceTemplate, surfacePublicInput,
} from "./surfaceTemplateContract";
import { surfaceCollection, surfaceRequestPayload } from "./surfaceTemplateState";

const values = (pattern: string) => ({
  choice: { selected: ["one"] }, form: { fields: { title: "Review", count: 1, enabled: false } },
  collection: { selected_id: "record" }, resource_input: { resource: {
    version: "tobkiri.ui.surface-resource.v1", selection_id: "x".repeat(43), kind: "file",
    display_name: "Selected resource", stage: "exchanged", expires_at_ms: Date.now() + 30000,
  } },
}[pattern] ?? {});

for (const pattern of Object.keys(SURFACE_EVENTS)) {
  test(`${pattern} has the same typed intent/outcome through both captured trusted renderers`, () => {
    const standard = surfaceFixture(); const compact = surfaceFixture("semantic_compact");
    const data = values(pattern);
    const first = normalizeSurfaceIntent(standard.template, pattern, "apply", data)!;
    const second = normalizeSurfaceIntent(compact.template, pattern, "apply", data)!;
    assert.ok(first); assert.deepEqual(first, second);
    const request = standard.template.nodes.find((node) => node.id === pattern)!.intents![0].request;
    const payload = surfaceRequestPayload(request, {}, {}, { surface_intent: first })!;
    assert.deepEqual(viewOperationRequest(standard.catalog, standard.registered, request.operation, payload),
      viewOperationRequest(compact.catalog, compact.registered, request.operation, payload));
    const outcome = { ...first, version: "tobkiri.ui.surface-outcome.v1", status: "accepted" } as Record<string, unknown>;
    delete outcome.values;
    assert.ok(parseSurfaceOutcome(outcome, first));
    assert.deepEqual(parseSurfaceOutcome(outcome, first), parseSurfaceOutcome(outcome, second));
  });
}

for (const mutation of [
  { version: "tobkiri.ui.surface-template.v2" }, { renderer_api_version: "2.0.0" },
  { url: "https://evil.test" }, { code: "alert(1)" }, { nodes: [{ id: "unsafe", pattern: "svg", label: "Unsafe" }] },
]) test("unknown version/pattern/executable surface fields fail closed " + JSON.stringify(mutation), () => {
  assert.equal(parseSurfaceTemplate({ ...tenPatternTemplate(), ...mutation }), null);
});

test("identity collision, prototype bindings, nested authority and fixed-value collision reject registration", () => {
  const value = tenPatternTemplate(); value.nodes.push(value.nodes[0]);
  assert.equal(parseSurfaceTemplate(value), null);
  for (const key of ["approved", "profile_id", "resource_handle", "permissions", "_secret"]) {
    const invalid = tenPatternTemplate(); invalid.nodes[0].intents![0].request.input = { nested: { [key]: "forged" } };
    assert.equal(parseSurfaceTemplate(invalid), null);
  }
  const invalid = tenPatternTemplate(); invalid.nodes[0].intents![0].request.source_bindings = { surface_intent: "constructor.name" };
  assert.equal(parseSurfaceTemplate(invalid), null);
  assert.equal(surfacePublicInput({ cyclic: Number.NaN }), false);
});

test("typed forms/choices and outcome identity cannot widen or confirm unverified action results", () => {
  const template = tenPatternTemplate();
  assert.equal(normalizeSurfaceIntent(template, "choice", "apply", { selected: ["missing"] }), null);
  assert.equal(normalizeSurfaceIntent(template, "choice", "apply", { selected: ["one", "two"] }), null);
  assert.equal(normalizeSurfaceIntent(template, "form", "apply", { fields: { title: "Review", count: true } }), null);
  assert.equal(normalizeSurfaceIntent(template, "form", "apply", { fields: { title: "Review", count: 11 } }), null);
  const intent = normalizeSurfaceIntent(template, "confirmation", "apply", {})!;
  assert.equal(parseSurfaceOutcome({ status: "pending_approval", approved: true }, intent), null);
  const outcome = { ...intent, version: "tobkiri.ui.surface-outcome.v1", status: "accepted" } as Record<string, unknown>;
  delete outcome.values;
  assert.equal(parseSurfaceOutcome({ ...outcome, node_id: "other" }, intent), null);
});

test("source/context bindings retain explicit CAS and require own data without overriding intent", () => {
  const { template } = surfaceFixture();
  const request = { ...template.nodes[0].intents![0].request,
    source_bindings: { expected_revision: "revision" }, context_bindings: { conversation_id: "conversation_id" as const } };
  assert.deepEqual(surfaceRequestPayload(request, { revision: 4 }, { conversation_id: "child" }, { surface_intent: {} }),
    { operation: "apply", expected_revision: 4, conversation_id: "child", surface_intent: {} });
  assert.equal(surfaceRequestPayload(request, Object.create({ revision: 4 }), { conversation_id: "child" }), null);
  assert.equal(surfaceRequestPayload(request, { revision: 4 }, {}), null);
});

test("resource tokens are typed data, expiry and wrong kind/stage/forgery reject predispatch", () => {
  const resource = values("resource_input").resource!;
  assert.ok(parseSurfaceResource(resource, "file", "exchanged"));
  for (const changed of [{ selection_id: "handle:forged" }, { kind: "image" }, { stage: "selected" }, { approved: true }, { expires_at_ms: 1 }]) {
    assert.equal(parseSurfaceResource({ ...resource, ...changed }, "file", "exchanged"), null);
  }
  const fixture = surfaceFixture();
  const acquire = fixture.template.nodes.find((node) => node.resource)!.resource!.acquire;
  assert.equal(viewOperationRequest(fixture.catalog, fixture.registered, acquire.operation, acquire.input!), null);
});

test("collection rejects duplicate IDs and cannot read inherited or executable values", () => {
  const node = tenPatternTemplate().nodes.find((item) => item.pattern === "collection")!;
  assert.deepEqual(surfaceCollection(node, { records: [{ id: "one", label: "First" }] }), [{ id: "one", label: "First" }]);
  assert.equal(surfaceCollection(node, { records: [{ id: "one", label: "First" }, { id: "one", label: "Second" }] }), null);
  assert.equal(surfaceCollection(node, { records: [Object.create({ id: "one", label: "Inherited" })] }), null);
});

test("surface registration collision and disabled Logic operations remain unavailable", () => {
  const fixture = surfaceFixture();
  assert.equal(viewsForSlot(fixture.catalog, "workspace_tab", fixture.catalog.plan_hash).length, 1);
  fixture.catalog.contributions.push({ ...fixture.item });
  assert.equal(viewsForSlot(fixture.catalog, "workspace_tab", fixture.catalog.plan_hash).length, 0);
});
