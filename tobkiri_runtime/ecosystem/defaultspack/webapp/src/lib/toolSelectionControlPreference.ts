import { useCallback, useSyncExternalStore } from "react";

type PreferenceStorage = Pick<Storage, "getItem" | "setItem">;
const listeners = new Map<string, Set<() => void>>();

/** Keep this display preference separate from Host settings and tool authority. */
export function toolSelectionControlPreferenceKey(profileId?: string): string | null {
  if (!profileId || !profileId.trim() || profileId === "unavailable") return null;
  return `tobkiri:tool-selection-control:v1:${encodeURIComponent(profileId)}`;
}

function browserStorage(): PreferenceStorage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; }
  catch { return null; }
}

/** Hydrate only an explicit Boolean value, without writing during a read. */
export function readToolSelectionControlPreference(
  profileId?: string,
  storage: PreferenceStorage | null = browserStorage(),
): boolean {
  const key = toolSelectionControlPreferenceKey(profileId);
  try { return key !== null && storage?.getItem(key) === "true"; }
  catch { return false; }
}

/** Publish a Profile-local display change only after storage accepts the write. */
export function saveToolSelectionControlPreference(
  profileId: string | undefined,
  visible: boolean,
  storage: PreferenceStorage | null = browserStorage(),
): boolean {
  const key = toolSelectionControlPreferenceKey(profileId);
  if (key === null || typeof visible !== "boolean" || !storage) return false;
  try { storage.setItem(key, String(visible)); }
  catch { return false; }
  listeners.get(key)?.forEach((listener) => listener());
  return true;
}

/** Refresh Settings and composers for same-tab and other-tab display changes. */
export function subscribeToolSelectionControlPreference(
  profileId: string | undefined,
  listener: () => void,
): () => void {
  const key = toolSelectionControlPreferenceKey(profileId);
  if (key === null) return () => undefined;
  const profileListeners = listeners.get(key) ?? new Set<() => void>();
  listeners.set(key, profileListeners);
  profileListeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === key) listener();
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    profileListeners.delete(listener);
    if (!profileListeners.size) listeners.delete(key);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

/** Share visibility only; selecting a tool mode still uses the existing owner path. */
export function useToolSelectionControlPreference(profileId?: string): boolean {
  const subscribe = useCallback(
    (listener: () => void) => subscribeToolSelectionControlPreference(profileId, listener),
    [profileId],
  );
  const snapshot = useCallback(() => readToolSelectionControlPreference(profileId), [profileId]);
  return useSyncExternalStore(subscribe, snapshot, () => false);
}
