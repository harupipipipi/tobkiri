import assert from "node:assert/strict";
import test from "node:test";

import {
  beginCalendarMutation,
  cancelCalendarMutation,
  CALENDAR_DOCUMENT_KEY,
  CALENDAR_JOURNAL_KEY,
  CALENDAR_LEGACY_KEY,
  CALENDAR_RECOVERY_KEY,
  CalendarPersistenceError,
  loadCalendarDocument,
  reconcileCalendarSchedules,
  replayCalendarRemoteMutation,
  repairCalendarDocument,
  settleCalendarMutation,
  type DurableCalendarItem,
} from "./calendarDurability";

class MemoryStorage implements Storage {
  values = new Map<string, string>();
  failReads = false;
  failWrites = false;
  failRemoves = false;
  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) {
    if (this.failReads) throw new DOMException("denied", "SecurityError");
    return this.values.get(key) ?? null;
  }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) {
    if (this.failRemoves) throw new DOMException("denied", "SecurityError");
    this.values.delete(key);
  }
  setItem(key: string, value: string) {
    if (this.failWrites) throw new DOMException("quota", "QuotaExceededError");
    this.values.set(key, value);
  }
}

const item: DurableCalendarItem = {
  id: "calendar-1",
  date: "2026-07-20",
  kind: "task",
  title: "Daily review",
  time: "09:00",
};

test("legacy items migrate without deleting the original record", () => {
  const storage = new MemoryStorage();
  storage.setItem(CALENDAR_LEGACY_KEY, JSON.stringify([item]));
  const loaded = loadCalendarDocument(storage, "tab-a");
  assert.equal(loaded.status, "migrated");
  assert.deepEqual(loaded.document.items, [item]);
  assert.ok(storage.getItem(CALENDAR_LEGACY_KEY));
  assert.equal(storage.getItem(CALENDAR_DOCUMENT_KEY), null);
});

test("corrupt data is preserved and cannot be silently overwritten", () => {
  const storage = new MemoryStorage();
  storage.setItem(CALENDAR_LEGACY_KEY, "{broken calendar");
  const loaded = loadCalendarDocument(storage, "tab-a");
  assert.equal(loaded.status, "corrupt");
  assert.equal(loaded.recoveryRaw, "{broken calendar");
  assert.match(storage.getItem(CALENDAR_RECOVERY_KEY) ?? "", /broken calendar/);
  assert.throws(
    () => beginCalendarMutation(storage, loaded.document, { mutationId: "m1", operation: "upsert", proposedItems: [item] }),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "CORRUPT",
  );
  assert.equal(storage.getItem(CALENDAR_LEGACY_KEY), "{broken calendar");
});

test("storage denial blocks remote mutation before a draft can be lost", () => {
  const storage = new MemoryStorage();
  storage.failReads = true;
  const loaded = loadCalendarDocument(storage, "tab-a");
  assert.equal(loaded.status, "unavailable");
  assert.throws(
    () => beginCalendarMutation(storage, loaded.document, { mutationId: "m1", operation: "upsert", proposedItems: [item] }),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "STORAGE_UNAVAILABLE",
  );
});

test("journal precedes the acknowledged document and survives quota failure", () => {
  const storage = new MemoryStorage();
  const loaded = loadCalendarDocument(storage, "tab-a");
  const mutation = beginCalendarMutation(storage, loaded.document, {
    mutationId: "m1",
    operation: "upsert",
    proposedItems: [item],
  });
  assert.ok(storage.getItem(CALENDAR_JOURNAL_KEY));
  storage.failWrites = true;
  assert.throws(
    () => settleCalendarMutation(storage, "tab-a", mutation),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "STORAGE_UNAVAILABLE",
  );
  assert.ok(storage.getItem(CALENDAR_JOURNAL_KEY));
  storage.failWrites = false;
  const settled = settleCalendarMutation(storage, "tab-a", mutation);
  assert.equal(settled.revision, 1);
  assert.equal(settled.items[0]?.id, item.id);
  assert.equal(storage.getItem(CALENDAR_JOURNAL_KEY), null);
});

