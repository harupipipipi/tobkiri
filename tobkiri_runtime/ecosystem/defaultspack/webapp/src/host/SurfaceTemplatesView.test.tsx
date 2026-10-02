import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { SurfaceTemplatesView } from "./SurfaceTemplatesView";
import { surfaceFixture } from "./surfaceTemplateFixture";
import { SURFACE_EVENTS } from "./surfaceTemplateContract";

const snapshot = { progress: { value: 2, total: 4 }, records: [{ id: "record", label: "Real item" }] };
const capabilities = { invokeAction: async () => { throw new Error("render cannot write"); }, readDataSource: async () => ({}) };
for (const renderer of ["semantic_standard", "semantic_compact"] as const) {
  test(`${renderer} renders all ten patterns accessibly with static reduced-motion-safe presentation`, () => {
    const fixture = surfaceFixture(renderer);
    const html = renderToStaticMarkup(<SurfaceTemplatesView {...fixture} snapshot={snapshot} sourceReady
      capabilities={capabilities} onRefresh={() => {}} />);
    for (const pattern of Object.keys(SURFACE_EVENTS)) assert.match(html, new RegExp(`data-surface-pattern="${pattern}"`));
    assert.match(html, /data-surface-motion="static"/);
    assert.match(html, /<progress aria-label="Progress" value="2" max="4"/);
    assert.match(html, /<fieldset/); assert.match(html, /<legend>Form/); assert.match(html, /<label[^>]*>Title/);
    assert.match(html, /Resource acquisition provider is unavailable/);
    assert.doesNotMatch(html, /<script|<svg|<iframe|style=|animation|transition/);
  });
}

test("absent renderer and stale activation render unavailable instead of builtin success", () => {
  const fixture = surfaceFixture(); fixture.catalog.contributions = fixture.catalog.contributions.filter((item) => item.kind !== "renderer");
  assert.match(renderToStaticMarkup(<SurfaceTemplatesView {...fixture} snapshot={snapshot} sourceReady capabilities={capabilities} onRefresh={() => {}} />), /renderer is unavailable/);
});

test("surface copy is escaped and progress is never invented without a valid source", () => {
  const fixture = surfaceFixture(); fixture.template.nodes[0].body = "<script>bad()</script>";
  const html = renderToStaticMarkup(<SurfaceTemplatesView {...fixture} snapshot={{}} sourceReady={false} capabilities={capabilities} onRefresh={() => {}} />);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Progress is unavailable/); assert.doesNotMatch(html, /<progress/);
});
