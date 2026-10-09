import assert from "node:assert/strict";
import test from "node:test";
import { parseCatalogView, viewReadRequest, viewReference } from "./catalogViewRegistry";
import type { FrontendCatalog, VerifiedFrontendContribution } from "./frontendContracts";
import { progressView, progressTicket, progressThread, progressPage } from "./threadProgressFixture";
import { parseThreadProgressPage } from "./threadProgressContract";
import { mergeThreadProgress, narrowThreadProgressDeadline, threadProgressPayload, threadProgressReadCapture, threadProgressWitness } from "./threadProgressState";

const witness = () => threadProgressWitness(progressTicket(), progressThread())!;

test("only the protected current canonical user branch and exact Turn input establish a retained ticket", () => {
  const ticket = progressTicket(); const thread = progressThread();
  assert.deepEqual(threadProgressWitness(ticket, thread), { turnId: "turn-1", conversationId: "child",
    parentId: "user-1", conversationRevision: 4, inputDigest: `sha256:${"a".repeat(64)}` });
  for (const mutate of [
    (value: typeof thread) => { value.conversation.id = "other"; },
    (value: typeof thread) => { value.conversation.current_node_id = "other"; },
    (value: typeof thread) => { value.messages[0].content = "different input"; },
    (value: typeof thread) => { value.messages[0].metadata = { turn_id: "other" }; },
    (value: typeof thread) => { value.turn!.id = "other"; },
    (value: typeof thread) => { delete (value.turn as unknown as Record<string, unknown>).input_digest; },
  ]) { const invalid = progressThread(); mutate(invalid); assert.equal(threadProgressWitness(ticket, invalid), null); }
  assert.equal(threadProgressWitness({ ...ticket, contradictoryRevision: 2 }, thread), null);
});

test("cursor input uses exactly three finite fields and never substitutes durable request identity", () => {
  const request = progressView().conversation_thread!.progress!;
  assert.deepEqual(threadProgressPayload(request, progressTicket(), {}, 2), { conversation_id: "child", turn_id: "turn-1", cursor: 2 });
  for (const cursor of [-1, 4097, 1.1, Number.NaN]) assert.equal(threadProgressPayload(request, progressTicket(), {}, cursor), null);
  assert.equal(threadProgressPayload(request, { ...progressTicket(), conversationId: "other" }, {}, 0), null);
});

test("actual ordered pages append text, suppress reasoning and cannot settle a canonical saved receipt", () => {
  const page = progressPage(); const first = mergeThreadProgress(null, page, witness())!;
  assert.equal(first.text, "Actual chunk");
  assert.equal(first.binding.request_id, "broker.saved.request");
  const next = { ...page, events: [
    { cursor: 2, event: { type: "thinking_delta" as const, delta: "never expose raw reasoning" } },
    { cursor: 3, event: { type: "text_delta" as const, delta: " then next chunk" } },
    { cursor: 4, event: { type: "finish" as const, finish_reason: "stop" as const } },
  ], cursor: 4, provider_complete: true, canonical_turn_status: "completed" as const };
  const result = mergeThreadProgress(first, next, witness())!;
  assert.equal(result.text, "Actual chunk then next chunk");
  assert.equal(result.finishSeen, true);
  assert.equal((result as unknown as Record<string, unknown>).result_reference, undefined);
  assert.equal(progressTicket().draft, "  retained draft\n");
});

test("all seven first-page binding fields and the original expiry are immutable", () => {
  const firstPage = progressPage(); const first = mergeThreadProgress(null, firstPage, witness())!;
  for (const key of Object.keys(first.binding) as Array<keyof typeof first.binding>) {
    const binding = { ...first.binding, [key]: typeof first.binding[key] === "number" ? 5
      : key.endsWith("digest") ? `sha256:${"f".repeat(64)}` : "other" };
    assert.equal(mergeThreadProgress(first, { ...firstPage, binding, cursor: 1, events: [] }, witness()), null, key);
  }
  assert.equal(mergeThreadProgress(first, { ...firstPage, cursor: 1, events: [], expires_at_ms: first.expiresAtMs + 1 }, witness()), null);
  assert.equal(mergeThreadProgress(first, { ...firstPage, cursor: 1, events: [] }, witness(), first.expiresAtMs), null);
});

