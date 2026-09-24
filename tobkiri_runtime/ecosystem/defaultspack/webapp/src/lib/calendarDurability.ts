export const CALENDAR_DOCUMENT_KEY = "defaultspack.calendar.document.v2";
export const CALENDAR_LEGACY_KEY = "defaultspack.calendar.items.v1";
export const CALENDAR_JOURNAL_KEY = "defaultspack.calendar.mutation.v2";
export const CALENDAR_RECOVERY_KEY = "defaultspack.calendar.recovery.v2";
export const CALENDAR_DOCUMENT_SCHEMA = "tobkiri.calendar.document.v2";

export type DurableCalendarItem = {
  id: string;
  date: string;
  endDate?: string;
  agentPrompt?: string;
  kind: "task" | "event" | "reminder";
  lastRunStatus?: string;
  scheduleId?: string;
  scheduleRevision?: number;
  scheduleStatus?: string;
  calendarRevision?: number;
  syncState?: "settled" | "missing_schedule" | "conflict" | "pending";
  syncMessage?: string;
  title: string;
  time?: string;
};

export type CalendarDocument = {
  schema: typeof CALENDAR_DOCUMENT_SCHEMA;
  revision: number;
  writerId: string;
  updatedAt: string;
  lastMutationId?: string;
  items: DurableCalendarItem[];
};

export type CalendarMutation = {
  schema: "tobkiri.calendar.mutation.v1";
  mutationId: string;
  operation: "upsert" | "delete" | "repair";
  baseRevision: number;
  writerId: string;
  targetItemId?: string;
  createdAt: string;
  proposedItems: DurableCalendarItem[];
};

export type CalendarLoadResult = {
  document: CalendarDocument;
  pendingMutation: CalendarMutation | null;
  recoveryRaw: string | null;
  status: "ready" | "migrated" | "corrupt" | "unavailable";
};

export type CalendarReconciliationIssue = {
  itemId: string;
  kind: "remote_orphan" | "missing_schedule" | "conflict";
  message: string;
  scheduleId?: string;
};

export type CalendarRemoteReplay = {
  upsertSchedule: (
    existing: DurableCalendarItem | null,
    proposed: DurableCalendarItem,
    mutationId: string,
  ) => Promise<Pick<DurableCalendarItem, "scheduleId" | "scheduleRevision" | "scheduleStatus">>;
  deleteSchedule: (existing: DurableCalendarItem, mutationId: string) => Promise<void>;
};

export class CalendarPersistenceError extends Error {
  constructor(
    message: string,
    readonly code: "CORRUPT" | "CONFLICT" | "STORAGE_UNAVAILABLE" | "VERIFY_FAILED",
  ) {
    super(message);
    this.name = "CalendarPersistenceError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function safeItems(value: unknown): DurableCalendarItem[] | null {
  if (!Array.isArray(value)) return null;
  const items: DurableCalendarItem[] = [];
  const itemIds = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)) return null;
    const id = typeof candidate.id === "string" ? candidate.id.trim() : "";
    const date = typeof candidate.date === "string" ? candidate.date.trim() : "";
    const title = typeof candidate.title === "string" ? candidate.title.trim() : "";
    const kind = candidate.kind;
    if (
      !id
      || itemIds.has(id)
      || !date
      || !title
      || !["task", "event", "reminder"].includes(String(kind))
    ) return null;
    itemIds.add(id);
    items.push(candidate as DurableCalendarItem);
  }
  return items;
}

function emptyDocument(writerId: string): CalendarDocument {
  return {
    schema: CALENDAR_DOCUMENT_SCHEMA,
    revision: 0,
    writerId,
    updatedAt: new Date(0).toISOString(),
    items: [],
  };
}

function parseDocument(raw: string, writerId: string): CalendarDocument | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || value.schema !== CALENDAR_DOCUMENT_SCHEMA) return null;
    const items = safeItems(value.items);
    const revision = Number(value.revision);
    if (!items || !Number.isSafeInteger(revision) || revision < 0) return null;
    return {
      schema: CALENDAR_DOCUMENT_SCHEMA,
      revision,
      writerId,
      updatedAt: typeof value.updatedAt === "string" ? value.updatedAt : new Date(0).toISOString(),
      lastMutationId: typeof value.lastMutationId === "string" ? value.lastMutationId : undefined,
      items,
    };
  } catch {
    return null;
  }
}

