export type AdaptiveDurableDraft<T> = {
  baseRevision: number;
  requestId?: string | null;
  resourceId: string;
  updatedAt: string;
  value: T;
};

export function adaptiveDraftKey(kind: "onboarding" | "operating-profile", resourceId: string): string {
  return `tobkiri:adaptive:${kind}:${resourceId}`;
}

export function loadAdaptiveDraft<T>(key: string): AdaptiveDurableDraft<T> | null {
  try {
    const storage = globalThis.localStorage;
    if (!storage) return null;
    const raw = storage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<AdaptiveDurableDraft<T>>;
    if (!parsed || typeof parsed !== "object" || typeof parsed.resourceId !== "string" || !("value" in parsed)) {
      return null;
    }
    const baseRevision = Number(parsed.baseRevision);
    return {
      baseRevision: Number.isSafeInteger(baseRevision) && baseRevision >= 0 ? baseRevision : 0,
      requestId: typeof parsed.requestId === "string" ? parsed.requestId : null,
      resourceId: parsed.resourceId,
      updatedAt: typeof parsed.updatedAt === "string" ? parsed.updatedAt : "",
      value: parsed.value as T,
    };
  } catch {
    return null;
  }
}

export function saveAdaptiveDraft<T>(key: string, draft: AdaptiveDurableDraft<T>): boolean {
  try {
    const storage = globalThis.localStorage;
    if (!storage) return false;
    storage.setItem(key, JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

export function clearAdaptiveDraft(key: string): void {
  try {
    globalThis.localStorage?.removeItem(key);
  } catch {
    // Storage denial is represented by the draft remaining visibly unsaved.
  }
}
