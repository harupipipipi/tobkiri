import { useEffect, useRef } from "react";

import { cn } from "../lib/cn";
import {
  backendConnectionCopy,
  type BackendPendingOperation,
  type BackendConnectionState,
} from "../lib/backendConnection";
import type { LocaleSetting } from "../lib/i18n";

type BackendConnectionBannerProps = {
  state: BackendConnectionState;
  lastHealthyAt: number | null;
  pendingOperation: BackendPendingOperation;
  locale: LocaleSetting;
  onCheckConnection: () => void;
};

export function BackendConnectionBanner({
  state,
  lastHealthyAt,
  pendingOperation,
  locale,
  onCheckConnection,
}: BackendConnectionBannerProps) {
  const previousStateRef = useRef(state);
  const announceRecovery = previousStateRef.current !== "online"
    && state === "online";
  useEffect(() => {
    previousStateRef.current = state;
  }, [state]);

  const copy = backendConnectionCopy(
    state,
    lastHealthyAt,
    pendingOperation,
    locale,
  );

  if (state === "online") {
    if (!announceRecovery) return null;
    return (
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
        data-backend-connection-state="online"
      >
        {copy.title}. {copy.detail}
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-backend-connection-state={state}
      className={cn(
        "mx-3 mt-3 rounded-2xl border px-4 py-3",
        state === "offline"
          ? "border-red-500/20 bg-red-500/10 text-red-100"
          : "border-amber-500/20 bg-amber-500/10 text-amber-100",
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full",
            state === "offline"
              ? "bg-red-400"
              : "bg-amber-300 animate-pulse motion-reduce:animate-none",
          )}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{copy.title}</p>
          <p className="mt-1 text-xs leading-5 opacity-90">{copy.detail}</p>
        </div>
        <button
          type="button"
          onClick={onCheckConnection}
          className="shrink-0 rounded-xl border border-current/20 px-3 py-1.5 text-xs font-semibold text-current transition hover:bg-white/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"
        >
          {copy.actionLabel}
        </button>
      </div>
    </div>
  );
}
