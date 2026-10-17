import type { SavedTurnEventSnapshot } from "./api";
import type { SavedTurnViewTicket } from "./optimisticSavedTurn";
import { savedTurnActionFailureNotice, savedTurnTerminalNotice, type PendingChatRequest } from "./pendingChat";

const STABLE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

export type PendingTerminalScope = {
  profileId: string;
  storeId: string | null;
  view: SavedTurnViewTicket;
};

export type PendingTerminalObservation = {
  snapshot: SavedTurnEventSnapshot;
  notice: string;
};

/** Re-read after the existing active-turn reconciliation; never replay a send. */
export async function freshPendingTurnSnapshot({
  initial, isCurrent, reconcile, readEvents,
}: {
  initial: SavedTurnEventSnapshot;
  isCurrent: () => boolean;
  reconcile: () => Promise<unknown>;
  readEvents: () => Promise<SavedTurnEventSnapshot>;
}): Promise<SavedTurnEventSnapshot | null> {
  if (!isCurrent()) return null;
  if (initial.turn.status !== "running" && initial.turn.status !== "waiting") return initial;
  await reconcile();
  if (!isCurrent()) return null;
  const fresh = await readEvents();
  return isCurrent() ? fresh : null;
}

/** Terminal display belongs to the exact request and its captured owner view. */
export function observePendingTerminal(
  snapshot: SavedTurnEventSnapshot | null | undefined,
  expected: PendingChatRequest,
  current: Record<string, PendingChatRequest>,
  readScope: PendingTerminalScope,
  currentScope: PendingTerminalScope,
): PendingTerminalObservation | null {
  const entry = current[expected.conversationId];
  if (!expected.savedTurn || !STABLE_ID.test(expected.conversationId)
    || !STABLE_ID.test(expected.operationId ?? "")
    || !STABLE_ID.test(expected.requestFingerprint ?? "")
    || !entry?.savedTurn || entry.conversationId !== expected.conversationId
    || entry.operationId !== expected.operationId
    || entry.requestFingerprint !== expected.requestFingerprint
    || !STABLE_ID.test(readScope.profileId) || !readScope.storeId
    || readScope.profileId !== currentScope.profileId
    || readScope.storeId !== currentScope.storeId
    || readScope.view.conversationId !== expected.conversationId
    || currentScope.view.conversationId !== readScope.view.conversationId
    || currentScope.view.workspaceTabId !== readScope.view.workspaceTabId
    || !STABLE_ID.test(readScope.view.workspaceTabId)
    || !Number.isSafeInteger(readScope.view.epoch) || readScope.view.epoch < 0
    || currentScope.view.epoch !== readScope.view.epoch) return null;
  const turn = snapshot?.turn;
  const terminal = snapshot?.terminal;
  if (!snapshot || !turn || !terminal
    || !["failed", "cancelled"].includes(snapshot.status)
    || snapshot.turn_id !== expected.operationId
    || snapshot.operation_id !== expected.operationId
    || snapshot.conversation_id !== expected.conversationId
    || !STABLE_ID.test(snapshot.request_id)
    || !Number.isSafeInteger(snapshot.turn_revision) || snapshot.turn_revision < 1
    || !Array.isArray(snapshot.events)
    || snapshot.events.some((event, sequence) => !event
      || event.turn_id !== snapshot.turn_id || event.operation_id !== snapshot.operation_id
      || event.conversation_id !== snapshot.conversation_id
      || event.request_id !== snapshot.request_id || event.turn_revision !== snapshot.turn_revision
      || event.sequence !== sequence || typeof event.name !== "string" || !event.name.startsWith("turn."))
    || turn.id !== snapshot.turn_id || turn.conversation_id !== snapshot.conversation_id
    || turn.status !== snapshot.status || turn.revision !== snapshot.turn_revision
    || (turn.request_id !== undefined && turn.request_id !== snapshot.request_id)
    || terminal.turn_id !== snapshot.turn_id || terminal.operation_id !== snapshot.operation_id
    || terminal.conversation_id !== snapshot.conversation_id
    || terminal.request_id !== snapshot.request_id || terminal.status !== snapshot.status
    || terminal.turn_revision !== snapshot.turn_revision) return null;
  const notice = savedTurnActionFailureNotice(snapshot, expected.conversationId, expected.operationId!)
    ?? savedTurnTerminalNotice(turn, expected.conversationId, expected.operationId!);
  return notice ? { snapshot, notice } : null;
}

/** Remove only this observed request; a later request and legacy recovery survive. */
export function removeObservedPendingTerminal(
  current: Record<string, PendingChatRequest>,
  expected: PendingChatRequest,
  observation: PendingTerminalObservation,
  readScope: PendingTerminalScope,
  currentScope: PendingTerminalScope,
): Record<string, PendingChatRequest> {
  if (!observePendingTerminal(observation.snapshot, expected, current, readScope, currentScope)) return current;
  const next = { ...current };
  delete next[expected.conversationId];
  return next;
}

/** Publish proven termination before any optional history read can stall or fail. */
export function publishPendingTerminal({
  snapshot, expected, current, readScope, currentScope, publish, refreshHistory,
}: {
  snapshot: SavedTurnEventSnapshot | null | undefined;
  expected: PendingChatRequest;
  current: Record<string, PendingChatRequest>;
  readScope: PendingTerminalScope;
  currentScope: PendingTerminalScope;
  publish: (observation: PendingTerminalObservation) => void;
  refreshHistory: () => Promise<unknown>;
}): boolean {
  const observation = observePendingTerminal(snapshot, expected, current, readScope, currentScope);
  if (!observation) return false;
  publish(observation);
  // History errors do not undo an owner-proven terminal outcome or permit resend.
  void Promise.resolve().then(refreshHistory).catch(() => undefined);
  return true;
}