function parseMutation(raw: string | null): CalendarMutation | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || value.schema !== "tobkiri.calendar.mutation.v1") return null;
    const proposedItems = safeItems(value.proposedItems);
    const baseRevision = Number(value.baseRevision);
    if (
      !proposedItems
      || typeof value.mutationId !== "string"
      || !value.mutationId.trim()
      || typeof value.writerId !== "string"
      || !value.writerId.trim()
      || !["upsert", "delete", "repair"].includes(String(value.operation))
      || !Number.isSafeInteger(baseRevision)
      || baseRevision < 0
    ) return null;
    return {
      ...value,
      proposedItems,
      baseRevision,
      targetItemId: typeof value.targetItemId === "string" ? value.targetItemId : undefined,
    } as CalendarMutation;
  } catch {
    return null;
  }
}

function preserveRecovery(storage: Storage, sourceKey: string, raw: string): boolean {
  try {
    const entry = {
      schema: "tobkiri.calendar.recovery.v1",
      sourceKey,
      capturedAt: new Date().toISOString(),
      raw,
    };
    const previousRaw = storage.getItem(CALENDAR_RECOVERY_KEY);
    let entries: unknown[] = [];
    if (previousRaw) {
      try {
        const previous: unknown = JSON.parse(previousRaw);
        if (isRecord(previous) && Array.isArray(previous.entries)) {
          entries = previous.entries;
        } else {
          entries = [previous];
        }
      } catch {
        entries = [{ schema: "tobkiri.calendar.recovery.v1", sourceKey: CALENDAR_RECOVERY_KEY, raw: previousRaw }];
      }
    }
    storage.setItem(CALENDAR_RECOVERY_KEY, JSON.stringify({
      schema: "tobkiri.calendar.recovery.archive.v1",
      entries: [...entries, entry].slice(-20),
    }));
    return true;
  } catch {
    // The original record remains untouched when even recovery storage is unavailable.
    return false;
  }
}

export function loadCalendarDocument(storage: Storage, writerId: string): CalendarLoadResult {
  let currentRaw: string | null;
  let legacyRaw: string | null;
  let journalRaw: string | null;
  let recoveryRaw: string | null;
  try {
    currentRaw = storage.getItem(CALENDAR_DOCUMENT_KEY);
    legacyRaw = storage.getItem(CALENDAR_LEGACY_KEY);
    journalRaw = storage.getItem(CALENDAR_JOURNAL_KEY);
    recoveryRaw = storage.getItem(CALENDAR_RECOVERY_KEY);
  } catch {
    return {
      document: emptyDocument(writerId),
      pendingMutation: null,
      recoveryRaw: null,
      status: "unavailable",
    };
  }
  const pendingMutation = parseMutation(journalRaw);
  if (journalRaw && !pendingMutation) {
    preserveRecovery(storage, CALENDAR_JOURNAL_KEY, journalRaw);
    return {
      document: currentRaw ? parseDocument(currentRaw, writerId) ?? emptyDocument(writerId) : emptyDocument(writerId),
      pendingMutation: null,
      recoveryRaw: journalRaw,
      status: "corrupt",
    };
  }
  if (currentRaw) {
    const document = parseDocument(currentRaw, writerId);
    if (!document) {
      preserveRecovery(storage, CALENDAR_DOCUMENT_KEY, currentRaw);
      return {
        document: emptyDocument(writerId),
        pendingMutation,
        recoveryRaw: currentRaw,
        status: "corrupt",
      };
    }
    return {
      document,
      pendingMutation,
      recoveryRaw,
      status: "ready",
    };
  }

  if (!legacyRaw) {
    return {
      document: emptyDocument(writerId),
      pendingMutation,
      recoveryRaw,
      status: "ready",
    };
  }
  let legacy: unknown;
  try {
    legacy = JSON.parse(legacyRaw);
  } catch {
    legacy = null;
  }
  const items = safeItems(legacy);
  if (!items) {
    preserveRecovery(storage, CALENDAR_LEGACY_KEY, legacyRaw);
    return {
      document: emptyDocument(writerId),
      pendingMutation,
      recoveryRaw: legacyRaw,
      status: "corrupt",
    };
  }
  return {
    document: { ...emptyDocument(writerId), items },
    pendingMutation,
    recoveryRaw,
    status: "migrated",
  };
}

