import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import {
  choicePayload, controlPayload, matchesViewReference, parseCatalogView,
  readViewPath, requestContextInput, validPublicViewInput,
  viewChoices, viewOperationRequest, viewsForSlot,
  type CatalogView, type ViewControl,
} from "./catalogViewRegistry";
import { FrontendViewSlot } from "./FrontendViewSlot";
import type { FrontendCatalog, VerifiedFrontendContribution } from "./frontendContracts";

const operation = {
  contribution_id: "pack.logic.logic.read", contract_id: "example.resource.v1",
  operation_id: "logic.read",
};
const view: CatalogView = {
  version: "tobkiri.ui.view.v1", slot: "sidebar", renderer: "panel",
  title: "Public state", data_source: operation,
  fields: [{ label: "State", path: "state", kind: "status" }],
};
const contribution = (overrides: Partial<VerifiedFrontendContribution> = {}): VerifiedFrontendContribution => ({
  contribution_id: "surface.public.view", kind: "view", mode: "declarative",
  label: "Public view", priority: 0, owner_pack_id: "surface",
  owner_pack_hash: "digest", build_identity: "build",
  resolved_profile_id: "profile", resolved_profile_revision: "revision",
  resolved_activation_id: "activation", resolved_plan_hash: "plan",
  descriptor_hash: "descriptor", view,
  localization: {}, accessibility: { name: "Public view", keyboard: true },
  ...overrides,
});
const catalog = (): FrontendCatalog => ({
  version: "rumi.ui.contribution.v1", profile_id: "profile",
  profile_revision: "revision", activation_id: "activation", plan_hash: "plan",
  selected_entry_route: "/", catalog_hash: "catalog", quarantined_pack_ids: [],
  diagnostics: [], contributions: [
    contribution(),
    contribution({ contribution_id: operation.contribution_id, kind: "action",
      owner_pack_id: "logic", action_contract: operation.contract_id,
      operation_id: operation.operation_id, view: null }),
  ],
});

test("two unrelated Surface Packs reuse the same fixed renderer without feature imports", () => {
  const value = catalog();
  value.contributions.push(contribution({ contribution_id: "upload.public.view", owner_pack_id: "upload", priority: 2 }));
  assert.deepEqual(viewsForSlot(value, "sidebar", "plan").map((item) => item.item.owner_pack_id), ["upload", "surface"]);
});

for (const [name, mutation] of Object.entries({
  version: { version: "tobkiri.ui.view.v2" },
  renderer: { renderer: "https://example.test/module.js" },
  slot: { slot: "authority_approval" },
  arbitrary_url: { url: "/api/run" },
  code: { onClick: "alert(1)" },
  schema: { fields: [{ label: "Bad", path: "state()", kind: "text" }] },
  inherited_path: { fields: [{ label: "Bad", path: "constructor.name", kind: "text" }] },
  undeclared_control: { controls: [{ id: "unsafe", label: "Unsafe", kind: "html", operation }] },
})) {
  test(`unknown/executable ${name} fails closed`, () => {
    assert.equal(parseCatalogView({ ...view, ...mutation }), null);
  });
}

test("slot/version/profile/revision/activation/plan/quarantine bindings are enforced", () => {
  for (const key of ["resolved_profile_id", "resolved_profile_revision", "resolved_activation_id", "resolved_plan_hash"] as const) {
    const value = catalog();
    value.contributions[0][key] = "stale";
    assert.equal(viewsForSlot(value, "sidebar", "plan").length, 0);
  }
  const value = catalog();
  assert.equal(viewsForSlot(value, "settings", "plan").length, 0);
  assert.equal(viewsForSlot(value, "sidebar", "stale").length, 0);
  value.quarantined_pack_ids = ["surface"];
  assert.equal(viewsForSlot(value, "sidebar", "plan").length, 0);
});

test("collisions reject every candidate and removal cannot leave a live registration", () => {
  const value = catalog();
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  value.contributions.push(contribution({ owner_pack_id: "shadow" }));
  assert.equal(viewsForSlot(value, "sidebar", "plan").length, 0);
  value.contributions = value.contributions.filter((item) => item.kind !== "view");
  assert.equal(viewOperationRequest(value, registered, operation, {}), null);
});

test("stale descriptor/catalog references cannot survive a tab restoration", () => {
  const value = catalog();
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  assert.equal(matchesViewReference(registered, { ...registered.reference, descriptorHash: "old" }), false);
  value.catalog_hash = "new";
  assert.equal(viewOperationRequest(value, registered, operation, {}), null);
});

