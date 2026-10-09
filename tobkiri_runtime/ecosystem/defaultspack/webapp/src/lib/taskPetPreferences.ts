import { useCallback, useSyncExternalStore } from "react";
import { loadTaskPetPreference, saveTaskPetPreference, taskPetPreferenceKey } from "./taskPet";

type PreferenceStorage = Pick<Storage, "getItem" | "setItem">;
const listeners = new Map<string, Set<() => void>>();

/** Access profile preferences without making storage availability a prerequisite. */
export function taskPetStorage(): Storage | null {
  try { return typeof window === "undefined" ? null : window.localStorage; } catch { return null; }
}

/** Persist the existing notification key and publish successful same-tab changes. */
export function saveTaskPetNotifications(profileId: string, enabled: boolean, storage: PreferenceStorage | null = taskPetStorage()): boolean {
  if (!saveTaskPetPreference(storage, profileId, "notifications", enabled)) return false;
  listeners.get(profileId)?.forEach((listener) => listener());
  return true;
}

/** Subscribe to profile-local writes and browser storage changes. */
export function subscribeTaskPetNotifications(profileId: string, listener: () => void): () => void {
  const profileListeners = listeners.get(profileId) ?? new Set<() => void>();
  listeners.set(profileId, profileListeners);
  profileListeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === taskPetPreferenceKey(profileId, "notifications")) listener();
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    profileListeners.delete(listener);
    if (!profileListeners.size) listeners.delete(profileId);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

/** Share persisted notification opt-in across settings and the mounted publisher. */
export function useTaskPetNotifications(profileId: string): boolean {
  const subscribe = useCallback((listener: () => void) => subscribeTaskPetNotifications(profileId, listener), [profileId]);
  const snapshot = useCallback(() => loadTaskPetPreference(taskPetStorage(), profileId, "notifications"), [profileId]);
  return useSyncExternalStore(subscribe, snapshot, () => false);
}

export type NotificationEnableResult = "enabled" | "denied" | "default" | "stale" | "storage-error" | "request-error";

/** Request permission only for an explicit enable operation, fencing stale callers. */
export async function enableTaskPetNotifications(options: {
  profileId: string;
  requestPermission: () => Promise<NotificationPermission>;
  isCurrent: () => boolean;
  storage?: PreferenceStorage | null;
}): Promise<NotificationEnableResult> {
  if (!options.isCurrent()) return "stale";
  try {
    const permission = await options.requestPermission();
    if (!options.isCurrent()) return "stale";
    if (permission !== "granted") return permission;
    return saveTaskPetNotifications(options.profileId, true, options.storage) ? "enabled" : "storage-error";
  } catch {
    return options.isCurrent() ? "request-error" : "stale";
  }
}