export function beginCalendarMutation(
  storage: Storage,
  document: CalendarDocument,
  mutation: Omit<CalendarMutation, "schema" | "baseRevision" | "writerId" | "createdAt">,
): CalendarMutation {
  const live = loadCalendarDocument(storage, document.writerId);
  if (live.status === "corrupt") {
    throw new CalendarPersistenceError("Calendar storage is corrupt; export or repair it before saving.", "CORRUPT");
  }
  if (live.status === "unavailable") {
    throw new CalendarPersistenceError("Calendar storage is unavailable; the draft was not sent.", "STORAGE_UNAVAILABLE");
  }
  if (live.pendingMutation) {
    throw new CalendarPersistenceError("Another Calendar change is still pending. Retry, cancel, or repair it first.", "CONFLICT");
  }
  if (live.document.revision !== document.revision) {
    throw new CalendarPersistenceError("Calendar changed in another tab. Reload and review the newer version.", "CONFLICT");
  }
  const journal: CalendarMutation = {
    schema: "tobkiri.calendar.mutation.v1",
    mutationId: mutation.mutationId,
    operation: mutation.operation,
    baseRevision: document.revision,
    writerId: document.writerId,
    targetItemId: mutation.targetItemId,
    createdAt: new Date().toISOString(),
    proposedItems: mutation.proposedItems,
  };
  try {
    storage.setItem(CALENDAR_JOURNAL_KEY, JSON.stringify(journal));
    const verified = parseMutation(storage.getItem(CALENDAR_JOURNAL_KEY));
    if (
      !verified
      || verified.mutationId !== journal.mutationId
      || verified.writerId !== journal.writerId
    ) {
      throw new CalendarPersistenceError("Calendar mutation journal could not be verified.", "VERIFY_FAILED");
    }
  } catch (error) {
    if (error instanceof CalendarPersistenceError) throw error;
    throw new CalendarPersistenceError("Calendar mutation journal could not be saved and verified.", "STORAGE_UNAVAILABLE");
  }
  return journal;
}

export function settleCalendarMutation(
  storage: Storage,
  writerId: string,
  mutation: CalendarMutation,
): CalendarDocument {
  const live = loadCalendarDocument(storage, writerId);
  if (live.status === "corrupt") {
    throw new CalendarPersistenceError("Calendar storage became unreadable during the mutation.", "CORRUPT");
  }
  if (live.status === "unavailable") {
    throw new CalendarPersistenceError("Calendar storage became unavailable during the mutation.", "STORAGE_UNAVAILABLE");
  }
  if (
    live.document.revision === mutation.baseRevision + 1
    && live.document.lastMutationId === mutation.mutationId
  ) {
    if (live.pendingMutation && live.pendingMutation.mutationId !== mutation.mutationId) {
      throw new CalendarPersistenceError("A newer Calendar mutation is pending.", "CONFLICT");
    }
    try {
      storage.removeItem(CALENDAR_JOURNAL_KEY);
      if (storage.getItem(CALENDAR_JOURNAL_KEY) !== null) {
        throw new CalendarPersistenceError("Committed Calendar journal could not be cleared.", "VERIFY_FAILED");
      }
      return live.document;
    } catch (error) {
      if (error instanceof CalendarPersistenceError) throw error;
      throw new CalendarPersistenceError("Committed Calendar journal could not be cleared.", "STORAGE_UNAVAILABLE");
    }
  }
  if (live.document.revision !== mutation.baseRevision) {
    throw new CalendarPersistenceError("Calendar changed before this mutation settled.", "CONFLICT");
  }
  if (
    !live.pendingMutation
    || live.pendingMutation.mutationId !== mutation.mutationId
    || live.pendingMutation.writerId !== mutation.writerId
  ) {
    throw new CalendarPersistenceError("Calendar mutation ownership changed before settlement.", "CONFLICT");
  }
  const document: CalendarDocument = {
    schema: CALENDAR_DOCUMENT_SCHEMA,
    revision: mutation.baseRevision + 1,
    writerId,
    updatedAt: new Date().toISOString(),
    lastMutationId: mutation.mutationId,
    items: mutation.proposedItems.map((item) => ({ ...item, syncState: item.syncState ?? "settled" })),
  };
  try {
    storage.setItem(CALENDAR_DOCUMENT_KEY, JSON.stringify(document));
    const verified = parseDocument(storage.getItem(CALENDAR_DOCUMENT_KEY) ?? "", writerId);
    if (
      !verified
      || verified.revision !== document.revision
      || verified.lastMutationId !== mutation.mutationId
    ) {
      throw new CalendarPersistenceError("Calendar write acknowledgement could not be verified.", "VERIFY_FAILED");
    }
    storage.removeItem(CALENDAR_JOURNAL_KEY);
    if (storage.getItem(CALENDAR_JOURNAL_KEY) !== null) {
      throw new CalendarPersistenceError("Calendar mutation journal could not be cleared.", "VERIFY_FAILED");
    }
    return verified;
  } catch (error) {
    if (error instanceof CalendarPersistenceError) throw error;
    throw new CalendarPersistenceError("Calendar document could not be saved.", "STORAGE_UNAVAILABLE");
  }
}

