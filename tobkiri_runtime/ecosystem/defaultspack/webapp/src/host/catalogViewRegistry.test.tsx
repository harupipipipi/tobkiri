import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import {
  choicePayload, controlPayload, matchesViewReference, parseCatalogView,
  readViewPath, requestContextInput, validPublicViewInput,
  viewChoices, viewOperationRequest, viewReadRequest, viewsForSlot, viewContextKey,
  type CatalogView, type ConversationThreadDefinition, type ViewControl,
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
      operation_id: operation.operation_id, read_only: true, view: null }),
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

test("automatic sources cannot invoke mutations while explicit controls retain captured invocation", () => {
  const value = catalog();
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  assert.ok(viewReadRequest(value, registered, operation, {}));
  value.contributions[1].read_only = false;
  assert.equal(viewReadRequest(value, registered, operation, {}), null);
  assert.ok(viewOperationRequest(value, registered, operation, {}));
  delete value.contributions[1].read_only;
  assert.equal(viewReadRequest(value, registered, operation, {}), null);
});

test("nested identity/private/prototype hints and nonfinite values are rejected", () => {
  for (const key of ["profile_revision", "approved", "_session_id", "constructor", "__proto__"]) {
    assert.equal(validPublicViewInput({ nested: { [key]: true } }), false);
  }
  assert.equal(validPublicViewInput({ profile_id: "execution-override" }), false);
  assert.equal(validPublicViewInput({ model_policy: { fixed: { profile_id: "model-target" } } }), true);
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

const editor = {
  records_path: "items", id_path: "id", title_path: "title",
  fields: [{ id: "title", label: "Title", path: "title", kind: "text" }],
  save: { operation, draft_key: "updates", record_bindings: { item_id: "id", expected_revision: "revision" } },
  actions: [{ id: "pause", label: "Pause", operation, input: { operation: "pause" },
    record_bindings: { item_id: "id" }, available_when: { path: "state", equals: "running" } }],
};
test("record editor admits only finite field/action/data bindings and shipped renderer", () => {
  assert.ok(parseCatalogView({ ...view, renderer: "record_editor", record_editor: editor }));
  for (const unsafe of [
    { ...editor, fields: [...editor.fields, { ...editor.fields[0] }] },
    { ...editor, fields: [{ ...editor.fields[0], path: "constructor.id" }] },
    { ...editor, save: { ...editor.save, input: { updates: {} } } },
    { ...editor, save: { ...editor.save, record_bindings: { profile_id: "id" } } },
    { ...editor, actions: [{ ...editor.actions[0], url: "/api/run" }] },
    { ...editor, fields: [{ ...editor.fields[0], path: "timing" }, { id: "at", label: "At", path: "timing.at", kind: "text" }] },
  ]) {
    assert.equal(parseCatalogView({ ...view, renderer: "record_editor", record_editor: unsafe }), null);
  }
  assert.equal(parseCatalogView({ ...view, record_editor: editor }), null);
});

test("unused turn context cannot remount conversation-only or profile-only editors", () => {
  const value = catalog();
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  assert.equal(viewContextKey(registered, { turn_id: "a" }), viewContextKey(registered, { turn_id: "b" }));
  registered.view.data_source = { ...operation, context_bindings: { conversation_id: "conversation_id" } };
  assert.equal(viewContextKey(registered, { conversation_id: "chat", turn_id: "a" }),
    viewContextKey(registered, { conversation_id: "chat", turn_id: "b" }));
  assert.notEqual(viewContextKey(registered, { conversation_id: "chat-a" }),
    viewContextKey(registered, { conversation_id: "chat-b" }));
});

const thread: ConversationThreadDefinition = {
  conversation_path: "thread.conversation", messages_path: "thread.messages",
  pending_turn_path: "thread.pending_turn", model_reference_path: "thread.context.model_reference",
  send: { operation: { ...operation, operation_id: "logic.send", contribution_id: "pack.logic.logic.send" },
    content_key: "content", turn_id_key: "turn_id",
    source_bindings: { conversation_id: "conversation_id", expected_child_revision: "revision" } },
  events: { operation, turn_id_key: "turn_id" },
};
test("thread grammar admits only canonical text/turn keys and exact declared requests", () => {
  const declared = { ...view, renderer: "conversation_thread", conversation_thread: thread };
  assert.ok(parseCatalogView(declared));
  for (const unsafe of [
    { ...thread, url: "/api/send" },
    { ...thread, messages_path: "constructor.messages" },
    { ...thread, send: { ...thread.send, content_key: "model_override" } },
    { ...thread, send: { ...thread.send, input: { turn_id: "forged" } } },
    { ...thread, send: { ...thread.send, context_bindings: { content: "conversation_id" } } },
    { ...thread, send: { ...thread.send, source_bindings: { profile_id: "thread.id" } } },
    { ...thread, events: { ...thread.events, content_key: "content" } },
  ]) assert.equal(parseCatalogView({ ...declared, conversation_thread: unsafe }), null);
  assert.equal(parseCatalogView({ ...declared, data_source: undefined }), null);
  assert.equal(parseCatalogView({ ...view, conversation_thread: thread }), null);
  const value = catalog();
  value.contributions[0].view = declared;
  value.contributions.push(contribution({ kind: "action", contribution_id: thread.send.operation.contribution_id,
    owner_pack_id: "logic", action_contract: operation.contract_id, operation_id: "logic.send", view: null }));
  const registered = viewsForSlot(value, "sidebar", "plan")[0];
  assert.ok(viewOperationRequest(value, registered, thread.send.operation, { content: "hello", turn_id: "ticket" }));
  assert.equal(viewReadRequest(value, registered, thread.send.operation, {}), null);
  assert.ok(viewReadRequest(value, registered, thread.events!.operation, { turn_id: "ticket" }));
});