test("cancel preserves the pending draft as recovery data", () => {
  const storage = new MemoryStorage();
  const loaded = loadCalendarDocument(storage, "tab-a");
  beginCalendarMutation(storage, loaded.document, { mutationId: "m1", operation: "upsert", proposedItems: [item] });
  const cancelled = cancelCalendarMutation(storage, "tab-a");
  assert.equal(cancelled.pendingMutation, null);
  assert.equal(storage.getItem(CALENDAR_JOURNAL_KEY), null);
  assert.match(storage.getItem(CALENDAR_RECOVERY_KEY) ?? "", /calendar-1/);
});

test("cancel keeps the journal when recovery storage cannot be acknowledged", () => {
  const storage = new MemoryStorage();
  const loaded = loadCalendarDocument(storage, "tab-a");
  beginCalendarMutation(storage, loaded.document, {
    mutationId: "m1",
    operation: "upsert",
    proposedItems: [item],
  });
  storage.failWrites = true;
  assert.throws(
    () => cancelCalendarMutation(storage, "tab-a"),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "STORAGE_UNAVAILABLE",
  );
  storage.failWrites = false;
  assert.equal(loadCalendarDocument(storage, "tab-a").pendingMutation?.mutationId, "m1");
});

test("another tab revision prevents silent last-write-wins", () => {
  const storage = new MemoryStorage();
  const first = loadCalendarDocument(storage, "tab-a");
  const mutation = beginCalendarMutation(storage, first.document, {
    mutationId: "m1",
    operation: "upsert",
    proposedItems: [item],
  });
  settleCalendarMutation(storage, "tab-a", mutation);
  assert.throws(
    () => beginCalendarMutation(storage, first.document, { mutationId: "stale", operation: "delete", proposedItems: [] }),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "CONFLICT",
  );
});

test("a pending journal cannot be overwritten by another tab", () => {
  const storage = new MemoryStorage();
  const first = loadCalendarDocument(storage, "tab-a");
  beginCalendarMutation(storage, first.document, {
    mutationId: "m1",
    operation: "upsert",
    proposedItems: [item],
  });
  const second = loadCalendarDocument(storage, "tab-b");
  assert.throws(
    () => beginCalendarMutation(storage, second.document, {
      mutationId: "m2",
      operation: "delete",
      proposedItems: [],
    }),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "CONFLICT",
  );
  assert.equal(loadCalendarDocument(storage, "tab-a").pendingMutation?.mutationId, "m1");
});

test("malformed journals are preserved and block new remote work", () => {
  const storage = new MemoryStorage();
  storage.setItem(CALENDAR_JOURNAL_KEY, "{broken journal");
  const loaded = loadCalendarDocument(storage, "tab-a");
  assert.equal(loaded.status, "corrupt");
  assert.equal(loaded.recoveryRaw, "{broken journal");
  assert.match(storage.getItem(CALENDAR_RECOVERY_KEY) ?? "", /broken journal/);
  assert.throws(
    () => beginCalendarMutation(storage, loaded.document, {
      mutationId: "m2",
      operation: "upsert",
      proposedItems: [item],
    }),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "CORRUPT",
  );
});

test("settlement is idempotent when the document commits but journal cleanup fails", () => {
  const storage = new MemoryStorage();
  const loaded = loadCalendarDocument(storage, "tab-a");
  const mutation = beginCalendarMutation(storage, loaded.document, {
    mutationId: "m1",
    operation: "upsert",
    proposedItems: [item],
  });
  storage.failRemoves = true;
  assert.throws(
    () => settleCalendarMutation(storage, "tab-a", mutation),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "STORAGE_UNAVAILABLE",
  );
  assert.equal(loadCalendarDocument(storage, "tab-a").document.lastMutationId, "m1");
  storage.failRemoves = false;
  const retried = settleCalendarMutation(storage, "tab-a", mutation);
  assert.equal(retried.revision, 1);
  assert.equal(storage.getItem(CALENDAR_JOURNAL_KEY), null);
});