export function repairCalendarDocument(
  storage: Storage,
  writerId: string,
  items: DurableCalendarItem[],
  expectedRevision?: number,
): CalendarDocument {
  let liveRaw: string | null;
  let journalRaw: string | null;
  try {
    liveRaw = storage.getItem(CALENDAR_DOCUMENT_KEY);
    journalRaw = storage.getItem(CALENDAR_JOURNAL_KEY);
  } catch {
    throw new CalendarPersistenceError("Calendar storage is unavailable; repair was not started.", "STORAGE_UNAVAILABLE");
  }
  const liveDocument = liveRaw ? parseDocument(liveRaw, writerId) : null;
  if (
    liveDocument
    && expectedRevision !== undefined
    && liveDocument.revision !== expectedRevision
  ) {
    throw new CalendarPersistenceError("Calendar changed in another tab before repair.", "CONFLICT");
  }
  if (liveRaw && !parseDocument(liveRaw, writerId)) {
    if (!preserveRecovery(storage, CALENDAR_DOCUMENT_KEY, liveRaw)) {
      throw new CalendarPersistenceError("Corrupt Calendar data could not be preserved for repair.", "STORAGE_UNAVAILABLE");
    }
  }
  if (journalRaw && !parseMutation(journalRaw)) {
    if (!preserveRecovery(storage, CALENDAR_JOURNAL_KEY, journalRaw)) {
      throw new CalendarPersistenceError("Corrupt Calendar journal could not be preserved for repair.", "STORAGE_UNAVAILABLE");
    }
  }
  const document: CalendarDocument = {
    schema: CALENDAR_DOCUMENT_SCHEMA,
    revision: (liveDocument?.revision ?? 0) + 1,
    writerId,
    updatedAt: new Date().toISOString(),
    items: items.map((item) => ({ ...item, syncState: item.syncState === "pending" ? "settled" : item.syncState })),
  };
  try {
    storage.setItem(CALENDAR_DOCUMENT_KEY, JSON.stringify(document));
    const verified = parseDocument(storage.getItem(CALENDAR_DOCUMENT_KEY) ?? "", writerId);
    if (!verified || verified.revision !== document.revision) {
      throw new CalendarPersistenceError("Calendar repair could not be verified.", "VERIFY_FAILED");
    }
    storage.removeItem(CALENDAR_JOURNAL_KEY);
    if (storage.getItem(CALENDAR_JOURNAL_KEY) !== null) {
      throw new CalendarPersistenceError("Calendar repair journal could not be cleared.", "VERIFY_FAILED");
    }
    return verified;
  } catch (error) {
    if (error instanceof CalendarPersistenceError) throw error;
    throw new CalendarPersistenceError("Calendar repair could not be saved.", "STORAGE_UNAVAILABLE");
  }
}

