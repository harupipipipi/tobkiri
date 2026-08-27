import { normalizeLocale, t, type LocaleSetting } from "./i18n";

export type HistoryConversationType = "research" | "code" | "chat";
export type HistoryDateBucket = "today" | "recent" | "older";

export type HistoryMetadataRecord = {
  title?: string;
  date?: string;
  type?: string | null;
  conversationKind?: string | null;
  tags?: string[];
  workspaceId?: string | null;
  createdAt?: number | string | null;
  updatedAt?: number | string | null;
  metadata?: Record<string, unknown> | null;
};

export type HistoryClockOptions = {
  now?: number;
  timeZone?: string;
};

const DAY_MS = 86_400_000;

function cleanValue(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function explicitType(value: unknown): HistoryConversationType | null {
  const normalized = cleanValue(value).replace(/[\s-]+/g, "_");
  if (["code", "coding", "developer", "software_development"].includes(normalized)) return "code";
  if (["research", "analysis", "investigation"].includes(normalized)) return "research";
  if (["chat", "conversation", "general", "default"].includes(normalized)) return "chat";
  return null;
}

/** Resolve a history icon/category from explicit conversation and workspace metadata. */
export function historyConversationType(record: HistoryMetadataRecord): HistoryConversationType {
  const metadata = record.metadata ?? {};
  const declared = [
    record.conversationKind,
    metadata.conversation_kind,
    metadata.conversationKind,
    metadata.category,
    metadata.conversation_type,
    metadata.conversationType,
    metadata.mode,
  ];
  for (const candidate of declared) {
    const resolved = explicitType(candidate);
    if (resolved) return resolved;
  }

  const workspaceId = cleanValue(
    record.workspaceId ?? metadata.workspace_id ?? metadata.workspaceId,
  );
  const tags = (record.tags ?? [])
    .map((tag) => cleanValue(tag).replace(/[\s_]+/g, "-"))
    .filter(Boolean);
  if (workspaceId || tags.includes("coding") || tags.includes("code")) return "code";
  if (tags.includes("research") || tags.includes("analysis")) return "research";
  return explicitType(record.type) ?? "chat";
}

function parseTimestamp(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return value < 100_000_000_000 ? value * 1000 : value;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) {
    return numeric < 100_000_000_000 ? numeric * 1000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Return the canonical update/create timestamp without consulting display copy. */
export function historyTimestamp(record: HistoryMetadataRecord): number | null {
  const metadata = record.metadata ?? {};
  const candidates = [
    record.updatedAt,
    metadata.updated_at,
    metadata.updatedAt,
    record.createdAt,
    metadata.created_at,
    metadata.createdAt,
  ];
  for (const candidate of candidates) {
    const timestamp = parseTimestamp(candidate);
    if (timestamp !== null) return timestamp;
  }
  return null;
}

function dateOrdinal(timestamp: number, timeZone: string): number | null {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(timestamp));
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const year = Number(values.year);
    const month = Number(values.month);
    const day = Number(values.day);
    if (![year, month, day].every(Number.isFinite)) return null;
    return Math.floor(Date.UTC(year, month - 1, day) / DAY_MS);
  } catch {
    return null;
  }
}

function effectiveTimeZone(timeZone?: string): string {
  return timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

/** Group canonical timestamps by local calendar boundaries, including DST days. */
export function historyDateBucket(
  record: HistoryMetadataRecord,
  options: HistoryClockOptions = {},
): HistoryDateBucket {
  const timestamp = historyTimestamp(record);
  if (timestamp === null) return "older";
  const now = options.now ?? Date.now();
  const timeZone = effectiveTimeZone(options.timeZone);
  const today = dateOrdinal(now, timeZone);
  const updated = dateOrdinal(timestamp, timeZone);
  if (today === null || updated === null) return "older";
  const ageInCalendarDays = today - updated;
  if (ageInCalendarDays === 0) return "today";
  if (ageInCalendarDays > 0 && ageInCalendarDays <= 7) return "recent";
  return "older";
}

export function historyGroupTitle(locale: LocaleSetting, bucket: HistoryDateBucket): string {
  return t(locale, `history.group.${bucket}`);
}

/** Format timestamp display copy independently from date grouping decisions. */
export function formatHistoryTimestamp(
  record: HistoryMetadataRecord,
  locale: LocaleSetting,
  options: HistoryClockOptions = {},
): string {
  const timestamp = historyTimestamp(record);
  if (timestamp === null) return "";
  const bucket = historyDateBucket(record, options);
  if (bucket === "today") return t(locale, "history.group.today");

  const timeZone = effectiveTimeZone(options.timeZone);
  const today = dateOrdinal(options.now ?? Date.now(), timeZone);
  const updated = dateOrdinal(timestamp, timeZone);
  if (today !== null && updated !== null && today - updated === 1) {
    return t(locale, "history.date.yesterday");
  }

  return new Intl.DateTimeFormat(normalizeLocale(locale), {
    timeZone,
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(new Date(timestamp));
}
