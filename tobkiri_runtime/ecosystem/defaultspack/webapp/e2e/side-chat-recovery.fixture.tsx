import { act } from "react";
import { createRoot } from "react-dom/client";
import descriptor from "../../../tobkiri_side_chat_pack/frontend/contributions/side-chat.json" with { type: "json" };
import { FrontendViewSlot } from "../src/host/FrontendViewSlot";
import type { FrontendCatalog, CapturedCapabilityInvocation, VerifiedFrontendContribution } from "../src/host/frontendContracts";

// Mount the shipped renderer with captured public operations and inert fake owners.
// This adds no runtime route and never connects a model or a production Host.
export async function mountRecoveryFixture() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const item: VerifiedFrontendContribution = {
    contribution_id: "pack.tobkiri_side_chat_pack.tobkiri.side-chat", kind: "view",
    mode: "declarative", label: descriptor.label, priority: 0,
    owner_pack_id: "tobkiri_side_chat_pack", owner_pack_hash: "sha256:fixture",
    build_identity: "test", resolved_profile_id: "profile", resolved_profile_revision: "r1",
    resolved_activation_id: "a1", resolved_plan_hash: "plan", descriptor_hash: "d1",
    localization: {}, accessibility: descriptor.accessibility, view: descriptor.view,
  };
  const actions = [
    ["side-chat-resource", "tobkiri.resource.side-chat.v1", true],
    ["side-chat-manage", "tobkiri.action.side-chat.v1", false],
    ["side-chat-turn", "tobkiri.service.side-chat.turn.v1", false],
  ].map(([id, contract, readOnly]) => ({ ...item, kind: "action" as const,
    contribution_id: `pack.tobkiri_side_chat_pack.tobkiri_side_chat_pack.${id}`,
    operation_id: `tobkiri_side_chat_pack.${id}`, action_contract: String(contract),
    read_only: Boolean(readOnly), view: undefined,
  }));
  let catalog: FrontendCatalog = {
    version: "rumi.ui.contribution.v1", profile_id: "profile", profile_revision: "r1",
    activation_id: "a1", plan_hash: "plan", catalog_hash: "catalog",
    selected_entry_route: "/chat", contributions: [item, ...actions],
    diagnostics: [], quarantined_pack_ids: [],
  };
  const base = {
    conversation_id: "child", revision: 3, parent_revision: 5,
    thread: {
      conversation: { id: "child", conversation_revision: 3, title: "Child" },
      messages: [] as unknown[], pending_turn: null as unknown,
      context: { model_reference: "Inherited model" },
    },
  };
  let source: unknown = structuredClone(base);
  const requests: CapturedCapabilityInvocation[] = [];
  const reads: CapturedCapabilityInvocation[] = [];
  const guards = new Map<string, () => boolean>();
  let mode: "pending" | "failed" | "returned_failure" | "approval" | "completed" = "failed";
  let settle: ((value: unknown) => void) | null = null;
  let reject: ((reason: Error) => void) | null = null;
  const capturedTurn = () => String(requests.find((request) => request.payload.operation === "send")?.payload.turn_id);
  const completed = () => ({
    id: capturedTurn(), conversation_id: "child", revision: 3, status: "completed",
    result_reference: {
      conversation_id: "child", conversation_revision: 4,
      user_message_id: "user-1", assistant_message_id: "assistant-1",
      outcome_digest: `sha256:${"a".repeat(64)}`,
    },
  });
  const capabilities = {
    invokeAction: async (request: CapturedCapabilityInvocation) => {
      requests.push(structuredClone(request));
      if (request.payload.operation === "send") throw new Error("lost_send_reply");
      if (mode === "pending") return new Promise<unknown>((resolve, fail) => { settle = resolve; reject = fail; });
      if (mode === "failed") throw new Error("owner_unavailable");
      if (mode === "returned_failure") return { status: "failed" };
      if (mode === "approval") return { status: "approval_required" };
      return { status: "completed", turn: completed() };
    },
    readDataSource: async (request: CapturedCapabilityInvocation) => {
      reads.push(structuredClone(request));
      return request.payload.operation === "events"
        ? { turn: { id: capturedTurn(), conversation_id: "child", revision: 1, status: "running" } }
        : structuredClone(source);
    },
  };
  const render = () => root.render(<FrontendViewSlot catalog={catalog} slot="workspace_tab"
    activePlanHash="plan" capabilities={capabilities} context={{ conversation_id: "parent" }}
    onNavigationGuardChange={(id, guard) => { if (guard) guards.set(id, guard); else guards.delete(id); }} />);
  await act(async () => { render(); });
  return {
    requests: () => structuredClone(requests), reads: () => structuredClone(reads),
    canLeave: () => [...guards.values()].every((guard) => guard()),
    setMode: (value: typeof mode) => { mode = value; },
    replaceSource: (value: unknown) => { source = structuredClone(value); },
    withLatestTurn: () => ({ ...structuredClone(base), thread: { ...structuredClone(base.thread),
      pending_turn: { id: "newer-turn", conversation_id: "child", revision: 1, status: "running" } } }),
    withSavedReceipt: () => ({ conversation_id: "child", revision: 4, parent_revision: 5,
      thread: { ...structuredClone(base.thread), conversation: { ...base.thread.conversation, conversation_revision: 4 },
        pending_turn: completed(), messages: [
          { id: "user-1", role: "user", content: "  retained draft\n", metadata: { turn_id: capturedTurn() } },
          { id: "assistant-1", role: "assistant", content: "Saved answer", finish_reason: "stop", metadata: { turn_id: capturedTurn() } },
        ] } }),
    setAvailable: async (available: boolean) => {
      catalog = { ...catalog, contributions: [item, ...actions.filter((action) =>
        available || action.operation_id !== "tobkiri_side_chat_pack.side-chat-turn")] };
      await act(async () => { render(); });
    },
    failPending: () => { reject?.(new Error("lost_recovery_reply")); },
    resolvePending: (value: unknown) => { settle?.(value); },
    destroy: async () => { await act(async () => { root.unmount(); }); container.remove(); },
  };
}
