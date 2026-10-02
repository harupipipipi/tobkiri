import { readViewPath, viewReadRequest, type RegisteredCatalogView, type ConversationThreadProgressRequest, type ViewInputContext } from "./catalogViewRegistry";
import type { FrontendCatalog } from "./frontendContracts";
import { threadRequestPayload, type ThreadSnapshot, type ThreadTicket } from "./conversationThreadState";
import { parseThreadProgressPage, progressBytes, progressDigest, progressId, THREAD_PROGRESS_LIMITS,
  type ThreadProgressBinding } from "./threadProgressContract";

export type ThreadProgressWitness = {
  turnId: string; conversationId: string; parentId: string; conversationRevision: number; inputDigest: string;
};
export type ThreadProgressState = {
  binding: ThreadProgressBinding; expiresAtMs: number; cursor: number; text: string;
  bytes: number; eventCount: number; providerComplete: boolean; finishSeen: boolean;
};

/** The protected canonical user branch and owner Turn must establish the ticket. */
export function threadProgressWitness(ticket: ThreadTicket, thread: ThreadSnapshot): ThreadProgressWitness | null {
  const conversation = thread.conversation;
  const parentId = readViewPath(conversation, "current_node_id");
  const revision = readViewPath(conversation, "conversation_revision");
  const turn = thread.turn;
  const inputDigest = readViewPath(turn, "input_digest");
  if (ticket.contradictoryRevision !== undefined || conversation.id !== ticket.conversationId || !turn || turn.id !== ticket.turnId
    || turn.conversation_id !== ticket.conversationId || !["queued", "running", "waiting"].includes(turn.status)
    || !progressId(parentId) || !Number.isSafeInteger(revision) || Number(revision) < 1 || !progressDigest(inputDigest)) return null;
  const user = thread.messages.find((message) => message.id === parentId);
  const content = readViewPath(user, "content");
  const text = typeof content === "string" ? content : Array.isArray(content) && content.length === 1
    && readViewPath(content[0], "type") === "text" ? readViewPath(content[0], "text") : undefined;
  if (!user || user.role !== "user" || readViewPath(user, "metadata.turn_id") !== ticket.turnId || text !== ticket.draft) return null;
  return { turnId: ticket.turnId, conversationId: ticket.conversationId, parentId, conversationRevision: Number(revision), inputDigest };
}

/** Read only this retained ticket and contiguous cursor; all identity is data. */
export function threadProgressPayload(
  request: ConversationThreadProgressRequest, ticket: ThreadTicket, context: ViewInputContext, cursor: number,
): Record<string, unknown> | null {
  if (request.cursor_key !== "cursor" || !Number.isSafeInteger(cursor) || cursor < 0 || cursor > 4096) return null;
  const payload = threadRequestPayload(request, ticket.source, context, ticket.turnId);
  if (!payload || payload.conversation_id !== ticket.conversationId || Object.keys(payload).sort().join(",") !== "conversation_id,turn_id") return null;
  return { ...payload, cursor };
}

const equalBinding = (left: ThreadProgressBinding, right: ThreadProgressBinding) =>
  (Object.keys(left) as Array<keyof ThreadProgressBinding>).every((key) => left[key] === right[key]);

/** Append actual ordered text only; finish/status can never settle a saved turn. */
export function mergeThreadProgress(
  previous: ThreadProgressState | null, value: unknown, witness: ThreadProgressWitness, now = Date.now(),
): ThreadProgressState | null {
  const page = parseThreadProgressPage(value, now);
  if (!page || page.binding.turn_id !== witness.turnId || page.binding.conversation_id !== witness.conversationId
    || page.binding.parent_id !== witness.parentId || page.binding.conversation_revision !== witness.conversationRevision
    || page.binding.input_digest !== witness.inputDigest || (previous && (previous.expiresAtMs <= now
      || previous.expiresAtMs !== page.expires_at_ms || !equalBinding(previous.binding, page.binding)
      || previous.providerComplete && !page.provider_complete))) return null;
  let cursor = previous?.cursor ?? 0;
  let bytes = previous?.bytes ?? 0;
  let text = previous?.text ?? "";
  let finishSeen = previous?.finishSeen ?? false;
  for (const item of page.events) {
    if (finishSeen || item.cursor !== cursor + 1) return null;
    cursor = item.cursor; bytes += progressBytes(item.event);
    if (bytes > THREAD_PROGRESS_LIMITS.bytes) return null;
    if (item.event.type === "text_delta") text += item.event.delta;
    // No approved safe-summary provenance exists in this version: suppress all thinking text.
    else if (item.event.type === "finish") finishSeen = true;
  }
  const eventCount = (previous?.eventCount ?? 0) + page.events.length;
  if (page.cursor !== cursor || eventCount > THREAD_PROGRESS_LIMITS.events || finishSeen && !page.provider_complete) return null;
  return { binding: { ...page.binding }, expiresAtMs: page.expires_at_ms, cursor, text, bytes,
    eventCount, finishSeen, providerComplete: page.provider_complete };
}

/** Host capture deadlines can shrink on refresh, but cannot renew a retained read. */
export function threadProgressReadCapture(
  catalog: FrontendCatalog, registered: RegisteredCatalogView, payload: Record<string, unknown>,
): { request: NonNullable<ReturnType<typeof viewReadRequest>>; expiresAtMs: number | null } | null {
  const progress = registered.view.conversation_thread?.progress;
  if (!progress) return null;
  const request = viewReadRequest(catalog, registered, progress.operation, payload);
  if (!request) return null;
  const source = registered.view.data_source;
  const items = [registered.item, ...catalog.contributions.filter((item) =>
    item.contribution_id === progress.operation.contribution_id || item.contribution_id === source?.contribution_id)];
  const expiries = items.filter((item) => item.resolved_expires_at_ms !== undefined).map((item) => item.resolved_expires_at_ms!);
  if (expiries.some((value) => !Number.isSafeInteger(value) || value <= Date.now())) return null;
  return { request, expiresAtMs: expiries.length ? Math.min(...expiries) : null };
}

/** Preserve an original deadline even if a refreshed catalog omits/extends it. */
export function narrowThreadProgressDeadline(original: number | null | undefined, current: number | null): number | null {
  if (original === undefined || original === null) return current;
  return current === null ? original : Math.min(original, current);
}