test("replay, skipped/out-of-order cursors, events after finish and oversized aggregate fail closed", () => {
  const page = progressPage(); const first = mergeThreadProgress(null, page, witness())!;
  assert.equal(mergeThreadProgress(first, page, witness()), null);
  assert.equal(mergeThreadProgress(null, { ...page, cursor: 2, events: [{ cursor: 2, event: page.events[0].event }] }, witness()), null);
  assert.equal(mergeThreadProgress(first, { ...page, cursor: 3, events: [{ cursor: 2, event: page.events[0].event }] }, witness()), null);
  assert.equal(mergeThreadProgress({ ...first, bytes: 4194304 }, { ...page, cursor: 2, events: [{ cursor: 2, event: page.events[0].event }] }, witness()), null);
  assert.equal(mergeThreadProgress({ ...first, finishSeen: true, providerComplete: true }, { ...page, cursor: 2, provider_complete: true,
    events: [{ cursor: 2, event: page.events[0].event }] }, witness()), null);
  assert.equal(mergeThreadProgress({ ...first, providerComplete: true }, { ...page, cursor: 1, events: [] }, witness()), null);
});

test("unversioned, expired, forged, excessive pages/deltas and private payload fields are rejected", () => {
  const page = progressPage();
  for (const change of [{ version: "tobkiri.turn-progress.v2" }, { provisional: false }, { approved: true }, { expires_at_ms: 1 },
    { expires_at_ms: Date.now() + 121000 }, { events: Array.from({ length: 129 }, () => page.events[0]) },
    { events: [{ cursor: 1, event: { type: "text_delta", delta: "界".repeat(5462) } }] },
    { binding: { ...page.binding, owner_principal: "forged" } }]) assert.equal(parseThreadProgressPage({ ...page, ...change }), null);
  assert.equal(parseThreadProgressPage({ ...page, events: Array.from({ length: 4 }, (_, index) => ({ cursor: index + 1,
    event: { type: "text_delta", delta: "x".repeat(16384) } })), cursor: 4 }), null);
});

test("progress grammar rejects extra/authority fields, wrong targets and claimed fixed cursor", () => {
  const view = progressView(); assert.ok(view.conversation_thread!.progress);
  for (const change of [{ content_key: "content" }, { cursor_key: "offset" }, { input: { approved: true } },
    { input: { cursor: 0 } }, { input: { operation: "start" } },
    { operation: { ...view.conversation_thread!.progress!.operation, contract_id: "other.v1" } }]) {
    assert.equal(parseCatalogView({ ...view, conversation_thread: { ...view.conversation_thread,
      progress: { ...view.conversation_thread!.progress, ...change } } }), null);
  }
});

test("selected progress resource must have Host read-only evidence and exact captured scope", () => {
  const view = progressView(); const operation = view.conversation_thread!.progress!.operation;
  const item: VerifiedFrontendContribution = { contribution_id: "fixture.thread", kind: "view", mode: "declarative", label: "Thread", priority: 0,
    owner_pack_id: "fixture.surface", owner_pack_hash: "sha256:fixture", build_identity: "test", descriptor_hash: "descriptor",
    resolved_profile_id: "profile", resolved_profile_revision: "revision", resolved_activation_id: "activation",
    resolved_plan_hash: "plan", localization: {}, accessibility: { name: "Thread", keyboard: true }, view };
  const action = { ...item, kind: "action" as const, contribution_id: operation.contribution_id, owner_pack_id: "rumi_turn_runtime_pack",
    action_contract: operation.contract_id, operation_id: operation.operation_id, read_only: true };
  const catalog: FrontendCatalog = { version: "rumi.ui.contribution.v1", profile_id: "profile", profile_revision: "revision", activation_id: "activation",
    plan_hash: "plan", catalog_hash: "catalog", selected_entry_route: "/chat", contributions: [item, action], diagnostics: [], quarantined_pack_ids: [] };
  const registered = { item, view, reference: viewReference(catalog, item) };
  const payload = threadProgressPayload(view.conversation_thread!.progress!, progressTicket(), {}, 0)!;
  assert.equal(viewReadRequest(catalog, registered, operation, payload)?.ownerPackId, "rumi_turn_runtime_pack");
  assert.equal(viewReadRequest({ ...catalog, activation_id: "new" }, registered, operation, payload), null);
  const deadline = Date.now() + 1000;
  action.resolved_expires_at_ms = deadline;
  assert.equal(threadProgressReadCapture(catalog, registered, payload)?.expiresAtMs, deadline);
  action.resolved_expires_at_ms = deadline + 60000;
  assert.equal(narrowThreadProgressDeadline(deadline, threadProgressReadCapture(catalog, registered, payload)!.expiresAtMs), deadline);
  assert.equal(narrowThreadProgressDeadline(deadline, null), deadline);
  action.resolved_expires_at_ms = Date.now() - 1;
  assert.equal(threadProgressReadCapture(catalog, registered, payload), null);
  delete action.resolved_expires_at_ms;
  action.read_only = false; assert.equal(viewReadRequest(catalog, registered, operation, payload), null);
  catalog.contributions = [item]; assert.equal(viewReadRequest(catalog, registered, operation, payload), null);
});