test("schedule commit plus local write failure retries one mutation without duplicating the schedule", async () => {
  const storage = new MemoryStorage();
  const loaded = loadCalendarDocument(storage, "tab-a");
  const pendingItem = { ...item, agentPrompt: "Run daily review", calendarRevision: 1, syncState: "pending" as const };
  const mutation = beginCalendarMutation(storage, loaded.document, {
    mutationId: "m-create",
    operation: "upsert",
    proposedItems: [pendingItem],
    targetItemId: item.id,
  });
  const schedules = new Map<string, { scheduleId: string; scheduleRevision: number; scheduleStatus: string }>();
  let remoteAttempts = 0;
  const remote = {
    upsertSchedule: async (_existing: DurableCalendarItem | null, _proposed: DurableCalendarItem, mutationId: string) => {
      remoteAttempts += 1;
      const committed = schedules.get(mutationId) ?? {
        scheduleId: "sched-only-once",
        scheduleRevision: 1,
        scheduleStatus: "active",
      };
      schedules.set(mutationId, committed);
      return committed;
    },
    deleteSchedule: async () => {},
  };
  const firstAcknowledgement = await replayCalendarRemoteMutation(loaded.document.items, mutation, remote);
  storage.failWrites = true;
  assert.throws(
    () => settleCalendarMutation(storage, "tab-a", firstAcknowledgement),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "STORAGE_UNAVAILABLE",
  );
  storage.failWrites = false;

  const reloaded = loadCalendarDocument(storage, "tab-a");
  assert.equal(reloaded.pendingMutation?.mutationId, "m-create");
  const retryAcknowledgement = await replayCalendarRemoteMutation(
    reloaded.document.items,
    reloaded.pendingMutation!,
    remote,
  );
  const settled = settleCalendarMutation(storage, "tab-a", retryAcknowledgement);

  assert.equal(remoteAttempts, 2);
  assert.equal(schedules.size, 1);
  assert.equal(settled.items[0]?.scheduleId, "sched-only-once");
  assert.equal(settled.items[0]?.syncState, "settled");
});

test("repair refuses to overwrite a newer tab revision", () => {
  const storage = new MemoryStorage();
  const loaded = loadCalendarDocument(storage, "tab-a");
  const mutation = beginCalendarMutation(storage, loaded.document, {
    mutationId: "m1",
    operation: "upsert",
    proposedItems: [item],
  });
  settleCalendarMutation(storage, "tab-a", mutation);
  assert.throws(
    () => repairCalendarDocument(storage, "tab-b", [], 0),
    (error: unknown) => error instanceof CalendarPersistenceError && error.code === "CONFLICT",
  );
  assert.equal(loadCalendarDocument(storage, "tab-a").document.items.length, 1);
});

test("reconnect restores remote orphans and marks missing schedules", () => {
  const local: DurableCalendarItem[] = [
    { ...item, scheduleId: "sched-missing", calendarRevision: 1 },
  ];
  const remote = {
    id: "sched-remote",
    name: "Calendar: Restored task",
    status: "active",
    revision: 3,
    task: {
      message: "restore me",
      metadata: {
        source: "calendar",
        calendar_item_id: "calendar-remote",
        calendar_start_date: "2026-08-01",
        calendar_end_date: "2026-08-01",
        calendar_time: "10:30",
        calendar_revision: 2,
      },
    },
  };
  const result = reconcileCalendarSchedules(local, [remote]);
  assert.equal(result.items.length, 2);
  assert.equal(result.items.find((entry) => entry.id === item.id)?.syncState, "missing_schedule");
  assert.equal(result.items.find((entry) => entry.id === "calendar-remote")?.scheduleId, "sched-remote");
  assert.deepEqual(result.issues.map((issue) => issue.kind).sort(), ["missing_schedule", "remote_orphan"]);
});

