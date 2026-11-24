import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { useVerifiedFrontendHost } from "../../host/VerifiedFrontendHostContext";
import { createModelPinStore, modelPinStorageKey, resolveModelPinScope, createModelPinOperationGuard } from "./modelPins";

const storage = {
  getItem(key: string): string | null {
    return typeof window === "undefined" ? null : window.localStorage.getItem(key);
  },
  setItem(key: string, value: string): void {
    if (typeof window !== "undefined") window.localStorage.setItem(key, value);
  },
};
const store = createModelPinStore(storage);

/** Bind pins only to the current verified host's stable profile identity. */
export function useModelPins() {
  const host = useVerifiedFrontendHost();
  const { profileId, fence, enabled } = resolveModelPinScope(host?.catalog, host?.activePlanHash);
  const guard = useRef(createModelPinOperationGuard()).current;
  const latestFence = useRef(fence);
  latestFence.current = fence;
  const scope = profileId;
  const subscribe = useCallback((listener: () => void) => store.subscribe(scope, listener), [scope]);
  const snapshot = useCallback(() => store.getSnapshot(scope), [scope]);
  const pins = useSyncExternalStore(subscribe, snapshot, snapshot);
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    const dispose = guard.activate(fence);
    store.storageChanged(scope);
    const onStorage = (event: StorageEvent): void => {
      if (event.storageArea !== null) {
        try { if (event.storageArea !== window.localStorage) return; }
        catch { return; }
      }
      if (event.key === null || event.key === modelPinStorageKey(scope)) {
        store.storageChanged(scope, event.key === null ? null : event.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => {
      dispose();
      window.removeEventListener("storage", onStorage);
    };
  }, [enabled, scope, fence, guard]);
  const isPinned = useCallback((identity: string | null): boolean =>
    enabled && identity !== null && pins.includes(identity), [enabled, pins]);
  const toggle = useCallback((identity: string | null): void => {
    if (enabled && identity !== null && fence === latestFence.current && guard.isCurrent(fence)) store.toggle(scope, identity);
  }, [enabled, scope, fence, guard]);
  return { pins, enabled, isPinned, toggle };
}
