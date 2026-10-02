import { act } from "react";
import { createRoot } from "react-dom/client";
import descriptor from "../../../tobkiri_side_chat_pack/frontend/contributions/side-chat.json";
import { FrontendViewSlot } from "../src/host/FrontendViewSlot";
import type { CapturedCapabilityInvocation, FrontendCatalog, VerifiedFrontendContribution } from "../src/host/frontendContracts";
import type { ThreadProgressEvent } from "../src/host/threadProgressContract";

/** Local mocked protected resource fixture; no model, Host execution or external traffic. */
export async function mountProgressFixture() {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const item: VerifiedFrontendContribution = { contribution_id: "fixture.thread", kind: "view", mode: "declarative",
    label: descriptor.label, priority: 0, owner_pack_id: "tobkiri_side_chat_pack", owner_pack_hash: "sha256:fixture",
    build_identity: "test", resolved_profile_id: "profile", resolved_profile_revision: "r1", resolved_activation_id: "a1",
    resolved_plan_hash: "plan", descriptor_hash: "d1", localization: {}, accessibility: descriptor.accessibility, view: descriptor.view };
  const actions = [
    ["tobkiri_side_chat_pack", "side-chat-resource", "tobkiri.resource.side-chat.v1", true],
    ["tobkiri_side_chat_pack", "side-chat-manage", "tobkiri.action.side-chat.v1", false],
    ["tobkiri_side_chat_pack", "side-chat-turn", "tobkiri.service.side-chat.turn.v1", false],
    ["rumi_turn_runtime_pack", "turn-progress-resource", "tobkiri.resource.turn.progress.v1", true],
  ].map(([pack, id, contract, readOnly]) => ({ ...item, kind: "action" as const,
    contribution_id: `pack.${pack}.${pack}.${id}`, owner_pack_id: String(pack), operation_id: `${pack}.${id}`,
    action_contract: String(contract), read_only: Boolean(readOnly), view: undefined }));
  let catalog: FrontendCatalog = { version: "rumi.ui.contribution.v1", profile_id: "profile", profile_revision: "r1",
    activation_id: "a1", plan_hash: "plan", catalog_hash: "catalog", selected_entry_route: "/chat",
    contributions: [item, ...actions], diagnostics: [], quarantined_pack_ids: [] };
  const requests: CapturedCapabilityInvocation[] = []; const reads: CapturedCapabilityInvocation[] = [];
  const guards = new Map<string, () => boolean>();
  let source: unknown = { conversation_id: "child", revision: 3, parent_revision: 5,
    thread: { conversation: { id: "child", conversation_revision: 3, current_node_id: "old-head" }, messages: [], pending_turn: null,
      context: { model_reference: "Inherited model" } } };
  let turnId = ""; let draft = ""; let expires = 0;
  let events: Array<{ cursor: number; event: ThreadProgressEvent }> = [];
  let complete = false;
  let tamper: Record<string, unknown> = {};
  let rejectSend: ((reason: Error) => void) | null = null;
  let releaseRead: (() => void) | null = null;
  let holdRead = false;
  const turn = (status = "running") => ({ id: turnId, conversation_id: "child", revision: 2, status,
    request_id: "saved-turn.durable", input_digest: `sha256:${"a".repeat(64)}` });
  const user = () => ({ id: "user-1", role: "user", content: draft, metadata: { turn_id: turnId } });
  const capabilities = {
    invokeAction: async (request: CapturedCapabilityInvocation) => {
      requests.push(structuredClone(request));
      if (request.payload.operation !== "send") return { turn: turn() };
      turnId = String(request.payload.turn_id);
      draft = (request.payload.content as Array<{ text: string }>)[0].text;
      expires = Date.now() + 60000;
      source = { conversation_id: "child", revision: 4, parent_revision: 5,
        thread: { conversation: { id: "child", conversation_revision: 4, current_node_id: "user-1" },
          messages: [user()], pending_turn: turn(), context: { model_reference: "Inherited model" } } };
      return new Promise<unknown>((_resolve, reject) => { rejectSend = reject; });
    },
    readDataSource: async (request: CapturedCapabilityInvocation) => {
      reads.push(structuredClone(request));
      if (request.contractId !== "tobkiri.resource.turn.progress.v1") return request.payload.operation === "events"
        ? { turn: turn() } : structuredClone(source);
      const cursor = Number(request.payload.cursor);
      const visible = events.filter((event) => event.cursor > cursor).slice(0, 128);
      const page = { version: "tobkiri.turn-progress.v1", provisional: true,
        binding: { turn_id: turnId, conversation_id: "child", parent_id: "user-1", conversation_revision: 4,
          request_id: "broker.saved.request", input_digest: `sha256:${"a".repeat(64)}`, ai_input_digest: `sha256:${"b".repeat(64)}` },
        events: structuredClone(visible), cursor: visible.at(-1)?.cursor ?? cursor,
        provider_complete: complete, expires_at_ms: expires, canonical_turn_status: "running", ...structuredClone(tamper) };
      if (holdRead) await new Promise<void>((resolve) => { releaseRead = resolve; });
      return page;
    },
  };
  const render = () => root.render(<FrontendViewSlot catalog={catalog} slot="workspace_tab" activePlanHash="plan"
    capabilities={capabilities} context={{ conversation_id: "parent" }}
    onNavigationGuardChange={(id, guard) => { if (guard) guards.set(id, guard); else guards.delete(id); }} />);
  await act(async () => { render(); });
  return {
    requests: () => structuredClone(requests), reads: () => structuredClone(reads),
    hasHeldRead: () => releaseRead !== null,
    canLeave: () => [...guards.values()].every((guard) => guard()),
    append: (event: ThreadProgressEvent) => { events.push({ cursor: events.length + 1, event }); if (event.type === "finish") complete = true; },
    pulse: () => { document.dispatchEvent(new Event("visibilitychange")); },
    tamper: (value: Record<string, unknown>) => { tamper = structuredClone(value); },
    hold: () => { holdRead = true; },
    release: () => { holdRead = false; releaseRead?.(); },
    failSend: () => { rejectSend?.(new Error("lost_send_reply")); },
    changeCapture: async () => { catalog = { ...catalog, activation_id: "different" }; await act(async () => { render(); }); },
    captureExpiry: async (duration: number) => {
      catalog = { ...catalog, contributions: catalog.contributions.map((entry) => entry.owner_pack_id === "rumi_turn_runtime_pack"
        ? { ...entry, resolved_expires_at_ms: Date.now() + duration } : entry) };
      await act(async () => { render(); });
    },
    disableProgress: async () => { catalog = { ...catalog, contributions: [item, ...actions.filter((action) => action.owner_pack_id !== "rumi_turn_runtime_pack")] };
      await act(async () => { render(); }); },
    savedHistory: () => {
      const receipt = { conversation_id: "child", conversation_revision: 5, user_message_id: "user-1", assistant_message_id: "assistant-1",
        outcome_digest: `sha256:${"f".repeat(64)}` };
      source = { conversation_id: "child", revision: 5, parent_revision: 5,
        thread: { conversation: { id: "child", conversation_revision: 5, current_node_id: "assistant-1" },
          messages: [user(), { id: "assistant-1", role: "assistant", content: "Canonical saved answer", finish_reason: "stop", metadata: { turn_id: turnId } }],
          pending_turn: { ...turn("completed"), revision: 3, result_reference: receipt }, context: { model_reference: "Inherited model" } } };
    },
    destroy: async () => { rejectSend?.(new Error("fixture_closed")); await act(async () => { root.unmount(); }); container.remove(); },
  };
}
