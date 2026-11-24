import { searchCaptureScope, type SearchCaptureIdentity } from "../search/searchCaptureScope";

/** Pins contain opaque identities only; their array order is insertion order. */
export type ModelPinState = readonly string[];
type PinStorage = Pick<Storage, "getItem" | "setItem">;
const EMPTY: ModelPinState = Object.freeze([]);
const MAX_PINS = 500;
const MAX_COMPONENT = 512;
const MAX_RAW = 1024 * 1024;
const PREFIX = "tobkiri:model-pins:";

/** Reject metadata and malformed or oversized identity tuples. */
export function isModelPinIdentity(identity: unknown): identity is string {
  if (typeof identity !== "string" || identity.length > MAX_COMPONENT * 3 + 64) return false;
  try {
    const tuple: unknown = JSON.parse(identity);
    return Array.isArray(tuple) && tuple.length === 3
      && (tuple[0] === "connection" || tuple[0] === "catalog")
      && tuple.slice(1).every((part) => typeof part === "string"
        && part.trim().length > 0 && part.length <= MAX_COMPONENT
        && !/[\u0000-\u001f\u007f]/u.test(part))
      && JSON.stringify(tuple) === identity;
  } catch { return false; }
}

/** Namespace local pin preferences by the verified profile identity. */
export function modelPinStorageKey(profileId: string): string {
  return PREFIX + encodeURIComponent(profileId);
}

function decode(raw: string | null): { pins: ModelPinState; migrate: boolean } {
  if (!raw || raw.length > MAX_RAW) return { pins: EMPTY, migrate: false };
  try {
    const parsed: unknown = JSON.parse(raw);
    const migrate = Array.isArray(parsed);
    const pins: unknown = migrate ? parsed : parsed && typeof parsed === "object"
      && "version" in parsed && parsed.version === 1 && "pins" in parsed ? parsed.pins : null;
    if (!Array.isArray(pins) || pins.length > MAX_PINS
      || !pins.every(isModelPinIdentity)) return { pins: EMPTY, migrate: false };
    return { pins: Object.freeze([...new Set(pins)]), migrate };
  } catch { return { pins: EMPTY, migrate: false }; }
}

/** A profile-scoped store with persistence and same-tab notifications. */
export function createModelPinStore(storage?: PinStorage) {
  const snapshots = new Map<string, ModelPinState>();
  const listeners = new Map<string, Set<() => void>>();
  const dirty = new Set<string>();
  const persist = (profileId: string, pins: ModelPinState): void => {
    try {
      storage?.setItem(modelPinStorageKey(profileId), JSON.stringify({ version: 1, pins }));
      dirty.delete(profileId);
    } catch {
      dirty.add(profileId);
      // Retain unsaved local intent during later profile activation reloads.
    }
  };
  const read = (profileId: string): ModelPinState => {
    if (!storage || dirty.has(profileId)) return snapshots.get(profileId) ?? EMPTY;
    let raw: string | null = null;
    try { raw = storage?.getItem(modelPinStorageKey(profileId)) ?? null; }
    catch { return snapshots.get(profileId) ?? EMPTY; }
    const result = decode(raw);
    if (result.migrate) persist(profileId, result.pins);
    return result.pins;
  };
  const publish = (profileId: string, pins: ModelPinState): void => {
    const old = snapshots.get(profileId);
    if (old && old.length === pins.length && old.every((pin, index) => pin === pins[index])) return;
    snapshots.set(profileId, pins);
    for (const listener of listeners.get(profileId) ?? []) listener();
  };
  const getSnapshot = (profileId: string): ModelPinState => {
    if (!profileId.trim()) return EMPTY;
    if (!snapshots.has(profileId)) snapshots.set(profileId, read(profileId));
    return snapshots.get(profileId)!;
  };
  return {
    getSnapshot,
    subscribe(profileId: string, listener: () => void): () => void {
      if (!profileId.trim()) return () => {};
      const group = listeners.get(profileId) ?? new Set<() => void>();
      group.add(listener);
      listeners.set(profileId, group);
      return () => { group.delete(listener); if (!group.size) listeners.delete(profileId); };
    },
    toggle(profileId: string, identity: string): void {
      if (!profileId.trim() || !isModelPinIdentity(identity)) return;
      const old = getSnapshot(profileId);
      if (!old.includes(identity) && old.length >= MAX_PINS) return;
      const next = Object.freeze(old.includes(identity)
        ? old.filter((pin) => pin !== identity) : [...old, identity]);
      persist(profileId, next);
      publish(profileId, next);
    },
    storageChanged(profileId: string, raw?: string | null): void {
      if (!profileId.trim()) return;
      publish(profileId, raw === undefined ? read(profileId) : decode(raw).pins);
    },
  };
}

/** Reorder loaded rows only, preserving pinned insertion order and other row order. */
export function orderPinnedModels<T>(
  items: readonly T[], pins: ModelPinState, identity: (item: T) => string | null,
): T[] {
  const ranks = new Map(pins.map((pin, index) => [pin, index]));
  return items.map((item, index) => ({ item, index, rank: ranks.get(identity(item) ?? "") }))
    .sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity) || a.index - b.index)
    .map(({ item }) => item);
}

/** Resolve an operation fence without putting capture revisions into persistence keys. */
export function resolveModelPinScope(
  capture: SearchCaptureIdentity | null | undefined,
  activePlanHash: string | undefined,
): { profileId: string; fence: string; enabled: boolean } {
  const enabled = Boolean(capture && capture.profile_id.trim()
    && activePlanHash?.trim() && activePlanHash === capture.plan_hash
    && capture.profile_revision.trim() && capture.activation_id.trim()
    && capture.catalog_hash.trim());
  if (!enabled || !capture) return { profileId: "", fence: "", enabled: false };
  return { profileId: capture.profile_id,
    fence: searchCaptureScope(capture.profile_id, capture, activePlanHash), enabled: true };
}

/** An effect lease fences operations after cleanup, including Strict Mode replay. */
export function createModelPinOperationGuard() {
  let activeFence = "";
  let generation = 0;
  return {
    activate(fence: string): () => void {
      activeFence = fence;
      const lease = ++generation;
      return () => { if (lease === generation) activeFence = ""; };
    },
    isCurrent(fence: string): boolean {
      return fence.length > 0 && activeFence === fence;
    },
  };
}
