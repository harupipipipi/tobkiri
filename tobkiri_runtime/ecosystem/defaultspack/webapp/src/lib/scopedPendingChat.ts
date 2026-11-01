import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { resetPersistedSavedTurnOwnerObservations, type PendingChatRequest } from "./pendingChat";

export type PendingRecoveryEntry = {
  id: string;
  storeId: string | null;
  request: PendingChatRequest;
  archived: boolean;
};
type PendingMap = Record<string, PendingChatRequest>;
type PendingStorage = { scopes: Record<string, PendingMap>; archive: PendingRecoveryEntry[] };
type StorageLike = Pick<Storage, "getItem" | "setItem">;

export function savedTurnStoreIdentity(value: unknown): string | null {
  return typeof value === "string" && /^sha256:[a-f0-9]{64}$/.test(value) ? value : null;
}

function readJson(storage: StorageLike, key: string): unknown {
  try { return JSON.parse(storage.getItem(key) ?? "null"); } catch { return null; }
}
function pendingMap(value: unknown): PendingMap {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([id, request]) => (
    request && typeof request === "object" && request.conversationId === id
    && typeof request.startedAt === "number" && typeof request.status === "string"
    && Array.isArray(request.toolNames)
  )));
}
function keys(profileId: string): { current: string; legacy: string } {
  const profile = encodeURIComponent(profileId);
  return { current: `rumi-pending-chat-v2:${profile}`, legacy: `rumi-pending-chat-requests:${profile}` };
}
function readStorage(storage: StorageLike, profileId: string): PendingStorage {
  const raw = readJson(storage, keys(profileId).current) as Partial<PendingStorage> | null;
  const scopes = Object.fromEntries(Object.entries(raw?.scopes ?? {}).map(([id, map]) => [id, pendingMap(map)]));
  const archive = Array.isArray(raw?.archive) ? raw.archive.filter((entry) => (
    entry && typeof entry.id === "string" && entry.request
    && entry.archived === true && Object.keys(pendingMap({ [entry.request.conversationId]: entry.request })).length === 1
  )) : [];
  return { scopes, archive };
}

/** Read one owner's pending requests without adopting unknown or foreign IDs. */
export function readScopedPendingChat(storage: StorageLike, profileId: string, storeId: string | null): {
  pending: PendingMap; recovery: PendingRecoveryEntry[];
} {
  const document = readStorage(storage, profileId);
  const recovery = [...document.archive];
  const archivedIds = new Set(recovery.map((entry) => entry.id));
  const append = (source: string | null, requests: PendingMap) => {
    for (const request of Object.values(requests)) {
      const id = `${source ?? "legacy"}:${request.conversationId}:${request.operationId ?? "unknown"}`;
      if (!archivedIds.has(id)) recovery.push({ id, storeId: source, request, archived: false });
    }
  };
  append(null, pendingMap(readJson(storage, keys(profileId).legacy)));
  for (const [source, requests] of Object.entries(document.scopes)) {
    if (source !== storeId) append(source, requests);
  }
  return { pending: storeId ? document.scopes[storeId] ?? {} : {}, recovery };
}

/** Writes are explicit and always target the captured store, never a new key. */
export function writeScopedPendingChat(storage: StorageLike, profileId: string, storeId: string, pending: PendingMap): void {
  const document = readStorage(storage, profileId);
  document.scopes[storeId] = pending;
  storage.setItem(keys(profileId).current, JSON.stringify(document));
}

/** Retains an unresolved identity without claiming remote cancellation. */
export function archiveScopedPendingChat(storage: StorageLike, profileId: string, entry: PendingRecoveryEntry): void {
  const document = readStorage(storage, profileId);
  if (!document.archive.some((item) => item.id === entry.id)) {
    document.archive.push({ ...entry, archived: true });
  }
  if (entry.storeId) {
    const map = document.scopes[entry.storeId];
    if (map?.[entry.request.conversationId]?.operationId === entry.request.operationId) {
      delete map[entry.request.conversationId];
    }
  }
  storage.setItem(keys(profileId).current, JSON.stringify(document));
}

/** Async store discovery cannot copy persisted requests between owners. */
export function useScopedPendingChat(profileId: string, storeId: string | null) {
  const [revision, render] = useState(0);
  const [persistenceError, setPersistenceError] = useState(false);
  const memory = useRef(new Map<string, string>());
  const unpersisted = useRef(new Set<string>());
  const storage = useMemo<StorageLike>(() => ({
    getItem: (key) => {
      if (unpersisted.current.has(key)) return memory.current.get(key) ?? null;
      try {
        const live = localStorage.getItem(key);
        if (live !== null) memory.current.set(key, live);
        else memory.current.delete(key);
        return live;
      } catch { return memory.current.get(key) ?? null; }
    },
    setItem: (key, value) => {
      // A full or unavailable browser store must not erase an in-flight identity.
      memory.current.set(key, value);
      try { localStorage.setItem(key, value); unpersisted.current.delete(key); }
      catch { unpersisted.current.add(key); }
      setPersistenceError(unpersisted.current.size > 0);
    },
  }), []);
  const scope = `${profileId}:${storeId ?? "unverified"}`;
  const currentScope = useRef({ scope, generation: 0 });
  if (currentScope.current.scope !== scope) {
    currentScope.current = { scope, generation: currentScope.current.generation + 1 };
  }
  const generation = currentScope.current.generation;
  const loadedScope = useRef<string | null>(null);
  const snapshot = useMemo(() => {
    const read = readScopedPendingChat(storage, profileId, storeId);
    // Withhold old ownership during render; persist the reset only after commit.
    return loadedScope.current === scope ? read : {
      ...read, pending: resetPersistedSavedTurnOwnerObservations(read.pending),
    };
  }, [profileId, storeId, scope, revision, storage]);
  useLayoutEffect(() => {
    if (!storeId || loadedScope.current === scope) return;
    loadedScope.current = scope;
    const previous = readScopedPendingChat(storage, profileId, storeId).pending;
    const reset = resetPersistedSavedTurnOwnerObservations(previous);
    if (reset !== previous) writeScopedPendingChat(storage, profileId, storeId, reset);
  }, [profileId, storeId, scope, storage]);
  const setPending = useCallback((value: PendingMap | ((current: PendingMap) => PendingMap)) => {
    if (!storeId || (currentScope.current.scope !== scope || currentScope.current.generation !== generation)) return;
    const current = readScopedPendingChat(storage, profileId, storeId).pending;
    const next = typeof value === "function" ? value(current) : value;
    if (next === current) return;
    writeScopedPendingChat(storage, profileId, storeId, next);
    render((value) => value + 1);
  }, [profileId, scope, storeId, storage, generation]);
  const archive = useCallback((entry: PendingRecoveryEntry) => {
    if ((currentScope.current.scope !== scope || currentScope.current.generation !== generation)) return;
    archiveScopedPendingChat(storage, profileId, entry);
    render((value) => value + 1);
  }, [profileId, scope, storage, generation]);
  return { ...snapshot, setPending, archive, persistenceError };
}
