import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { ConversationThreadView } from "./ConversationThreadView";
import { controlPayload, parseCatalogView, viewOperationRequest, viewReference, type CatalogView, type RegisteredCatalogView } from "./catalogViewRegistry";
import type { FrontendCatalog, VerifiedFrontendContribution } from "./frontendContracts";

const item: VerifiedFrontendContribution = {
  contribution_id: "fixture.thread", kind: "view", mode: "declarative", label: "Thread", priority: 1,
  owner_pack_id: "fixture", owner_pack_hash: "sha256:fixture", build_identity: "test",
  resolved_profile_id: "profile", resolved_profile_revision: "r1", resolved_activation_id: "a1",
  resolved_plan_hash: "plan", descriptor_hash: "d1", localization: {},
  accessibility: { name: "Thread", keyboard: true },
};
const operation = { contribution_id: "fixture.send", contract_id: "fixture.turn.v1", operation_id: "fixture.send" };
const events = { contribution_id: "fixture.read", contract_id: "fixture.resource.v1", operation_id: "fixture.read" };
const catalog: FrontendCatalog = { version: "rumi.ui.contribution.v1", profile_id: "profile",
  profile_revision: "r1", activation_id: "a1", plan_hash: "plan", selected_entry_route: "/chat",
  contributions: [item,
    { ...item, contribution_id: operation.contribution_id, kind: "action", action_contract: operation.contract_id, operation_id: operation.operation_id },
    { ...item, contribution_id: events.contribution_id, kind: "action", action_contract: events.contract_id, operation_id: events.operation_id, read_only: true }],
  diagnostics: [], quarantined_pack_ids: [], catalog_hash: "catalog" };
const view: CatalogView = { version: "tobkiri.ui.view.v1", slot: "workspace_tab", renderer: "conversation_thread",
  data_source: events, conversation_thread: {
    conversation_path: "thread.conversation", messages_path: "thread.messages", pending_turn_path: "thread.pending_turn",
    model_reference_path: "thread.context.model_reference",
    send: { operation, input: { operation: "send" }, source_bindings: { conversation_id: "conversation_id", expected_child_revision: "revision" },
      turn_id_key: "turn_id", content_key: "content" },
    stop: { operation, input: { operation: "stop" }, source_bindings: { conversation_id: "conversation_id" }, turn_id_key: "turn_id" },
    events: { operation: events, input: { operation: "events" }, source_bindings: { conversation_id: "conversation_id" }, turn_id_key: "turn_id" },
  } };
const registered: RegisteredCatalogView = { item, view, reference: viewReference(catalog, item) };
const capabilities = { invokeAction: async () => { throw new Error("rendering must not execute"); },
  readDataSource: async () => { throw new Error("rendering must not poll"); } };
const snapshot = (status?: string) => ({ conversation_id: "child", revision: 3,
  thread: { conversation: { id: "child", conversation_revision: 3, title: "Thread", model: "parent-model" },
    messages: [{ id: "saved", role: "assistant", content: "Canonical child answer", created_at: 1000, finish_reason: "stop" }],
    pending_turn: status ? { id: "turn-1", conversation_id: "child", revision: 3, status } : null,
    context: { model_reference: { display_name: "<script>Inherited model</script>" } } } });
const render = (source: unknown, sourceReady = true, current = catalog) => renderToStaticMarkup(
  <ConversationThreadView registered={registered} catalog={current} capabilities={capabilities}
    snapshot={source} sourceReady={sourceReady} onRefresh={() => {}} />);

test("canonical child history and inherited model use shared escaped renderers without executing", () => {
  const html = render(snapshot());
  assert.match(html, /Canonical child answer/);
  assert.match(html, /&lt;script&gt;Inherited model&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /data-composer-widget="send"/);
  for (const widget of ["model-picker", "action-approval-control", "file-attach", "tool-selection-control"]) {
    assert.doesNotMatch(html, new RegExp('data-composer-widget="' + widget + '"'));
  }
});

test("authoritative pending state shows stop while historical completion does not create celebration", () => {
  const running = render(snapshot("running"));
  assert.match(running, /aria-label="生成を停止"/);
  assert.match(running, /readOnly=""/);
  const completed = render(snapshot("completed"));
  assert.doesNotMatch(completed, /aria-label="生成を停止"/);
  assert.doesNotMatch(completed, /保存完了|completed successfully|celebrat/i);
});

test("invalid projection is unavailable and removed operation cannot expose an active stop button", () => {
  assert.match(render(null), /まだ利用できません/);
  assert.match(render(snapshot("running"), true, { ...catalog, activation_id: "changed" }), /aria-label="生成を停止" disabled=""/);
  assert.match(render(snapshot(), false), /aria-label="メッセージを送信" disabled=""/);
});

test("shipped side-chat recovery binds only the authoritative child and pending turn", () => {
  const descriptor = JSON.parse(readFileSync(new URL(
    "../../../../tobkiri_side_chat_pack/frontend/contributions/side-chat.json", import.meta.url), "utf8")) as { view: unknown };
  const declared = parseCatalogView(descriptor.view)!;
  assert.ok(declared);
  const recovery = declared.controls!.find((control) => control.id === "reconcile")!;
  assert.deepEqual(recovery.input, { operation: "reconcile" });
  assert.deepEqual(recovery.input_bindings, { conversation_id: "conversation_id", turn_id: "thread.pending_turn.id" });
  const source = snapshot("running");
  const before = JSON.stringify(source);
  assert.deepEqual(controlPayload(recovery, source), {
    operation: "reconcile", conversation_id: "child", turn_id: "turn-1",
  });
  assert.equal(controlPayload(recovery, snapshot()), null);
  assert.equal(controlPayload(recovery, { ...source, conversation_id: undefined }), null);
  assert.equal(JSON.stringify(source), before);
  const target = recovery.operation;
  assert.deepEqual(target, {
    contribution_id: "pack.tobkiri_side_chat_pack.tobkiri_side_chat_pack.side-chat-turn",
    contract_id: "tobkiri.service.side-chat.turn.v1", operation_id: "tobkiri_side_chat_pack.side-chat-turn",
  });
  const current = { ...catalog, contributions: [item, { ...item, kind: "action" as const,
    contribution_id: target.contribution_id, owner_pack_id: "tobkiri_side_chat_pack",
    action_contract: target.contract_id, operation_id: target.operation_id }] };
  const recoveryView = { item, view: declared, reference: viewReference(current, item) };
  assert.equal(viewOperationRequest(current, recoveryView, target, controlPayload(recovery, source)!)?.ownerPackId,
    "tobkiri_side_chat_pack");
  assert.equal(viewOperationRequest({ ...current, contributions: [item] }, recoveryView, target, {}), null);
  assert.deepEqual(declared.conversation_thread!.reconcile, {
    operation: target, input: { operation: "reconcile" },
    source_bindings: { conversation_id: "conversation_id" }, turn_id_key: "turn_id",
  });
});