export function cancelCalendarMutation(storage: Storage, writerId: string): CalendarLoadResult {
  let pendingRaw: string | null;
  try {
    pendingRaw = storage.getItem(CALENDAR_JOURNAL_KEY);
  } catch {
    throw new CalendarPersistenceError("Pending Calendar mutation could not be read.", "STORAGE_UNAVAILABLE");
  }
  if (pendingRaw && !preserveRecovery(storage, CALENDAR_JOURNAL_KEY, pendingRaw)) {
    throw new CalendarPersistenceError("Pending Calendar mutation could not be preserved.", "STORAGE_UNAVAILABLE");
  }
  try {
    storage.removeItem(CALENDAR_JOURNAL_KEY);
    if (storage.getItem(CALENDAR_JOURNAL_KEY) !== null) {
      throw new CalendarPersistenceError("Pending Calendar mutation could not be cancelled.", "VERIFY_FAILED");
    }
  } catch {
    throw new CalendarPersistenceError("Pending Calendar mutation could not be cancelled.", "STORAGE_UNAVAILABLE");
  }
  return loadCalendarDocument(storage, writerId);
}

function mutationTargetItemId(
  currentItems: DurableCalendarItem[],
  mutation: CalendarMutation,
): string | undefined {
  return mutation.targetItemId ?? mutation.proposedItems.find((proposed) => {
    const existing = currentItems.find((item) => item.id === proposed.id);
    return JSON.stringify(existing) !== JSON.stringify(proposed);
  })?.id ?? currentItems.find(
    (item) => !mutation.proposedItems.some((proposed) => proposed.id === item.id),
  )?.id;
}

export async function replayCalendarRemoteMutation(
  currentItems: DurableCalendarItem[],
  mutation: CalendarMutation,
  remote: CalendarRemoteReplay,
): Promise<CalendarMutation> {
  const targetItemId = mutationTargetItemId(currentItems, mutation);
  const existing = targetItemId
    ? currentItems.find((item) => item.id === targetItemId) ?? null
    : null;
  const proposed = targetItemId
    ? mutation.proposedItems.find((item) => item.id === targetItemId) ?? null
    : null;
  if (mutation.operation === "delete") {
    if (existing?.scheduleId) await remote.deleteSchedule(existing, mutation.mutationId);
    return mutation;
  }
  if (proposed?.kind === "task" && proposed.agentPrompt) {
    const schedule = await remote.upsertSchedule(existing, proposed, mutation.mutationId);
    if (!schedule.scheduleId) {
      throw new CalendarPersistenceError("Backend did not acknowledge the Calendar schedule ID.", "VERIFY_FAILED");
    }
    const settledItem: DurableCalendarItem = {
      ...proposed,
      ...schedule,
      syncState: "settled",
    };
    return {
      ...mutation,
      proposedItems: mutation.proposedItems.map(
        (item) => item.id === settledItem.id ? settledItem : item,
      ),
    };
  }
  if (existing?.scheduleId) await remote.deleteSchedule(existing, mutation.mutationId);
  return mutation;
}

