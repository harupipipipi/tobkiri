import { readFileSync } from "node:fs";
import { parseCatalogView } from "./catalogViewRegistry";
import { readConversationThread, type ThreadTicket } from "./conversationThreadState";
import type { ThreadProgressBinding, ThreadProgressPage } from "./threadProgressContract";

/** Test-only protected owner records; no production identity or execution proof. */
export const progressView = () => parseCatalogView(JSON.parse(readFileSync(new URL(
  "../../../../tobkiri_side_chat_pack/frontend/contributions/side-chat.json", import.meta.url), "utf8" )).view)!;
export const progressTicket = (): ThreadTicket => ({ turnId: "turn-1", conversationId: "child", draft: "  retained draft\n",
  source: { conversation_id: "child", revision: 3 }, turn: null });
export const protectedProgressSource = (turnId = "turn-1", draft = progressTicket().draft) => ({
  conversation_id: "child", revision: 4, parent_revision: 5,
  thread: { conversation: { id: "child", conversation_revision: 4, current_node_id: "user-1" },
    messages: [{ id: "user-1", role: "user", content: draft, metadata: { turn_id: turnId } }],
    pending_turn: { id: turnId, conversation_id: "child", revision: 2, status: "running",
      request_id: "saved-turn.durable", input_digest: `sha256:${"a".repeat(64)}` },
    context: { model_reference: "Inherited model" } },
});
export const progressThread = () => readConversationThread(protectedProgressSource(), progressView().conversation_thread!)!;
export const progressBinding = (): ThreadProgressBinding => ({
  turn_id: "turn-1", conversation_id: "child", conversation_revision: 4, parent_id: "user-1",
  request_id: "broker.saved.request", input_digest: `sha256:${"a".repeat(64)}`, ai_input_digest: `sha256:${"b".repeat(64)}`,
});
export const progressPage = (cursor = 1, delta = "Actual chunk"): ThreadProgressPage => ({
  version: "tobkiri.turn-progress.v1", provisional: true, binding: progressBinding(),
  events: [{ cursor, event: { type: "text_delta", delta } }], cursor, provider_complete: false,
  expires_at_ms: Date.now() + 60000, canonical_turn_status: "running",
});
