import { createHash } from "node:crypto";

export class HistoryReferenceFixtureError extends Error {
  constructor(readonly status: 400 | 404, message: string) { super(message); }
}

const PROFILE_ID = "defaults";
const OWNER_ID = "c-smoke";
const OWNER_LABEL = "Preview Calendar Chat";
const SNAPSHOT_TTL_MS = 600_000;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

// chat_reference.py hashes UTF-8 JSON with sorted keys and compact separators.
// These fixed keys are already sorted; the digest is not a Host signature.
const snapshotDigest = `sha256:${createHash("sha256").update(JSON.stringify({
  conversation_ids: [OWNER_ID],
  id: OWNER_ID,
  kind: "chat",
  profile_id: PROFILE_ID,
  version: "tobkiri.chat-reference/v1",
}), "utf8").digest("hex")}`;

const malformed = (): never => {
  throw new HistoryReferenceFixtureError(400, "Unsupported or malformed history reference fixture request");
};
const record = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === "object" && !Array.isArray(value)
);

/** Model one captured Host owner, not authentication or cryptographic signing.
 * Only the smoke chat is exposed; unrelated catalog groups are outside this
 * fixture. Profile and revisions match the existing conversation/Project reads.
 */
export function createHistoryReferenceFixture(clock: () => number = Date.now) {
  const snapshot = () => {
    const now = clock();
    if (!Number.isSafeInteger(now) || now < 0 || now > Number.MAX_SAFE_INTEGER - SNAPSHOT_TTL_MS) return malformed();
    return {
      kind: "tobkiri.chat.reference.snapshot.v1",
      profile_id: PROFILE_ID,
      store_revision: 1,
      project_revision: 0,
      snapshot_time: now,
      expires_at: now + SNAPSHOT_TTL_MS,
      references: [{
        kind: "chat", id: OWNER_ID, label: OWNER_LABEL,
        conversation_ids: [OWNER_ID], snapshot_digest: snapshotDigest,
        member_count: 1, membership_complete: true,
      }],
      next_cursor: null,
      truncated: false,
    };
  };

  return {
    list(query: URLSearchParams) {
      if (!(query instanceof URLSearchParams)
        || [...query.keys()].some((key) => key !== "limit" && key !== "cursor")
        || query.getAll("limit").length > 1 || query.getAll("cursor").length > 1) return malformed();
      const limit = query.get("limit");
      if (limit !== null && (!/^[1-9][0-9]{0,2}$/.test(limit) || Number(limit) > 100)) return malformed();
      // One finite row never issues a next_cursor; no supplied cursor is valid.
      if (query.has("cursor")) return malformed();
      return snapshot();
    },
    resolve(payload: unknown) {
      if (!record(payload) || Object.keys(payload).length !== 1 || !("references" in payload)
        || !Array.isArray(payload.references) || payload.references.length < 1
        || payload.references.length > 16) return malformed();
      const seen = new Set<string>();
      for (const reference of payload.references) {
        if (!record(reference) || Object.keys(reference).length !== 2
          || !Object.prototype.hasOwnProperty.call(reference, "kind") || !Object.prototype.hasOwnProperty.call(reference, "id")
          || (reference.kind !== "chat" && reference.kind !== "group")
          || typeof reference.id !== "string" || !ID_PATTERN.test(reference.id)) return malformed();
        const identity = `${reference.kind}:${reference.id}`;
        if (seen.has(identity)) return malformed();
        seen.add(identity);
      }
      if (payload.references.some((reference) => reference.kind !== "chat" || reference.id !== OWNER_ID)) {
        throw new HistoryReferenceFixtureError(404, "History reference fixture owner is unavailable");
      }
      return snapshot();
    },
  };
}
