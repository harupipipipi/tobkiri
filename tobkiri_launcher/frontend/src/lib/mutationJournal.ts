import {recordClientDiagnostic} from './clientDiagnostics';
import {
  getBrowserStorage,
  readSafeStorageValue,
  writeSafeStorageValue,
} from './safeStorage';

const MUTATION_JOURNAL_STORAGE_KEY = 'tobkiri-launcher-mutation-journal-v1';

export type MutationJournalState = 'pending' | 'unknown' | 'invalid';

export interface MutationJournalRecord {
  key: string;
  requestId: string;
  state: MutationJournalState;
  createdAt: number;
  metadata: Record<string, unknown>;
}

export class MutationResultUnknownError extends Error {
  readonly mutationKey: string;
  readonly requestId: string;

  constructor(mutationKey: string, requestId: string) {
    super('The mutation result is unknown. Refresh the authoritative state before trying again.');
    this.name = 'MutationResultUnknownError';
    this.mutationKey = mutationKey;
    this.requestId = requestId;
  }
}

export class MutationBlockedError extends MutationResultUnknownError {
  readonly record: MutationJournalRecord;
  readonly journalState: MutationJournalState;

  constructor(record: MutationJournalRecord) {
    super(record.key, record.requestId);
    this.record = record;
    this.name = 'MutationBlockedError';
    this.journalState = record.state;
  }
}

const memoryJournal = new Map<string, MutationJournalRecord>();
let observedStorage: unknown = null;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === 'string'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function currentStorage(): Storage | null {
  return getBrowserStorage('local');
}

function synchronizeStorageContext(storage: Storage | null): void {
  if (storage === observedStorage) return;
  memoryJournal.clear();
  observedStorage = storage;
}

function readStoredRecords(): MutationJournalRecord[] {
  const raw = readSafeStorageValue(
    currentStorage(),
    MUTATION_JOURNAL_STORAGE_KEY,
  );
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((value): MutationJournalRecord[] => {
      if (!isRecord(value)) return [];
      const key = value.key;
      const requestId = value.requestId;
      const state = value.state;
      const createdAt = value.createdAt;
      const metadata = value.metadata;
      if (
        typeof key !== 'string'
        || key.length === 0
        || !isUuid(requestId)
        || (state !== 'pending' && state !== 'unknown' && state !== 'invalid')
        || typeof createdAt !== 'number'
        || !Number.isFinite(createdAt)
        || !isRecord(metadata)
      ) return [{
        key: typeof key === 'string' ? key : 'invalid-journal-entry',
        requestId: isUuid(requestId) ? requestId : '',
        state: 'invalid',
        createdAt: typeof createdAt === 'number' && Number.isFinite(createdAt) ? createdAt : 0,
        metadata: {},
      }];
      return [{
        key,
        requestId,
        // A pending request cannot be assumed to have failed after a reload.
        // Hydration therefore deliberately turns it into an unknown result.
        state: state === 'pending' ? 'unknown' : state,
        createdAt,
        metadata,
      }];
    });
  } catch (error) {
    recordClientDiagnostic({
      code: 'mutation.journal.parse_failed',
      operation: 'mutation.journal.hydrate',
      error,
    });
    return [{
      key: 'invalid-journal-storage',
      requestId: '',
      state: 'invalid',
      createdAt: 0,
      metadata: {},
    }];
  }
}

function allRecords(): MutationJournalRecord[] {
  const storage = currentStorage();
  synchronizeStorageContext(storage);
  const storedRecords = readStoredRecords();
  const merged = new Map<string, MutationJournalRecord>();
  for (const record of storedRecords) merged.set(record.key, record);
  // A fresh browsing context can replace localStorage while this module's
  // in-memory map is still reachable in a test harness. Treat persisted
  // storage as authoritative whenever it exists so stale memory cannot
  // resurrect an unrelated request after re-hydration.
  if (!storage) {
    for (const record of memoryJournal.values()) merged.set(record.key, record);
  } else {
    for (const record of memoryJournal.values()) {
      const stored = storedRecords.find((item) => (
        item.key === record.key && item.requestId === record.requestId
      ));
      // The in-memory pending state is authoritative for the current
      // browsing context. A fresh context has no matching memory record and
      // therefore hydrates a persisted pending request as unknown.
      if (stored) merged.set(record.key, record);
    }
  }
  return [...merged.values()];
}

function persist(records: MutationJournalRecord[]): void {
  const storage = currentStorage();
  synchronizeStorageContext(storage);
  for (const record of records) memoryJournal.set(record.key, record);
  if (!storage) return;
  if (!writeSafeStorageValue(storage, MUTATION_JOURNAL_STORAGE_KEY, JSON.stringify(records))) {
    recordClientDiagnostic({
      code: 'mutation.journal.memory_fallback',
      operation: 'mutation.journal.persist',
    });
  }
}

function newRequestId(): string {
  return crypto.randomUUID();
}

export function listMutationJournal(): MutationJournalRecord[] {
  const records = allRecords();
  persist(records);
  return records;
}

/**
 * Start one logical mutation, retaining its request identity until it is
 * explicitly reconciled or definitively rejected by the server.
 */
