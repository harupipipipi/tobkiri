import type { ComposerEntityReference } from "./composerReferences";
import type { ComposerHistoryCandidate } from "./composerEntityCandidates";
import {
  buildHistoryChatReference, buildHistoryGroupReference,
  confirmResolvedHistoryReference, historyReferenceMention,
} from "./historyReferences";

export type ChatReferenceCatalog = {
  references: ComposerEntityReference[];
  candidates: ComposerHistoryCandidate[];
  nextCursor?: string;
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parse authenticated catalog rows through the same confirmation gate as selection. */
export function confirmedChatReferenceCatalog(raw: unknown, profileId: string): ChatReferenceCatalog {
  const empty: ChatReferenceCatalog = { references: [], candidates: [] };
  if (!record(raw) || raw.profile_id !== profileId || !profileId
    || raw.kind !== "tobkiri.chat.reference.snapshot.v1"
    || !Array.isArray(raw.references) || raw.references.length > 100) return empty;
  const allowed = new Set(["kind", "profile_id", "store_revision", "project_revision",
    "snapshot_time", "expires_at", "references", "next_cursor", "truncated"]);
  if (Object.keys(raw).some((key) => !allowed.has(key))) return empty;
  if (raw.next_cursor !== undefined && raw.next_cursor !== null
    && (typeof raw.next_cursor !== "string" || raw.next_cursor.length > 96)) return empty;
  const now = Date.now();
  if (typeof raw.truncated !== "boolean" || raw.next_cursor === undefined
    || !Number.isSafeInteger(raw.store_revision) || !Number.isSafeInteger(raw.project_revision)
    || !Number.isSafeInteger(raw.snapshot_time) || !Number.isSafeInteger(raw.expires_at)
    || (raw.store_revision as number) < 0 || (raw.project_revision as number) < 0
    || (raw.snapshot_time as number) < 0 || (raw.expires_at as number) <= now
    || (raw.expires_at as number) <= (raw.snapshot_time as number)
    || (raw.expires_at as number) - (raw.snapshot_time as number) > 600_000
    || (raw.snapshot_time as number) > now + 60_000) return empty;
  const snapshot = { ...raw, next_cursor: null, truncated: false };
  const seen = new Set<string>();
  const result: ChatReferenceCatalog = { references: [], candidates: [] };
  for (const row of raw.references) {
    if (!record(row) || typeof row.id !== "string" || typeof row.label !== "string") continue;
    const candidate = row.kind === "chat"
      ? buildHistoryChatReference({ id: row.id, title: row.label }, profileId)
      : row.kind === "group"
        ? buildHistoryGroupReference({ id: row.id, title: row.label, chats: [], subGroups: [] }, profileId)
        : null;
    if (!candidate) continue;
    const key = `${candidate.kind}:${candidate.id}`;
    if (seen.has(key)) continue;
    const confirmed = confirmResolvedHistoryReference(candidate, { ...snapshot, references: [row] });
    if (!confirmed) continue;
    seen.add(key);
    const { reference } = historyReferenceMention(confirmed);
    result.references.push(reference);
    result.candidates.push({ kind: reference.kind, id: reference.id, label: reference.label,
      profileId: reference.profileId, syntax: reference.syntax, available: true });
  }
  if (typeof raw.next_cursor === "string" && raw.next_cursor) result.nextCursor = raw.next_cursor;
  return result;
}
