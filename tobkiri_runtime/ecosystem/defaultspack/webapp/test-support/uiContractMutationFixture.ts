import { isDeepStrictEqual } from "node:util";
import type { SavedTurn, SavedTurnEventSnapshot } from "../src/lib/api";

/** Deliberately finite write contracts for the UI smoke fixture, not a backend emulator. */
type RecordValue = Record<string, unknown>;
const record = (value: unknown): value is RecordValue => value !== null && typeof value === "object" && !Array.isArray(value);
const keys = (value: RecordValue, allowed: string[]) => Object.keys(value).every((key) => allowed.includes(key));
export class FixtureMutationError extends Error {
  constructor(readonly status: 400 | 409, message: string) { super(message); }
}
const malformed = (): never => { throw new FixtureMutationError(400, "Unsupported or malformed fixture mutation"); };

export function settingsFixtureMutation(payload: unknown, revision: number) {
  if (!record(payload) || !keys(payload, ["changes", "expected_revision"])
    || !Number.isSafeInteger(payload.expected_revision) || Number(payload.expected_revision) < 0 || !record(payload.changes)) return malformed();
  const changes = payload.changes;
  if (Object.keys(changes).length !== 1) return malformed();
  let values: Record<string, Record<string, unknown>>;
  if (record(changes.general) && Object.keys(changes.general).length === 1
    && keys(changes.general, ["manual_runtime_mode_selection"])
    && typeof changes.general.manual_runtime_mode_selection === "boolean") {
    values = { general: { manual_runtime_mode_selection: changes.general.manual_runtime_mode_selection } };
  } else if (record(changes.tools) && Object.keys(changes.tools).length === 1
    && keys(changes.tools, ["action_approval_mode"]) && changes.tools.action_approval_mode === "ask") {
    // This scenario only downgrades to human approval; never fixture-admit elevation.
    values = { tools: { action_approval_mode: "ask" } };
  } else return malformed();
  if (payload.expected_revision !== revision) throw new FixtureMutationError(409, "Settings revision conflict");
  return { values, document_revision: revision + 1 };
}

export function conversationFixtureMutation(payload: unknown, revision: number, currentMetadata: RecordValue = {}) {
  if (!record(payload) || !keys(payload, ["conversation_id", "updates", "expected_conversation_revision"])
    || payload.conversation_id !== "c-smoke" || !Number.isSafeInteger(payload.expected_conversation_revision)
    || Number(payload.expected_conversation_revision) < 1
    || !record(payload.updates) || Object.keys(payload.updates).length !== 1 || !record(payload.updates.metadata)
    || !record(payload.updates.metadata.tool_preferences)) return malformed();
  // Production replaces metadata; the client carries unrelated fields forward.
  // This finite fixture permits those exact existing fields, never new edits.
  const metadata = payload.updates.metadata;
  const existingKeys = Object.keys(currentMetadata).filter((key) => key !== "tool_preferences");
  if (!keys(metadata, [...existingKeys, "tool_preferences"])
    || existingKeys.some((key) => !(key in metadata) || !isDeepStrictEqual(metadata[key], currentMetadata[key]))) return malformed();
  const preferences = payload.updates.metadata.tool_preferences;
  const fields = ["mode", "include", "exclude", "scope", "strategy", "must_use", "review", "preview_id"];
  const targets = (value: unknown) => Array.isArray(value) && value.length <= 1 && value.every((target) => record(target)
    && Object.keys(target).length === 2 && keys(target, ["kind", "id"]) && target.kind === "service" && target.id === "github");
  if (Object.keys(preferences).length !== fields.length || !keys(preferences, fields)
    || typeof preferences.mode !== "string" || !["auto", "manual"].includes(preferences.mode) || preferences.scope !== "conversation"
    || !targets(preferences.include) || !targets(preferences.exclude) || preferences.strategy !== null
    || typeof preferences.must_use !== "boolean" || typeof preferences.review !== "boolean" || preferences.preview_id !== null) return malformed();
  if (payload.expected_conversation_revision !== revision) throw new FixtureMutationError(409, "Conversation revision conflict");
  return { metadata: { ...structuredClone(metadata), tool_preferences: structuredClone(preferences) }, conversation_revision: revision + 1 };
}

/** One persisted revision shared by metadata writes and the smoke saved-turn receipt. */
export function createConversationFixtureState(initialRevision: number, initialMetadata: RecordValue = {}) {
  if (!Number.isSafeInteger(initialRevision) || initialRevision < 1) return malformed();
  let revision = initialRevision;
  let metadata = structuredClone(initialMetadata);
  const snapshot = () => ({ metadata: structuredClone(metadata), conversation_revision: revision });
  return {
    snapshot,
    mutate(payload: unknown) {
      const receipt = conversationFixtureMutation(payload, revision, metadata);
      revision = receipt.conversation_revision;
      metadata = receipt.metadata;
      return snapshot();
    },
    completeTurn(conversationId: unknown, expectedRevision: unknown) {
      if (conversationId !== "c-smoke" || !Number.isSafeInteger(expectedRevision) || Number(expectedRevision) < 1) return malformed();
      if (expectedRevision !== revision) throw new FixtureMutationError(409, "Conversation revision conflict");
      revision += 1;
      return snapshot();
    },
  };
}

/** The finite smoke conversation lists the same owner receipt returned by start. */
export function completedSavedTurnFixtureList(query: URLSearchParams, turn: SavedTurn | null): { turns: SavedTurn[] } {
  if (Array.from(query).length !== 1 || query.get("conversation_id") !== "c-smoke"
    || (turn && (turn.conversation_id !== "c-smoke" || turn.status !== "completed"))) return malformed();
  return { turns: turn ? [structuredClone(turn)] : [] };
}

/** Project only the completed owner already committed by this finite smoke fixture. */
export function completedSavedTurnFixtureEvents(query: URLSearchParams, turn: SavedTurn | null): SavedTurnEventSnapshot {
  const stableId = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
  if (Array.from(query).length !== 2 || query.getAll("turn_id").length !== 1
    || query.getAll("conversation_id").length !== 1 || query.get("conversation_id") !== "c-smoke"
    || !stableId(query.get("turn_id"))) return malformed();
  if (!turn || turn.id !== query.get("turn_id") || turn.conversation_id !== query.get("conversation_id")
    || turn.status !== "completed" || !Number.isSafeInteger(turn.revision) || turn.revision < 1
    || !stableId(turn.request_id) || !Array.isArray(turn.events) || turn.events.length !== 0
    || turn.guidance_parent_turn_id !== undefined || turn.guidance_source_turn_id !== undefined || turn.guidance_id !== undefined
    || turn.result_reference?.conversation_id !== turn.conversation_id
    || !Number.isSafeInteger(turn.result_reference.conversation_revision) || turn.result_reference.conversation_revision < 1
    || !stableId(turn.result_reference.user_message_id) || !stableId(turn.result_reference.assistant_message_id)
    || !/^sha256:[a-f0-9]{64}$/.test(turn.result_reference.outcome_digest)) {
    throw new FixtureMutationError(409, "Completed saved-turn fixture owner is unavailable or does not match");
  }
  const owner = structuredClone(turn);
  const identity = {
    turn_id: owner.id, conversation_id: owner.conversation_id, operation_id: owner.id,
    request_id: owner.request_id!, turn_revision: owner.revision,
  };
  return {
    ...identity, status: owner.status, turn: owner, events: [],
    terminal: { ...identity, status: owner.status, result_reference: structuredClone(owner.result_reference), error: null },
  };
}
