import {
  readViewPath, requestContextInput, validPublicViewInput, validViewPath,
  type RecordEditorAction, type RecordEditorDefinition, type RecordEditorField,
  type RecordEditorRequest, type ViewInputContext,
} from "./catalogViewRegistry";
export type RecordEditorDescriptor = RecordEditorDefinition;
export type RecordEditorOperation = RecordEditorRequest;
export type { RecordEditorAction, RecordEditorField } from "./catalogViewRegistry";
export type RecordEditorDraft = {
  id: string; record: Record<string, unknown>; original: Record<string, string>;
  values: Record<string, string>;
  source: unknown;
};
export type RecordEditorNavigationGuard = () => boolean;
export type RecordEditorGuardRegistration = (
  ownerId: string, guard: RecordEditorNavigationGuard | null,
) => void;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** Resolve at most 256 uniquely identified own-property records or fail closed. */
export function recordEditorRecords(
  snapshot: unknown, descriptor: RecordEditorDescriptor,
): Record<string, unknown>[] | null {
  const values = readViewPath(snapshot, descriptor.records_path);
  if (!Array.isArray(values) || values.length > 256) return null;
  const ids = new Set<string>();
  for (const item of values) {
    const id = readViewPath(item, descriptor.id_path);
    if (!isRecord(item) || typeof id !== "string" || !id || id.length > 256 || ids.has(id)) return null;
    ids.add(id);
  }
  return values as Record<string, unknown>[];
}

/** Search data locally; query text never becomes code or a provider argument. */
export function filterEditorRecords(
  records: Record<string, unknown>[], descriptor: RecordEditorDescriptor, query: string,
): Record<string, unknown>[] {
  const normalized = query.slice(0, 256).trim().toLocaleLowerCase();
  if (!normalized) return records;
  return records.filter((record) => (descriptor.search_paths ?? []).some((path) => {
    const value = readViewPath(record, path);
    return typeof value === "string" && value.toLocaleLowerCase().includes(normalized);
  }));
}

export function editorDisplayValue(value: unknown, kind = "text"): string {
  if (kind === "datetime" && typeof value === "number" && Number.isFinite(value) && value >= 0) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unavailable" : date.toLocaleString();
  }
  if (typeof value === "string") return value.slice(0, 16384);
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return "Unavailable";
}

function fieldDraft(field: RecordEditorField, record: Record<string, unknown>): string {
  const value = readViewPath(record, field.path);
  if (field.kind === "json") return JSON.stringify(value ?? {}, null, 2);
  if (field.kind === "datetime" && typeof value === "number") {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Date(value - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  }
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

export function beginRecordDraft(
  record: Record<string, unknown>, descriptor: RecordEditorDescriptor, snapshot?: unknown,
): RecordEditorDraft {
  const original = Object.fromEntries(descriptor.fields.map((field) =>
    [field.id, fieldDraft(field, record)]));
  return { id: String(readViewPath(record, descriptor.id_path)), record: clone(record),
    original, values: { ...original }, source: snapshot === undefined ? undefined : clone(snapshot) };
}

export function recordDraftDirty(draft: RecordEditorDraft | null): boolean {
  return draft !== null && Object.keys(draft.original).some((key) => draft.original[key] !== draft.values[key]);
}

export function recordActionAvailable(action: RecordEditorAction, record: unknown): boolean {
  if (action.available_when && readViewPath(record, action.available_when.path) !== action.available_when.equals) return false;
  if (action.disabled_when && readViewPath(record, action.disabled_when.path) === action.disabled_when.equals) return false;
  return true;
}

/** All native bindings must resolve; a partial payload must never be dispatched. */
export function recordOperationPayload(
  operation: RecordEditorOperation, snapshot: unknown, record: unknown,
  context: ViewInputContext = {},
): Record<string, unknown> | null {
  const payload = requestContextInput(operation, context);
  if (!payload) return null;
  for (const [bindings, source] of [
    [operation.source_bindings, snapshot], [operation.record_bindings, record],
  ] as const) {
    for (const [key, path] of Object.entries(bindings ?? {})) {
      if (Object.prototype.hasOwnProperty.call(payload, key) || !validPublicViewInput({ [key]: null })) return null;
      const value = readViewPath(source, path);
      if (value === undefined) return null;
      payload[key] = value;
    }
  }
  return validPublicViewInput(payload) ? payload : null;
}

function parsedField(field: RecordEditorField, value: string): unknown {
  if (value.length > 16384 || (field.required && !value.trim())) throw new Error("A required value is missing or too long.");
  if (field.kind === "integer") {
    if (!/^-?\d+$/.test(value)) throw new Error("Enter a whole number.");
    const number = Number(value);
    if (!Number.isSafeInteger(number) || (field.min !== undefined && number < field.min)
      || (field.max !== undefined && number > field.max)) throw new Error("The number is outside the allowed range.");
    return number;
  }
  if (field.kind === "datetime") {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp) || timestamp < 0) throw new Error("Enter a valid date and time.");
    return timestamp;
  }
  if (field.kind === "json") {
    const parsed: unknown = JSON.parse(value);
    if (!isRecord(parsed) || !validPublicViewInput(parsed)) throw new Error("Enter a supported JSON object.");
    return parsed;
  }
  return value;
}

/** Clone only editable roots; nested fields retain unedited JSON siblings. */
export function recordDraftUpdate(
  draft: RecordEditorDraft, descriptor: RecordEditorDescriptor,
): { update: Record<string, unknown> | null; errors: Record<string, string> } {
  const update: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const field of descriptor.fields) {
    if (field.read_only) continue;
    if (!validViewPath(field.path)) { errors[field.id] = "The field is unavailable."; continue; }
    try {
      const value = parsedField(field, draft.values[field.id] ?? "");
      const tokens = field.path.split(".");
      const root = tokens[0];
      if (tokens.length === 1) { update[root] = value; continue; }
      if (!Object.prototype.hasOwnProperty.call(update, root)) {
        const original = readViewPath(draft.record, root);
        update[root] = isRecord(original) ? clone(original) : {};
      }
      let parent = update;
      for (const token of tokens.slice(0, -1)) {
        if (!Object.prototype.hasOwnProperty.call(parent, token) || !isRecord(parent[token])) parent[token] = {};
        parent = parent[token] as Record<string, unknown>;
      }
      parent[tokens[tokens.length - 1]] = value;
    } catch {
      errors[field.id] = "The value is invalid. Check the field format and allowed range.";
    }
  }
  if (!validPublicViewInput(update)) errors._form = "The edited data contains unsupported fields.";
  return { update: Object.keys(errors).length ? null : update, errors };
}

export function recordSavePayload(
  descriptor: RecordEditorDescriptor, snapshot: unknown, draft: RecordEditorDraft,
  context: ViewInputContext = {},
): { payload: Record<string, unknown> | null; errors: Record<string, string> } {
  const base = recordOperationPayload(descriptor.save, draft.source ?? snapshot, draft.record, context);
  const { update, errors } = recordDraftUpdate(draft, descriptor);
  const key = descriptor.save.draft_key;
  if (!base || !update || Object.prototype.hasOwnProperty.call(base, key) || !validPublicViewInput({ [key]: null })) {
    return { payload: null, errors };
  }
  const payload = { ...base, [key]: update };
  return { payload: validPublicViewInput(payload) ? payload : null, errors };
}