test("public operations bind to the Logic Pack identity and ignore Surface ownership", () => {
  const value = catalog();
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  const request = viewOperationRequest(value, registered, operation, { operation: "get" });
  assert.equal(request?.ownerPackId, "logic");
  assert.equal(request?.profileId, "profile");
  assert.equal(request?.contributionId, operation.contribution_id);
  assert.equal(viewOperationRequest(value, registered, { ...operation, operation_id: "logic.write" }, {}), null);
  value.contributions.pop();
  assert.equal(viewOperationRequest(value, registered, operation, {}), null);
});

test("duplicate/mismatched/unready targets remain unavailable", () => {
  const value = catalog();
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  value.contributions.push({ ...value.contributions[1] });
  assert.equal(viewOperationRequest(value, registered, operation, {}), null);
  value.contributions.pop();
  value.contributions[1].action_contract = "wrong.v1";
  assert.equal(viewOperationRequest(value, registered, operation, {}), null);
});

test("nested identity/private/prototype hints and nonfinite values are rejected", () => {
  for (const key of ["profile_id", "approved", "_session_id", "constructor", "__proto__"]) {
    assert.equal(validPublicViewInput({ nested: { [key]: true } }), false);
  }
  assert.equal(validPublicViewInput({ count: Infinity }), false);
  assert.equal(validPublicViewInput({ text: "x".repeat(16385) }), false);
  assert.equal(readViewPath({ state: "ready" }, "state"), "ready");
  assert.equal(readViewPath(Object.create({ secret: "leaked" }), "secret"), undefined);
});

const choice: ViewControl = {
  id: "pick", label: "Choose item", kind: "choice", operation,
  value_key: "item_id", options_path: "items", id_path: "id", label_path: "label",
  disabled_path: "disabled", input_bindings: { expected_revision: "revision" },
};
test("disabled/unknown/duplicate IDs cannot be submitted and failed mutation leaves source unchanged", () => {
  const snapshot = { revision: 3, items: [
    { id: "a", label: "Allowed" }, { id: "b", label: "Denied", disabled: true },
    { id: "dup", label: "Duplicate" }, { id: "dup", label: "Duplicate" },
  ] };
  const before = JSON.stringify(snapshot);
  assert.deepEqual(choicePayload(choice, snapshot, ["a"]), { item_id: "a", expected_revision: 3 });
  assert.equal(choicePayload(choice, snapshot, ["b"]), null);
  assert.equal(choicePayload(choice, snapshot, ["unknown"]), null);
  assert.equal(choicePayload(choice, snapshot, ["dup"]), null);
  assert.equal(choicePayload(choice, snapshot, ["a", "a"]), null);
  assert.equal(JSON.stringify(snapshot), before);
  assert.equal(viewChoices(choice, { items: new Array(257).fill({ id: "a", label: "a" }) }).length, 0);
});

test("CAS/source/context bindings must all be present and cannot overwrite constants", () => {
  assert.equal(controlPayload(choice, {}, "a"), null);
  assert.deepEqual(requestContextInput({ context_bindings: { conversation_id: "conversation_id" } },
    { conversation_id: "chat-1" }), { conversation_id: "chat-1" });
  assert.equal(requestContextInput({ context_bindings: { conversation_id: "conversation_id" } }, {}), null);
  assert.equal(parseCatalogView({ ...view, controls: [{ ...choice, input: { item_id: "forged" } }] }), null);
});

test("shipped slot renderer escapes Pack text and exposes unavailable state with keyboard controls", () => {
  const value = catalog();
  value.contributions[0].view = { ...view, title: "<script>bad()</script>" };
  const markup = renderToStaticMarkup(<FrontendViewSlot catalog={value} slot="sidebar" activePlanHash="plan"
    capabilities={{ invokeAction: async () => ({}), readDataSource: async () => ({}) }} />);
  assert.match(markup, /&lt;script&gt;/);
  assert.doesNotMatch(markup, /<script>/);
  assert.match(markup, /type="button"/);
  const reference = viewsForSlot(value, "sidebar", "plan")[0].reference;
  const stale = renderToStaticMarkup(<FrontendViewSlot catalog={value} slot="sidebar" activePlanHash="plan"
    contributionId="surface.public.view" reference={{ ...reference, profileId: "other" }}
    capabilities={{ invokeAction: async () => ({}), readDataSource: async () => ({}) }} />);
  assert.match(stale, /view-unavailable/);
});
