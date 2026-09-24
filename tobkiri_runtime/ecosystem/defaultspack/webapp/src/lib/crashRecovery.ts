export const CRASH_LOOP_KEY = "rumi-crash-boundary-v1";
export const RECOVERABLE_DRAFT_KEYS = ["rumi-input"] as const;
export const RESETTABLE_CLIENT_KEYS = [
  "rumi-workspace-tabs",
  "rumi-active-workspace-tab",
  "rumi-ui-placements",
  "rumi-defaultspack-local-auth",
] as const;
const CRASH_WINDOW_MS = 60_000;

export type CrashDraftSnapshot = { capturedAt: string; drafts: Record<string, string> };

function storageValue(storage: Storage | null | undefined, key: string): string {
  try { return storage?.getItem(key) ?? ""; } catch { return ""; }
}

export function recoverableDraftSnapshot(storage: Storage | null | undefined): CrashDraftSnapshot | null {
  const drafts: Record<string, string> = {};
  for (const key of RECOVERABLE_DRAFT_KEYS) {
    const value = storageValue(storage, key);
    if (value.trim()) drafts[key] = value.slice(0, 200_000);
  }
  return Object.keys(drafts).length ? { capturedAt: new Date().toISOString(), drafts } : null;
}

export function crashDraftExport(snapshot: CrashDraftSnapshot): string {
  return JSON.stringify({ schema: "rumi.crash_drafts.v1", ...snapshot }, null, 2);
}

export function recordCrash(storage: Storage | null | undefined, now = Date.now()): number {
  let prior: number[] = [];
  try {
    const parsed = JSON.parse(storageValue(storage, CRASH_LOOP_KEY));
    if (Array.isArray(parsed)) prior = parsed.filter((value): value is number => typeof value === "number");
  } catch { prior = []; }
  const recent = [...prior.filter((value) => now - value < CRASH_WINDOW_MS), now].slice(-5);
  try { storage?.setItem(CRASH_LOOP_KEY, JSON.stringify(recent)); } catch { /* unavailable storage stays fail-safe */ }
  return recent.length;
}

export function resetAffectedClientState(storage: Storage | null | undefined): void {
  for (const key of RESETTABLE_CLIENT_KEYS) {
    try { storage?.removeItem(key); } catch { /* preserve recovery UI */ }
  }
}
