import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Conversation, type SavedTurnEventSnapshot } from "./api";
import type { PendingChatRequest } from "./pendingChat";
import { mergeSavedChatProgress, savedChatProgressMessage, savedChatProgressWitness, type SavedChatProgressState } from "./savedChatProgress";

/** Reads actual protected owner progress; no execution or replay is issued. */
export function useSavedChatProgress(
  storeId: string | null, snapshot: SavedTurnEventSnapshot | null | undefined,
  conversation: Conversation | null, pending: PendingChatRequest | null | undefined,
) {
  const witness = savedChatProgressWitness(snapshot, conversation, pending);
  const scope = storeId && witness ? `${storeId}:${JSON.stringify(witness)}` : null;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [display, setDisplay] = useState<{ scope: string; progress: SavedChatProgressState } | null>(null);
  const capturedWitness = useMemo(() => witness, [scope]);
  useEffect(() => {
    if (!scope || !capturedWitness) return;
    let disposed = false;
    let reading = false;
    let progress: SavedChatProgressState | null = null;
    const read = async () => {
      if (disposed || reading || currentScope.current !== scope) return;
      if (progress && progress.stage.expiresAtMs <= Date.now()) {
        progress = null;
        setDisplay((current) => current?.scope === scope ? null : current);
      }
      reading = true;
      try {
        const page = await api.getSavedTurnProgress(capturedWitness.turnId, capturedWitness.conversationId,
          progress?.stage.cursor ?? 0, progress?.stage.progressId);
        if (disposed || currentScope.current !== scope) return;
        const merged = mergeSavedChatProgress(progress, page, capturedWitness);
        if (!merged) {
          progress = null;
          setDisplay((current) => current?.scope === scope ? null : current);
          return;
        }
        progress = merged;
        setDisplay({ scope, progress: merged });
      } catch { /* Retain only unexpired confirmed display events; canonical polling owns errors. */ }
      finally { reading = false; }
    };
    void read();
    const timer = window.setInterval(() => { void read(); }, 750);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [scope, capturedWitness]);
  const progress = display?.scope === scope ? display.progress : null;
  return savedChatProgressMessage(progress, witness, conversation);
}