export function beginMutation(
  key: string,
  metadata: Record<string, unknown> = {},
  requestIds: Record<string, string> = {},
): MutationJournalRecord {
  const records = listMutationJournal();
  const existing = records.find((record) => record.key === key);
  if (existing) throw new MutationBlockedError(existing);
  const primaryRequestId = requestIds.primary ?? newRequestId();
  const allRequestIds = {primary: primaryRequestId, ...requestIds};
  if (Object.values(allRequestIds).some((requestId) => !isUuid(requestId))) {
    throw new Error('The mutation request identity is invalid.');
  }
  const record: MutationJournalRecord = {
    key,
    requestId: primaryRequestId,
    state: 'pending',
    createdAt: Date.now(),
    metadata: {...metadata, request_ids: allRequestIds},
  };
  persist([...records, record]);
  return record;
}

export function mutationRequestId(
  record: MutationJournalRecord,
  phase = 'primary',
): string {
  const requestIds = record.metadata.request_ids;
  const phaseRequestId = isRecord(requestIds) ? requestIds[phase] : undefined;
  if (isUuid(phaseRequestId)) return phaseRequestId;
  return record.requestId;
}

export function markMutationUnknown(
  key: string,
  requestId: string,
): MutationJournalRecord {
  const records = listMutationJournal();
  const current = records.find((record) => record.key === key);
  if (!current || current.requestId !== requestId) {
    throw new Error('The mutation journal no longer matches the active request.');
  }
  const next = {...current, state: 'unknown' as const};
  persist(records.map((record) => record.key === key ? next : record));
  return next;
}

export function completeMutation(key: string, requestId?: string): void {
  const records = listMutationJournal();
  const current = records.find((record) => record.key === key);
  if (current && requestId && current.requestId !== requestId) return;
  persist(records.filter((record) => record.key !== key));
  memoryJournal.delete(key);
}

/**
 * Pin the server's operation request digest to the durable journal record.
 *
 * The browser cannot mint or replace this digest. It is learned only from a
 * session-bound status projection and any later change is treated as a stale
 * or tampered operation record by the status reconciler.
 */
export function bindMutationStatusDigest(
  key: string,
  requestId: string,
  requestDigest: string,
  statusRequestId = requestId,
  statusPhase = 'primary',
): MutationJournalRecord {
  if (!/^sha256:[0-9a-f]{64}$/.test(requestDigest)) {
    throw new Error('The operation status request digest is invalid.');
  }
  if (!isUuid(statusRequestId)) {
    throw new Error('The operation status request identity is invalid.');
  }
  const records = listMutationJournal();
  const current = records.find((record) => record.key === key);
  if (!current || current.requestId !== requestId) {
    throw new Error('The mutation journal no longer matches the operation status.');
  }
  const storedDigests = current.metadata.status_request_digests;
  if (storedDigests !== undefined && !isRecord(storedDigests)) {
    throw new Error('The journaled operation status digest set is invalid.');
  }
  const storedRequestIds = current.metadata.status_request_ids;
  if (storedRequestIds !== undefined && !isRecord(storedRequestIds)) {
    throw new Error('The journaled operation status identity set is invalid.');
  }
  const existingRequestId = isRecord(storedRequestIds)
    ? storedRequestIds[statusPhase]
    : undefined;
  if (existingRequestId !== undefined && existingRequestId !== statusRequestId) {
    throw new Error('The operation status request identity changed.');
  }
  const existing = isRecord(storedDigests)
    ? storedDigests[statusPhase]
    : statusPhase === 'primary'
      ? current.metadata.status_request_digest
      : undefined;
  if (existing !== undefined && existing !== requestDigest) {
    throw new Error('The operation status request digest changed.');
  }
  if (
    existing === requestDigest
    && existingRequestId === statusRequestId
  ) return current;
  const next = {
    ...current,
    metadata: {
      ...current.metadata,
      ...(statusPhase === 'primary' ? {status_request_digest: requestDigest} : {}),
      status_request_ids: {
        ...(isRecord(storedRequestIds) ? storedRequestIds : {}),
        [statusPhase]: statusRequestId,
      },
      status_request_digests: {
        ...(isRecord(storedDigests) ? storedDigests : {}),
        [statusPhase]: requestDigest,
      },
    },
  };
  persist(records.map((record) => record.key === key ? next : record));
  return next;
}

export function isMutationResultUnknown(error: unknown): boolean {
  if (error instanceof MutationResultUnknownError) return true;
  if (!error || typeof error !== 'object') return false;
  const candidate = error as {name?: unknown; message?: unknown};
  if (candidate.name === 'ApiContractError') return false;
  if (candidate.name === 'AbortError' || candidate.name === 'ApiRequestTimeoutError') return true;
  const message = typeof candidate.message === 'string' ? candidate.message.toLowerCase() : '';
  return message.includes('timed out')
    || message.includes('timeout')
    || message.includes('failed to fetch')
    || message.includes('network error')
    || message.includes('networkerror')
    || message.includes('load failed');
}

export const MUTATION_UNKNOWN_MESSAGE =
  'The request result is unknown. Refresh the authoritative projection before trying again; no new request will be sent automatically.';
