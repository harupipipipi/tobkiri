import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { useScopedPendingChat } from "../src/lib/scopedPendingChat";
import type { PendingChatRequest } from "../src/lib/pendingChat";

/** Exercises storage transitions and failures without a runtime or model call. */
export async function mountScopedPendingFixture() {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const a = `sha256:${"a".repeat(64)}`;
  const b = `sha256:${"b".repeat(64)}`;
  const request: PendingChatRequest = { conversationId: "chat", operationId: "turn-a", savedTurn: true,
    ownerTurnObserved: true, startedAt: 1, status: "pending", toolNames: [] };
  localStorage.setItem("rumi-pending-chat-v2:fixture", JSON.stringify({ scopes: { [a]: { chat: request } }, archive: [] }));
  const container = document.createElement("div"); document.body.replaceChildren(container);
  let switchStore!: (store: string) => void;
  let update!: ReturnType<typeof useScopedPendingChat>["setPending"];
  function Fixture() {
    const [store, setStore] = useState(a);
    switchStore = setStore;
    const state = useScopedPendingChat("fixture", store);
    update = state.setPending;
    return <><p data-testid="pending">{Object.keys(state.pending).join(",")}</p>
      <p data-testid="owner">{String(state.pending.chat?.ownerTurnObserved ?? false)}</p>
      <p data-testid="persistence">{String(state.persistenceError)}</p></>;
  }
  await act(async () => { createRoot(container).render(<Fixture />); });
  const oldAUpdate = update;
  return {
    switchStore: async (store: "a" | "b") => { await act(async () => { switchStore(store === "a" ? a : b); }); },
    observe: async () => { await act(async () => { update((current) => ({ ...current, chat: { ...request, ownerTurnObserved: true } })); }); },
    lateAUpdate: async () => { await act(async () => { oldAUpdate({ late: { ...request, conversationId: "late" } }); }); },
    externalWriteThenForget: async () => {
      const document = JSON.parse(localStorage.getItem("rumi-pending-chat-v2:fixture")!);
      document.scopes[a].other = { ...request, conversationId: "other", operationId: "turn-other" };
      localStorage.setItem("rumi-pending-chat-v2:fixture", JSON.stringify(document));
      await act(async () => { update((current) => { const next = { ...current }; delete next.chat; return next; }); });
    },
    quotaFailure: async () => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = () => { throw new DOMException("Quota exceeded", "QuotaExceededError"); };
      try { await act(async () => { update({ chat: { ...request, ownerTurnObserved: false } }); }); }
      finally { Storage.prototype.setItem = original; }
    },
  };
}
