import type { ComposerMentionMetadata } from "./composerWidgets";

const STABLE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;

/** Project persisted server rows into read-only mention display, never draft authority. */
export function savedChatReferenceMentions(value: unknown): ComposerMentionMetadata[] {
  if (!Array.isArray(value) || value.length > 16) return [];
  const mentions: ComposerMentionMetadata[] = [];
  const seen = new Set<string>();
  for (const candidate of value) {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) continue;
    const row = candidate as Record<string, unknown>;
    if ((row.kind !== "chat" && row.kind !== "group")
      || typeof row.id !== "string" || !STABLE_ID.test(row.id)
      || typeof row.label !== "string" || !row.label.trim() || row.label.length > 256 || new TextEncoder().encode(row.label).length > 1024
      || /[\u0000-\u001f\u007f]/u.test(row.label)
      || typeof row.snapshot_digest !== "string" || !DIGEST.test(row.snapshot_digest)
      || row.membership_complete !== true || !Array.isArray(row.conversation_ids)
      || row.conversation_ids.length > 256
      || row.conversation_ids.some((id) => typeof id !== "string" || !STABLE_ID.test(id))
      || new Set(row.conversation_ids).size !== row.conversation_ids.length
      || !Number.isInteger(row.member_count) || row.member_count !== row.conversation_ids.length
      || (row.kind === "chat" && (row.member_count !== 1 || row.conversation_ids[0] !== row.id))) continue;
    const key = JSON.stringify([row.kind, row.id]);
    if (seen.has(key)) continue;
    seen.add(key);
    mentions.push({ kind: row.kind, id: row.id, label: row.label,
      syntax: `@${row.kind}:${row.id}`, memberIds: [...row.conversation_ids] as string[] });
  }
  return mentions;
}
