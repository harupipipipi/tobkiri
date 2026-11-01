import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { RecordEditorView } from "./RecordEditorView";
import { viewReference, type CatalogView, type RegisteredCatalogView } from "./catalogViewRegistry";
import type { FrontendCatalog, VerifiedFrontendContribution } from "./frontendContracts";

const item: VerifiedFrontendContribution = {
  contribution_id: "fixture.editor", kind: "view", mode: "declarative", label: "Records", priority: 1,
  owner_pack_id: "fixture-pack", owner_pack_hash: "sha256:fixture", build_identity: "test",
  resolved_profile_id: "fixture", resolved_profile_revision: "r1", resolved_activation_id: "a1",
  resolved_plan_hash: "p1", descriptor_hash: "d1", localization: {},
  accessibility: { name: "Records", keyboard: true },
};
const action: VerifiedFrontendContribution = { ...item, contribution_id: "fixture.manage", kind: "action",
  action_contract: "tobkiri.action.fixture.v1", operation_id: "fixture.manage" };
const catalog: FrontendCatalog = { version: "rumi.ui.contribution.v1", profile_id: "fixture",
  profile_revision: "r1", activation_id: "a1", plan_hash: "p1", selected_entry_route: "/chat",
  contributions: [item, action], diagnostics: [], quarantined_pack_ids: [], catalog_hash: "c1" };
const operation = { contribution_id: "fixture.manage", contract_id: "tobkiri.action.fixture.v1", operation_id: "fixture.manage" };
const view: CatalogView = { version: "tobkiri.ui.view.v1", renderer: "record_editor", slot: "workspace_tab",
  record_editor: { records_path: "records", id_path: "id", title_path: "name", search_paths: ["name"],
    columns: [{ label: "Name", path: "name", kind: "text" }, { label: "State", path: "status", kind: "status" }],
    fields: [{ id: "name", label: "Name", path: "name", kind: "text" }],
    save: { operation, input: { operation: "update" }, source_bindings: { expected_revision: "revision" },
      record_bindings: { record_id: "id" }, draft_key: "updates" },
    actions: [{ id: "pause", label: "Pause", operation, input: { operation: "pause" },
      source_bindings: { expected_revision: "revision" }, record_bindings: { record_id: "id" } }],
  } };
const registered: RegisteredCatalogView = { item, view, reference: viewReference(catalog, item) };
const capabilities = { invokeAction: async () => { throw new Error("rendering must not write"); }, readDataSource: async () => ({}) };
const render = (snapshot: unknown, current = catalog) => renderToStaticMarkup(<RecordEditorView
  registered={registered} catalog={current} capabilities={capabilities} snapshot={snapshot} onRefresh={() => {}} />);

test("renderer displays real records and accessible native actions without writing", () => {
  const html = render({ revision: 3, records: [{ id: "one", name: "Review", status: "failed" }] });
  assert.match(html, /Search records/);
  assert.match(html, /1 of 1 records/);
  assert.match(html, /failed/);
  assert.match(html, /aria-label="Edit Review"/);
  assert.match(html, /aria-label="Pause Review"/);
});

test("provider unavailable and duplicate records remain explicitly unavailable", () => {
  assert.match(render(null), /data source is unavailable/);
  assert.match(render({ records: [{ id: "one" }, { id: "one" }] }), /data source is unavailable/);
});

test("unregistered or stale action targets are disabled", () => {
  const html = render({ revision: 3, records: [{ id: "one", name: "Review" }] }, { ...catalog, activation_id: "new" });
  assert.match(html, /disabled="" aria-label="Pause Review"/);
});

test("read failure retains visible records while source readiness disables mutations", () => {
  const html = renderToStaticMarkup(<RecordEditorView registered={registered} catalog={catalog}
    capabilities={capabilities} snapshot={{ revision: 3, records: [{ id: "one", name: "Review" }] }}
    onRefresh={() => {}} sourceReady={false} />);
  assert.match(html, /1 of 1 records/);
  assert.match(html, /disabled="" aria-label="Pause Review"/);
});

test("record labels are escaped text and do not become executable markup", () => {
  const html = render({ revision: 3, records: [{ id: "one", name: "<script>bad()</script>" }] });
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
