import type { CompanyMessage } from "../lib/api";
import type { SubagentThread } from "./subagentTeamData";

export const SUBAGENT_TEAM_OUTBOX_STORAGE_PREFIX = "tobkiri.subagent-team.outbox.v1";

export type OutgoingMessageState =
  | "draft"
  | "queued"
  | "sending"
  | "committed"
  | "failed"
  | "unknown"
  | "cancelled";

export type SubagentTeamOutboxItem = {
  version: 1;
  clientMessageId: string;
  scopeKey: string;
  companyId: string | null;
  conversationId: string | null;
  thread: SubagentThread;
  channelId: string;
  content: string;
  mentions: string[];
  state: OutgoingMessageState;
  attempts: number;
  error?: string;
  createdAt: string;
  updatedAt: string;
};

export type OutboxStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export type LatestRequestGate = {
  begin: () => number;
  isCurrent: (requestId: number) => boolean;
};

type CreateOutboxItemInput = {
  clientMessageId: string;
  scopeKey: string;
  companyId: string | null;
  conversationId: string | null;
  thread: SubagentThread;
  channelId: string;
  content: string;
  mentions: string[];
  state: OutgoingMessageState;
  now?: string;
};

const OUTBOX_LIMIT = 100;
const CONTENT_LIMIT = 32_768;

export function createLatestRequestGate(): LatestRequestGate {
  let latestRequestId = 0;
  return {
    begin: () => {
      latestRequestId += 1;
      return latestRequestId;
    },
    isCurrent: (requestId) => requestId === latestRequestId,
  };
}

function safeString(value: unknown, limit = 256): string {
  return typeof value === "string" ? value.slice(0, limit) : "";
}

function storageKey(scopeKey: string): string {
  return `${SUBAGENT_TEAM_OUTBOX_STORAGE_PREFIX}:${encodeURIComponent(scopeKey)}`;
}

function isState(value: unknown): value is OutgoingMessageState {
  return ["draft", "queued", "sending", "committed", "failed", "unknown", "cancelled"].includes(String(value));
}

function normalizeItem(value: unknown, scopeKey: string): SubagentTeamOutboxItem | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const clientMessageId = safeString(record.clientMessageId, 160).trim();
  const content = safeString(record.content, CONTENT_LIMIT);
  const thread = record.thread && typeof record.thread === "object" && !Array.isArray(record.thread)
    ? record.thread as Record<string, unknown>
    : {};
  const threadType = thread.type === "dm" ? "dm" : thread.type === "channel" ? "channel" : null;
  const threadId = safeString(thread.id, 256).trim();
  const channelId = safeString(record.channelId, 256).trim();
  if (!clientMessageId || !content || !threadType || !threadId || !channelId || !isState(record.state)) return null;
  return {
    version: 1,
    clientMessageId,
    scopeKey,
    companyId: safeString(record.companyId, 256).trim() || null,
    conversationId: safeString(record.conversationId, 256).trim() || null,
    thread: { type: threadType, id: threadId },
    channelId,
    content,
    mentions: Array.isArray(record.mentions)
      ? record.mentions.map((item) => safeString(item, 256).trim()).filter(Boolean).slice(0, 100)
      : [],
    state: record.state,
    attempts: Math.max(0, Math.min(100, Number.isInteger(record.attempts) ? Number(record.attempts) : 0)),
    ...(safeString(record.error, 500).trim()
      ? { error: safeString(record.error, 500).trim() }
      : {}),
    createdAt: safeString(record.createdAt, 64) || new Date(0).toISOString(),
    updatedAt: safeString(record.updatedAt, 64) || new Date(0).toISOString(),
  };
}

export function outboxScopeKey(conversationId?: string | null, companyId?: string | null): string {
  const conversation = String(conversationId ?? "").trim();
  const company = String(companyId ?? "").trim();
  return `conversation:${conversation || "none"}|company:${company || "preview"}`;
}

export function createOutboxItem(input: CreateOutboxItemInput): SubagentTeamOutboxItem {
  const now = input.now ?? new Date().toISOString();
  return {
    version: 1,
    clientMessageId: input.clientMessageId,
    scopeKey: input.scopeKey,
    companyId: input.companyId,
    conversationId: input.conversationId,
    thread: input.thread,
    channelId: input.channelId,
    content: input.content,
    mentions: input.mentions,
    state: input.state,
    attempts: input.state === "queued" || input.state === "sending" ? 1 : 0,
    createdAt: now,
    updatedAt: now,
  };
}

export function transitionOutboxItem(
  item: SubagentTeamOutboxItem,
  state: OutgoingMessageState,
  options: { error?: string; now?: string } = {},
): SubagentTeamOutboxItem {
  const retrying = state === "queued" && ["failed", "unknown", "cancelled"].includes(item.state);
  const next: SubagentTeamOutboxItem = {
    ...item,
    state,
    attempts: item.attempts + (retrying ? 1 : 0),
    ...(options.error ? { error: safeString(options.error, 500).trim() } : {}),
    updatedAt: options.now ?? new Date().toISOString(),
  };
  if (!options.error) delete next.error;
  return next;
}

export function readOutbox(storage: OutboxStorage | null, scopeKey: string): SubagentTeamOutboxItem[] {
  if (!storage) return [];
  try {
    const raw = storage.getItem(storageKey(scopeKey));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(-OUTBOX_LIMIT).flatMap((item) => {
      const normalized = normalizeItem(item, scopeKey);
      return normalized ? [normalized] : [];
    });
  } catch {
    return [];
  }
}

export function writeOutbox(
  storage: OutboxStorage | null,
  scopeKey: string,
  items: SubagentTeamOutboxItem[],
): boolean {
  if (!storage) return false;
  try {
    const scopedItems = items
      .filter((item) => item.scopeKey === scopeKey && item.state !== "committed")
      .slice(-OUTBOX_LIMIT);
    if (scopedItems.length === 0) storage.removeItem(storageKey(scopeKey));
    else storage.setItem(storageKey(scopeKey), JSON.stringify(scopedItems));
    return true;
  } catch {
    return false;
  }
}

export function reconcileOutbox(
  items: SubagentTeamOutboxItem[],
  serverMessages: CompanyMessage[],
): SubagentTeamOutboxItem[] {
  const committedIds = new Set(serverMessages.flatMap((message) => {
    const value = message.metadata?.client_message_id;
    return typeof value === "string" && value.trim() ? [value.trim()] : [];
  }));
  return items.filter((item) => !committedIds.has(item.clientMessageId));
}

export function outboxItemAsMessage(item: SubagentTeamOutboxItem): CompanyMessage {
  return {
    id: `local-${item.clientMessageId}`,
    company_id: item.companyId ?? "preview-team",
    channel_id: item.thread.type === "dm" ? `dm-${item.thread.id}` : item.channelId,
    sender_id: "you",
    content: item.content,
    mentions: item.mentions,
    created_at: item.createdAt,
    metadata: {
      client_message_id: item.clientMessageId,
      outgoing_state: item.state,
      outgoing_attempts: item.attempts,
      outgoing_error: item.error,
      outgoing_thread_type: item.thread.type,
      outgoing_thread_id: item.thread.id,
      subagent_team: true,
      surface: "subagent_team_workspace",
    },
  };
}

export function deliveryFailureState(error: unknown): "failed" | "unknown" {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /abort|network|offline|timeout|timed out|failed to fetch|load failed/i.test(message)
    ? "unknown"
    : "failed";
}