test("newer backend revisions deterministically repair stale local records", () => {
  const result = reconcileCalendarSchedules(
    [{ ...item, title: "Old", scheduleId: "sched-1", calendarRevision: 1 }],
    [{
      id: "sched-1",
      name: "Calendar: New",
      status: "active",
      revision: 4,
      task: {
        message: "new prompt",
        metadata: {
          source: "calendar",
          calendar_item_id: item.id,
          calendar_start_date: item.date,
          calendar_time: item.time,
          calendar_revision: 2,
          calendar_item_snapshot: { ...item, title: "New", calendarRevision: 2 },
        },
      },
    }],
  );
  assert.equal(result.items[0]?.title, "New");
  assert.equal(result.items[0]?.scheduleRevision, 4);
  assert.deepEqual(result.issues, []);
});

test("a committed backend schedule completes an equal-revision pending local item", () => {
  const result = reconcileCalendarSchedules(
    [{ ...item, calendarRevision: 1, syncState: "pending" }],
    [{
      id: "sched-acknowledged",
      name: "Calendar: Daily review",
      status: "active",
      revision: 1,
      task: {
        message: "Daily review",
        metadata: {
          source: "calendar",
          calendar_item_id: item.id,
          calendar_start_date: item.date,
          calendar_time: item.time,
          calendar_revision: 1,
          calendar_item_snapshot: { ...item, calendarRevision: 1 },
        },
      },
    }],
  );
  assert.equal(result.items[0]?.scheduleId, "sched-acknowledged");
  assert.equal(result.items[0]?.syncState, "settled");
  assert.deepEqual(result.issues, []);
});

test("same-revision backend content drift is surfaced as a conflict", () => {
  const result = reconcileCalendarSchedules(
    [{ ...item, scheduleId: "sched-1", scheduleRevision: 1, calendarRevision: 1 }],
    [{
      id: "sched-1",
      name: "Calendar: Changed elsewhere",
      status: "active",
      revision: 2,
      task: {
        message: "Changed elsewhere",
        metadata: {
          source: "calendar",
          calendar_item_id: item.id,
          calendar_start_date: item.date,
          calendar_time: item.time,
          calendar_revision: 1,
          calendar_item_snapshot: { ...item, title: "Changed elsewhere", calendarRevision: 1 },
        },
      },
    }],
  );
  assert.equal(result.items[0]?.syncState, "conflict");
  assert.deepEqual(result.issues.map((issue) => issue.kind), ["conflict"]);
});

test("duplicate backend schedules are selected deterministically and never hidden", () => {
  const schedule = (id: string, revision: number) => ({
    id,
    name: "Calendar: Daily review",
    status: "active",
    revision,
    task: {
      message: "Daily review",
      metadata: {
        source: "calendar",
        calendar_item_id: item.id,
        calendar_start_date: item.date,
        calendar_time: item.time,
        calendar_revision: 1,
        calendar_item_snapshot: { ...item, calendarRevision: 1 },
      },
    },
  });
  const result = reconcileCalendarSchedules([], [
    schedule("sched-older", 1),
    schedule("sched-newer", 2),
  ]);
  assert.equal(result.items[0]?.scheduleId, "sched-newer");
  assert.deepEqual(result.issues.map((issue) => issue.kind).sort(), ["conflict", "remote_orphan"]);
});

test("an unacknowledged pending Agent task is not treated as settled", () => {
  const result = reconcileCalendarSchedules(
    [{ ...item, agentPrompt: "Run daily review", calendarRevision: 1, syncState: "pending" }],
    [],
  );
  assert.equal(result.items[0]?.syncState, "missing_schedule");
  assert.deepEqual(result.issues.map((issue) => issue.kind), ["missing_schedule"]);
});
