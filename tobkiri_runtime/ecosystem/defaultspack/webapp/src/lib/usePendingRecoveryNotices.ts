import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { savedTurnStoreIdentity, type PendingRecoveryEntry } from "./scopedPendingChat";
import { savedTurnActionFailureNotice } from "./pendingChat";

export type RecoveryReadTarget = {
  id: string;
  storeId: string | null;
  conversationId: string;
  operationId: string | null;
  savedTurn: boolean;
};
export type RecoveryReadScope = {
  profileId: string;
  currentStoreId: string | null;
  targets: RecoveryReadTarget[];
};

/** Only read a retained turn when its persisted owner is the current Host store. */
export function pendingRecoveryReadTargets(
  readScope: RecoveryReadScope,
): RecoveryReadTarget[] {
  if (!readScope.profileId.trim()) return [];
  const currentStoreId = savedTurnStoreIdentity(readScope.currentStoreId);
  if (!currentStoreId) return [];
  return readScope.targets.filter((target) => (
    target.savedTurn
    && target.storeId === currentStoreId
    && typeof target.operationId === "string"
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(target.operationId)
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(target.conversationId)
    && target.id === `${currentStoreId}:${target.conversationId}:${target.operationId}`
  )).slice(0, 16);
}

/** Read-only reconciliation for exact pending identities retained from an older store. */
export function usePendingRecoveryNotices(
  profileId: string,
  storeId: string | null,
  entries: PendingRecoveryEntry[],
): Record<string, string> {
  const scope = JSON.stringify({
    profileId,
    currentStoreId: storeId,
    targets: entries.map(({ id, storeId: entryStoreId, request }) => ({
      id,
      storeId: entryStoreId,
      conversationId: request.conversationId,
      operationId: request.operationId ?? null,
      savedTurn: request.savedTurn === true,
    })),
  });
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [notices, setNotices] = useState<{ scope: string; values: Record<string, string> } | null>(null);

  useEffect(() => {
    let disposed = false;
    const readScope = JSON.parse(scope) as RecoveryReadScope;
    const eligible = pendingRecoveryReadTargets(readScope);
    if (!eligible.length) {
      setNotices({ scope, values: {} });
      return () => { disposed = true; };
    }
    void Promise.all(eligible.map(async (target) => {
      try {
        const snapshot = await api.getSavedTurnEvents(
          target.operationId!,
          target.conversationId,
        );
        if (disposed || currentScope.current !== scope) return null;
        const message = savedTurnActionFailureNotice(
          snapshot,
          target.conversationId,
          target.operationId!,
        );
        return message ? [target.id, message] as const : null;
      } catch {
        return null;
      }
    })).then((results) => {
      if (disposed || currentScope.current !== scope) return;
      setNotices({
        scope,
        values: Object.fromEntries(results.filter((item): item is readonly [string, string] => item !== null)),
      });
    });
    return () => { disposed = true; };
  }, [scope]);

  return notices?.scope === scope ? notices.values : {};
}
