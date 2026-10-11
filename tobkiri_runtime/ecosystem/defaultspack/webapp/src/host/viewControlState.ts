/** A draft remembers its original value until explicit reload or confirmed save. */
export type TextControlDraft = { value: string; baseline: string; awaiting: string | null };

export const savedTextValue = (value: unknown): string => typeof value === "string" ? value : "";
export const freshTextDraft = (value: unknown): TextControlDraft => ({
  value: savedTextValue(value), baseline: savedTextValue(value), awaiting: null,
});
export const textDraftDirty = (draft: TextControlDraft): boolean => draft.value !== draft.baseline;

/** Remote changes cannot replace unsaved or rejected edits. */
export function refreshTextDraft(draft: TextControlDraft, authoritative: unknown): TextControlDraft {
  const value = savedTextValue(authoritative);
  if (draft.awaiting === value && draft.value === draft.awaiting) return freshTextDraft(value);
  if (!textDraftDirty(draft)) return draft.baseline === value ? draft : freshTextDraft(value);
  return draft;
}

/** Returned failure/approval states never count as a confirmed mutation. */
export function viewOperationOutcome(value: unknown): "approval" | "failed" | "returned" {
  const result = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const states = [String(result.state ?? ""), String(result.status ?? "")];
  if (states.some((state) => ["approval_required", "awaiting_approval", "pending_approval"].includes(state))) return "approval";
  if (states.some((state) => ["failed", "error", "denied", "unavailable", "conflict", "rejected"].includes(state))) return "failed";
  return "returned";
}