function scheduleRecord(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function calendarItemFromSchedule(schedule: Record<string, unknown>): DurableCalendarItem | null {
  const task = scheduleRecord(schedule.task);
  const metadata = scheduleRecord(task?.metadata);
  if (metadata?.source !== "calendar") return null;
  const snapshot = scheduleRecord(metadata.calendar_item_snapshot);
  const itemId = String(metadata.calendar_item_id ?? snapshot?.id ?? "").trim();
  const date = String(metadata.calendar_start_date ?? snapshot?.date ?? "").trim();
  if (!itemId || !date) return null;
  const title = String(snapshot?.title ?? schedule.name ?? "Calendar Agent task").replace(/^Calendar:\s*/, "");
  const scheduleRevision = Number(schedule.revision ?? 0) || 0;
  return {
    id: itemId,
    date,
    endDate: String(metadata.calendar_end_date ?? snapshot?.endDate ?? date) || undefined,
    kind: "task",
    title,
    time: String(metadata.calendar_time ?? snapshot?.time ?? "09:00"),
    agentPrompt: String(snapshot?.agentPrompt ?? task?.message ?? title),
    scheduleId: String(schedule.id ?? "") || undefined,
    scheduleRevision,
    scheduleStatus: String(schedule.status ?? "active"),
    calendarRevision: Number(metadata.calendar_revision ?? snapshot?.calendarRevision ?? 0) || 0,
    syncState: "settled",
  };
}

function sameCalendarAutomationContent(left: DurableCalendarItem, right: DurableCalendarItem): boolean {
  return (
    left.kind === right.kind
    && left.date === right.date
    && (left.endDate ?? left.date) === (right.endDate ?? right.date)
    && left.title === right.title
    && (left.time ?? "") === (right.time ?? "")
    && (left.agentPrompt ?? left.title) === (right.agentPrompt ?? right.title)
  );
}

export function reconcileCalendarSchedules(
  localItems: DurableCalendarItem[],
  schedules: unknown[],
): { items: DurableCalendarItem[]; issues: CalendarReconciliationIssue[] } {
  const remoteItems = schedules
    .map((value) => scheduleRecord(value))
    .filter((value): value is Record<string, unknown> => value !== null)
    .map((schedule) => calendarItemFromSchedule(schedule))
    .filter((item): item is DurableCalendarItem => item !== null);
  remoteItems.sort((left, right) => (
    (right.calendarRevision ?? 0) - (left.calendarRevision ?? 0)
    || (right.scheduleRevision ?? 0) - (left.scheduleRevision ?? 0)
    || String(left.scheduleId ?? "").localeCompare(String(right.scheduleId ?? ""))
  ));
  const remoteByItem = new Map<string, DurableCalendarItem>();
  const remoteScheduleIds = new Set(remoteItems.map((item) => item.scheduleId).filter(Boolean));
  const issues: CalendarReconciliationIssue[] = [];
  for (const remote of remoteItems) {
    const selected = remoteByItem.get(remote.id);
    if (!selected) {
      remoteByItem.set(remote.id, remote);
      continue;
    }
    issues.push({
      itemId: remote.id,
      kind: "conflict",
      scheduleId: remote.scheduleId,
      message: `Duplicate backend Calendar schedule detected; ${selected.scheduleId ?? "the newest record"} was selected deterministically.`,
    });
  }
  const items = localItems.map((local) => {
    const remote = remoteByItem.get(local.id);
    if (remote) {
      remoteByItem.delete(local.id);
      const localRevision = local.calendarRevision ?? 0;
      const remoteRevision = remote.calendarRevision ?? 0;
      if (remoteRevision > localRevision) return { ...local, ...remote };
      if (remoteRevision === localRevision && !local.scheduleId && remote.scheduleId) {
        return { ...local, ...remote };
      }
      if (remoteRevision === localRevision && local.scheduleId === remote.scheduleId) {
        if (!sameCalendarAutomationContent(local, remote)) {
          issues.push({ itemId: local.id, kind: "conflict", scheduleId: remote.scheduleId, message: "Calendar content differs at the same revision." });
          return { ...local, syncState: "conflict" as const, syncMessage: "Backend schedule content differs at the same revision; repair is required." };
        }
        return { ...local, scheduleRevision: remote.scheduleRevision, scheduleStatus: remote.scheduleStatus, syncState: "settled" as const, syncMessage: undefined };
      }
      issues.push({ itemId: local.id, kind: "conflict", scheduleId: remote.scheduleId, message: "Local and backend Calendar revisions disagree." });
      return { ...local, syncState: "conflict" as const, syncMessage: "Backend schedule differs; repair is required." };
    }
    if (local.scheduleId && !remoteScheduleIds.has(local.scheduleId)) {
      issues.push({ itemId: local.id, kind: "missing_schedule", scheduleId: local.scheduleId, message: "The linked backend schedule no longer exists." });
      return { ...local, syncState: "missing_schedule" as const, syncMessage: "Backend schedule is missing; edit to recreate or detach it." };
    }
    if (local.kind === "task" && local.agentPrompt && local.syncState === "pending") {
      issues.push({ itemId: local.id, kind: "missing_schedule", message: "The pending Agent task has no backend schedule acknowledgement." });
      return { ...local, syncState: "missing_schedule" as const, syncMessage: "Backend schedule acknowledgement is missing; retry or repair it." };
    }
    return local;
  });
  for (const remote of remoteByItem.values()) {
    issues.push({ itemId: remote.id, kind: "remote_orphan", scheduleId: remote.scheduleId, message: "A backend Calendar schedule was missing from this browser and was restored." });
    items.push(remote);
  }
  return { items, issues };
}

export function exportCalendarRecovery(load: CalendarLoadResult): string {
  return JSON.stringify({
    schema: "tobkiri.calendar.export.v1",
    exportedAt: new Date().toISOString(),
    status: load.status,
    document: load.document,
    pendingMutation: load.pendingMutation,
    recoveryRaw: load.recoveryRaw,
  }, null, 2);
}
