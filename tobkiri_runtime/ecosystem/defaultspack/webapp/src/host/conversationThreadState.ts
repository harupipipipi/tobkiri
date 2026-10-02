import type { ChatMessage, Conversation, SavedTurnResult } from "../lib/api";
import { savedTurnSnapshotState } from "../lib/pendingChat";
import {
  readViewPath, requestContextInput, validConversationThreadInput,
  type ConversationThreadDefinition, type ConversationThreadRequest, type ViewInputContext,
} from "./catalogViewRegistry";

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const identity = (value: unknown): value is string => typeof value === "string"
  && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
const revision = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;
const states = new Set(["accepted", "queued", "running", "waiting", "completed", "failed", "cancelled", "interrupted", "superseded"]);

export type ThreadTurn = SavedTurnResult["turn"] & { started_at_ms?: number };
export type ThreadTicket = {
  turnId: string; conversationId: string; draft: string;
  source: unknown; turn: ThreadTurn | null; contradictoryRevision?: number;
};
export type ThreadSnapshot = {
  conversation: Conversation;
  messages: ChatMessage[];
  turn: ThreadTurn | null;
  modelLabel: string | null;
};

/** Read a canonical owner turn only when its conversation and optional ticket match. */
export function readThreadTurn(value: unknown, conversationId: string, turnId?: string): ThreadTurn | null {
  if (!record(value) || !identity(readViewPath(value, "id"))
    || readViewPath(value, "conversation_id") !== conversationId
    || (readViewPath(value, "turn_id") !== undefined && readViewPath(value, "turn_id") !== readViewPath(value, "id"))
    || (turnId && readViewPath(value, "id") !== turnId)
    || !revision(readViewPath(value, "revision"))
    || !states.has(String(readViewPath(value, "status")))) return null;
  return value as ThreadTurn;
}

/** A read response cannot replace a different ticket or conversation. */
export function readThreadEventTurn(value: unknown, conversationId: string, turnId: string): ThreadTurn | null {
  const direct = readThreadTurn(value, conversationId, turnId);
  if (direct) return direct;
  if (!record(value)) return null;
  if ((readViewPath(value, "conversation_id") !== undefined && readViewPath(value, "conversation_id") !== conversationId)
    || (readViewPath(value, "turn_id") !== undefined && readViewPath(value, "turn_id") !== turnId)) return null;
  return readThreadTurn(readViewPath(value, "turn"), conversationId, turnId);
}

/** Parse bounded child history; labels and message blocks stay data for shared renderers. */
export function readConversationThread(snapshot: unknown, descriptor: ConversationThreadDefinition): ThreadSnapshot | null {
  const conversation = readViewPath(snapshot, descriptor.conversation_path);
  const messages = readViewPath(snapshot, descriptor.messages_path);
  if (!record(conversation) || !identity(readViewPath(conversation, "id"))
    || !revision(readViewPath(conversation, "conversation_revision"))
    || !Array.isArray(messages) || messages.length > 4000) return null;
  const conversationId = conversation.id as string;
  const seen = new Set<string>();
  const normalized: ChatMessage[] = [];
  for (const message of messages) {
    if (!record(message) || !identity(readViewPath(message, "id")) || seen.has(message.id as string)
      || !["user", "assistant", "tool", "system"].includes(String(readViewPath(message, "role")))
      || (readViewPath(message, "conversation_id") !== undefined && readViewPath(message, "conversation_id") !== conversationId)
      || !(typeof readViewPath(message, "content") === "string" || Array.isArray(readViewPath(message, "content")))) return null;
    seen.add(message.id as string);
    normalized.push({ ...message, conversation_id: conversationId } as ChatMessage);
  }
  const rawTurn = readViewPath(snapshot, descriptor.pending_turn_path);
  const turn = rawTurn === undefined || rawTurn === null ? null : readThreadTurn(rawTurn, conversationId);
  if (rawTurn !== undefined && rawTurn !== null && !turn) return null;
  const model = readViewPath(snapshot, descriptor.model_reference_path);
  const label = typeof model === "string" ? model : record(model)
    ? ["display_name", "qualified_model_id", "model_id", "profile_id"].map((path) => readViewPath(model, path))
      .find((value): value is string => typeof value === "string") : null;
  return {
    conversation: { ...conversation, messages: normalized } as Conversation,
    messages: normalized, turn,
    modelLabel: typeof label === "string" && label.length <= 256 ? label : null,
  };
}

/** Bind only declared data and fixed text/ticket fields, never model or grant overrides. */
export function threadRequestPayload(
  request: ConversationThreadRequest, snapshot: unknown, context: ViewInputContext,
  turnId: string, content?: string,
): Record<string, unknown> | null {
  if (!identity(turnId)) return null;
  const payload = requestContextInput(request, context);
  if (!payload) return null;
  for (const [key, path] of Object.entries(request.source_bindings ?? {})) {
    if (Object.prototype.hasOwnProperty.call(payload, key)) return null;
    const value = readViewPath(snapshot, path);
    if (value === undefined) return null;
    payload[key] = value;
  }
  if (request.turn_id_key !== "turn_id" || Object.prototype.hasOwnProperty.call(payload, "turn_id")) return null;
  payload.turn_id = turnId;
  if (request.content_key) {
    if (request.content_key !== "content" || Object.prototype.hasOwnProperty.call(payload, "content")
      || typeof content !== "string" || !content.trim() || new TextEncoder().encode(content).length > 16384) return null;
    payload.content = [{ type: "text", text: content }];
  } else if (content !== undefined) return null;
  return validConversationThreadInput(payload) ? payload : null;
}

export function threadTurnIsActive(turn: ThreadTurn | null): boolean {
  return Boolean(turn && ["accepted", "queued", "running", "waiting"].includes(turn.status));
}

/** Late reads cannot roll a ticket back or settle contradictory owner revisions. */
export function newestThreadTurn(left: ThreadTurn | null, right: ThreadTurn | null): ThreadTurn | null {
  if (!left) return right;
  if (!right) return left;
  if (left.id !== right.id || left.conversation_id !== right.conversation_id) return null;
  if (left.revision === right.revision && left.status !== right.status) return null;
  return left.revision >= right.revision ? left : right;
}

/** Conflicting equal revisions require a newer owner record before settlement. */
export function mergeThreadTicketTurn(ticket: ThreadTicket, incoming: ThreadTurn): ThreadTicket {
  if (ticket.turnId !== incoming.id || ticket.conversationId !== incoming.conversation_id) return ticket;
  const newest = newestThreadTurn(ticket.turn, incoming);
  if (!newest) return { ...ticket, contradictoryRevision: incoming.revision };
  const contradiction = ticket.contradictoryRevision;
  return { ...ticket, turn: newest,
    contradictoryRevision: contradiction !== undefined && newest.revision <= contradiction ? contradiction : undefined };
}

/** Clear a sent draft only after a saved receipt and the fresh child projection agree. */
export function threadTurnIsSaved(turn: ThreadTurn, thread: ThreadSnapshot, turnId: string): boolean {
  const state = savedTurnSnapshotState(turn, thread.conversation, thread.conversation.id, turnId);
  return state === "current" || state === "changed";
}

export function threadTurnFailureNotice(turn: ThreadTurn): string | null {
  if (turn.status === "cancelled") return "実行の停止を確認しました。入力内容は保持しました。";
  if (turn.status === "interrupted") return "実行が中断されました。入力内容は保持しました。";
  if (turn.status === "failed" || turn.status === "superseded") return "実行を完了できませんでした。入力内容は保持しました。";
  return null;
}
