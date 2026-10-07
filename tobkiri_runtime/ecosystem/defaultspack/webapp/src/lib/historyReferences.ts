import type { DroppedWidget } from "../renderers/types";

export const HISTORY_REFERENCE_SCHEMA = "io.tobkiri.history-reference.v1";
export const HISTORY_REFERENCE_DROP_MIME = "application/x-tobkiri-history-reference+json";
export const HISTORY_REFERENCE_DROP_EVENT = "tobkiri:history-reference-drop";
export const HISTORY_REFERENCE_MAX_BYTES = 32_768;
export const HISTORY_REFERENCE_MAX_MEMBERS = 256;
const MAX_LABEL_LENGTH = 256;
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const CONTROL_PATTERN = /[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g;

export type HistoryReferenceDragPayload = {
  schema: typeof HISTORY_REFERENCE_SCHEMA;
  kind: "chat" | "group";
  profile_id: string;
  id: string;
  label: string;
  /** Display snapshot only. Backend resolution replaces these IDs. */
  member_ids?: string[];
};
export type HistoryReferenceDropDetail = {
  rawPayload: string;
  point: { x: number; y: number };
  targetId: string;
};
export type HistoryEntityReference = {
  kind: "chat" | "group";
  id: string;
  syntax: string;
  label: string;
  profileId: string;
  memberIds?: string[];
};
declare const confirmedHistoryReference: unique symbol;
export type ConfirmedHistoryReference = Readonly<HistoryReferenceDragPayload> & {
  readonly [confirmedHistoryReference]: true;
};
const confirmedReferences = new WeakSet<object>();

type HistoryChatSnapshot = { id: string; children?: HistoryChatSnapshot[] };

type HistoryGroupSnapshot = {
  id: string;
  sourceGroupId?: string;
  title: string;
  chats: HistoryChatSnapshot[];
  subGroups: HistoryGroupSnapshot[];
};

function validId(value: unknown): value is string {
  return typeof value === "string" && ID_PATTERN.test(value);
}

function validProfile(value: unknown): value is string {
  return typeof value === "string" && value.length > 0
    && new TextEncoder().encode(value).byteLength <= 128
    && value.trim() === value && !value.includes("/") && !value.includes("\\")
    && !value.includes("..") && !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/.test(value);
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeLabel(value: unknown, fallback: string): string | null {
  if (typeof value !== "string" || Array.from(value).length > MAX_LABEL_LENGTH
      || new TextEncoder().encode(value).byteLength > 1024) return null;
  const label = value.replace(/<[^>]*>/g, "").replace(/[<>]/g, "")
    .replace(CONTROL_PATTERN, "").trim();
  return label || fallback;
}

function members(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > HISTORY_REFERENCE_MAX_MEMBERS) return null;
  if (!value.every(validId)) return null;
  return [...new Set(value as string[])];
}

function normalize(value: unknown, expectedProfile: string, requireSchema: boolean): HistoryReferenceDragPayload | null {
  if (!validProfile(expectedProfile) || !record(value)) return null;
  const fields = new Set(["schema", "kind", "profile_id", "id", "label", "member_ids"]);
  if (Object.keys(value).some((key) => !fields.has(key))) return null;
  if ((requireSchema || "schema" in value) && value.schema !== HISTORY_REFERENCE_SCHEMA) return null;
  if (value.kind !== "chat" && value.kind !== "group") return null;
  if (!validProfile(value.profile_id) || value.profile_id !== expectedProfile || !validId(value.id)) return null;
  const label = safeLabel(value.label, value.id);
  if (label === null) return null;
  if (value.kind === "chat" && "member_ids" in value) return null;
  const memberIds = value.kind === "group" ? members(value.member_ids) : undefined;
  if (memberIds === null) return null;
  const normalized: HistoryReferenceDragPayload = { schema: HISTORY_REFERENCE_SCHEMA, kind: value.kind, profile_id: value.profile_id,
    id: value.id, label, ...(memberIds ? { member_ids: memberIds } : {}) };
  return new TextEncoder().encode(JSON.stringify(normalized)).byteLength <= HISTORY_REFERENCE_MAX_BYTES ? normalized : null;
}

/** Match the backend tag-bucket codec without deriving messaging authority. */
export function canonicalHistoryTagGroupId(tag: string): string {
  const normalized = Array.from(tag.toLowerCase().replace(/\s+/g, "-")).slice(0, 40).join("");
  const binary = Array.from(new TextEncoder().encode(normalized), (byte) => String.fromCharCode(byte)).join("");
  return `group-tag-${btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
}

/** Decode an untrusted display candidate. This grants no messaging authority. */
export function decodeHistoryReferenceDrag(raw: unknown, expectedProfile: string): HistoryReferenceDragPayload | null {
  if (typeof raw !== "string" || raw.length > HISTORY_REFERENCE_MAX_BYTES
      || new TextEncoder().encode(raw).byteLength > HISTORY_REFERENCE_MAX_BYTES) return null;
  try {
    return normalize(JSON.parse(raw), expectedProfile, true);
  } catch {
    return null;
  }
}
export const decodeHistoryReferenceDrop = decodeHistoryReferenceDrag;

/** Build a sidebar display snapshot using the captured active profile. */
export function buildHistoryChatReference(chat: { id: string; title: string }, profileId: string): HistoryReferenceDragPayload | null {
  return normalize({ schema: HISTORY_REFERENCE_SCHEMA, kind: "chat", profile_id: profileId,
    id: chat.id, label: chat.title }, profileId, true);
}

/** Include every recursive member exactly once; fail rather than truncate. */
export function buildHistoryGroupReference(group: HistoryGroupSnapshot, profileId: string): HistoryReferenceDragPayload | null {
  type Node = HistoryGroupSnapshot | HistoryChatSnapshot;
  const pending: { node: Node; group: boolean; exit?: boolean }[] = [{ node: group, group: true }];
  const active = new Set<Node>();
  const visited = new Set<Node>();
  const ids = new Set<string>();
  while (pending.length) {
    const entry = pending.pop()!;
    const current = entry.node;
    if (entry.exit) {
      active.delete(current);
      continue;
    }
    if (active.has(current)) return null;
    if (visited.has(current)) continue;
    if (!record(current)) return null;
    visited.add(current);
    active.add(current);
    if (visited.size > 4096) return null;
    pending.push({ ...entry, exit: true });
    if (entry.group) {
      const currentGroup = current as HistoryGroupSnapshot;
      if (!Array.isArray(currentGroup.chats) || !Array.isArray(currentGroup.subGroups)
          || currentGroup.chats.length > 4096 || currentGroup.subGroups.length > 1024) return null;
      for (let index = currentGroup.subGroups.length - 1; index >= 0; index--) {
        pending.push({ node: currentGroup.subGroups[index], group: true });
      }
      for (let index = currentGroup.chats.length - 1; index >= 0; index--) {
        pending.push({ node: currentGroup.chats[index], group: false });
      }
    } else {
      const currentChat = current as HistoryChatSnapshot;
      if (!validId(currentChat.id)) return null;
      ids.add(currentChat.id);
      if (ids.size > HISTORY_REFERENCE_MAX_MEMBERS) return null;
      if (currentChat.children !== undefined) {
        if (!Array.isArray(currentChat.children) || currentChat.children.length > 4096) return null;
        for (let index = currentChat.children.length - 1; index >= 0; index--) {
          pending.push({ node: currentChat.children[index], group: false });
        }
      }
    }
    if (pending.length > 8192) return null;
  }
  return normalize({ schema: HISTORY_REFERENCE_SCHEMA, kind: "group", profile_id: profileId,
    id: group.sourceGroupId ?? group.id, label: group.title, member_ids: [...ids] }, profileId, true);
}

/** Confirm one candidate against the whole authenticated resolver response.
 * The backend revalidates kind/Profile/ID again when receiving chat_references.
 * Renderer flags and dragged member snapshots never grant messaging authority.
 */
export function confirmResolvedHistoryReference(candidate: HistoryReferenceDragPayload, resolved: unknown, nowMs = Date.now()): ConfirmedHistoryReference | null {
  if (!record(candidate) || !validProfile(candidate.profile_id)) return null;
  const request = normalize(candidate, candidate.profile_id, true);
  if (!request || !record(resolved)) return null;
  const envelopeFields = new Set(["kind", "profile_id", "store_revision", "project_revision", "snapshot_time", "expires_at", "references", "next_cursor", "truncated"]);
  if (Object.keys(resolved).some((key) => !envelopeFields.has(key))
      || resolved.kind !== "tobkiri.chat.reference.snapshot.v1"
      || resolved.profile_id !== request.profile_id
      || resolved.next_cursor !== null || resolved.truncated !== false
      || !validRevision(resolved.store_revision) || !validRevision(resolved.project_revision)
      || !validRevision(nowMs) || !validRevision(resolved.snapshot_time) || !validRevision(resolved.expires_at)
      || resolved.expires_at <= resolved.snapshot_time || resolved.expires_at <= nowMs
      || resolved.expires_at - resolved.snapshot_time > 600_000
      || resolved.snapshot_time > nowMs + 60_000
      || !Array.isArray(resolved.references) || resolved.references.length !== 1) return null;
  const item = resolved.references[0];
  const itemFields = new Set(["kind", "id", "label", "conversation_ids", "snapshot_digest", "member_count", "membership_complete"]);
  if (!record(item) || Object.keys(item).some((key) => !itemFields.has(key))
      || item.kind !== request.kind || item.id !== request.id
      || typeof item.snapshot_digest !== "string" || !/^sha256:[0-9a-f]{64}$/.test(item.snapshot_digest)) return null;
  const ids = members(item.conversation_ids);
  const label = safeLabel(item.label, request.id);
  if (ids === null || label === null
      || !validRevision(item.member_count) || item.member_count > 4096
      || item.member_count !== ids.length || item.membership_complete !== true
      || ids.length !== (item.conversation_ids as unknown[]).length
      || ids.some((id, index) => index > 0 && ids[index - 1] >= id)
      || (request.kind === "chat" && (ids.length !== 1 || ids[0] !== request.id))) return null;
  const result: HistoryReferenceDragPayload = {
    schema: HISTORY_REFERENCE_SCHEMA, kind: request.kind, id: request.id,
    profile_id: request.profile_id, label,
    ...(request.kind === "group" ? { member_ids: ids } : {}),
  };
  if (result.member_ids) Object.freeze(result.member_ids);
  Object.freeze(result);
  confirmedReferences.add(result);
  return result as ConfirmedHistoryReference;
}

function validRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function historyEntityReference(confirmed: ConfirmedHistoryReference): HistoryEntityReference {
  if (!confirmedReferences.has(confirmed)) throw new Error("History reference is unresolved");
  return { kind: confirmed.kind, id: confirmed.id, syntax: `@${confirmed.kind}:${confirmed.id}`, label: confirmed.label,
    profileId: confirmed.profile_id, memberIds: confirmed.kind === "group" ? [...(confirmed.member_ids ?? [])] : [confirmed.id] };
}

export function droppedWidgetFromHistoryReference(confirmed: ConfirmedHistoryReference): DroppedWidget {
  const reference = historyEntityReference(confirmed);
  return {
    id: `${confirmed.kind}:${confirmed.profile_id}:${confirmed.id}`,
    type: confirmed.kind === "chat" ? "conversation" : "group",
    widgetKind: "history_context", sourceItemId: confirmed.id,
    label: confirmed.label, enabled: true,
    metadata: { source: "composer_at_mention", history_reference: { schema: HISTORY_REFERENCE_SCHEMA, kind: confirmed.kind,
      profile_id: confirmed.profile_id, id: confirmed.id, label: confirmed.label,
      ...(confirmed.kind === "group" ? { member_ids: [...(confirmed.member_ids ?? [])] } : {}) },
    mention: reference },
  };
}

export function historyReferenceMention(confirmed: ConfirmedHistoryReference): {
  widget: DroppedWidget; reference: HistoryEntityReference; syntax: string;
} {
  const reference = historyEntityReference(confirmed);
  return { widget: droppedWidgetFromHistoryReference(confirmed), reference, syntax: reference.syntax };
}
