import type {
  CapturedCapabilityInvocation, FrontendCatalog, VerifiedFrontendContribution,
} from "./frontendContracts";
import { parseSurfaceTemplate, surfaceOperations, type SurfaceTemplate } from "./surfaceTemplateContract";
import { resolveSurfaceRenderer, type SurfaceRendererCapture } from "./surfaceRendererCapture";

export const VIEW_VERSION = "tobkiri.ui.view.v1";
export const VIEW_SLOTS = [
  "workspace_tab", "sidebar", "settings", "chat_header",
  "composer_above", "composer_below",
] as const;
export type ViewSlot = typeof VIEW_SLOTS[number];
export type ViewOperation = {
  contribution_id: string; contract_id: string; operation_id: string;
};
export type ViewInputContext = { conversation_id?: string; turn_id?: string };
type ContextBindings = Record<string, keyof ViewInputContext>;
export type ViewNavigationGuard = () => boolean;
export type ViewNavigationGuardChange = (
  ownerId: string, guard: ViewNavigationGuard | null,
) => void;
export type RecordEditorRequest = {
  operation: ViewOperation; input?: Record<string, unknown>;
  context_bindings?: ContextBindings; source_bindings?: Record<string, string>;
  record_bindings?: Record<string, string>;
};
export type RecordEditorField = {
  id: string; label: string; path: string;
  kind: "text" | "multiline" | "integer" | "datetime" | "json";
  required?: boolean; read_only?: boolean; min?: number; max?: number;
};
export type RecordEditorCondition = { path: string; equals: string | number | boolean | null };
export type RecordEditorAction = RecordEditorRequest & {
  id: string; label: string;
  available_when?: RecordEditorCondition; disabled_when?: RecordEditorCondition;
};
export type RecordEditorDefinition = {
  records_path: string; id_path: string; title_path: string; search_paths?: string[];
  columns?: Array<{ label: string; path: string; kind: "text" | "status" | "datetime" }>;
  fields: RecordEditorField[];
  save: RecordEditorRequest & { draft_key: string }; actions?: RecordEditorAction[];
};
export type ConversationThreadRequest = {
  operation: ViewOperation; input?: Record<string, unknown>;
  context_bindings?: ContextBindings; source_bindings?: Record<string, string>;
  turn_id_key: "turn_id"; content_key?: "content";
};
export type ConversationThreadProgressRequest = Omit<ConversationThreadRequest, "content_key"> & { cursor_key: "cursor" };
export type ConversationThreadDefinition = {
  conversation_path: string; messages_path: string; pending_turn_path: string;
  model_reference_path?: string;
  send: ConversationThreadRequest & { content_key: "content" };
  stop?: ConversationThreadRequest; events?: ConversationThreadRequest;
  reconcile?: ConversationThreadRequest;
  progress?: ConversationThreadProgressRequest;
};
export type ViewField = {
  label: string; path: string; kind: "text" | "status" | "progress";
  total_path?: string;
};
export type ViewControl = {
  id: string; label: string; kind: "button" | "toggle" | "text" | "choice";
  operation: ViewOperation; input?: Record<string, unknown>;
  input_bindings?: Record<string, string>; value_key?: string; value_path?: string;
  options_path?: string; id_path?: string; label_path?: string;
  disabled_path?: string; multiple?: boolean;
  context_bindings?: ContextBindings;
};
export type CatalogView = {
  version: typeof VIEW_VERSION; slot: ViewSlot;
  renderer: "panel" | "status" | "entity_picker" | "record_editor" | "conversation_thread" | "surface_template";
  title?: string; body?: string;
  data_source?: ViewOperation & {
    input?: Record<string, unknown>; context_bindings?: ContextBindings;
  };
  fields?: ViewField[]; controls?: ViewControl[];
  record_editor?: RecordEditorDefinition;
  conversation_thread?: ConversationThreadDefinition;
  surface_template?: SurfaceTemplate;
};
export type CatalogViewReference = Partial<SurfaceRendererCapture> & {
  contributionId: string; ownerPackId: string; descriptorHash: string;
  profileId: string; profileRevision: string; activationId: string;
  planHash: string; catalogHash: string;
};
export type RegisteredCatalogView = {
  item: VerifiedFrontendContribution; view: CatalogView;
  reference: CatalogViewReference;
};

const reserved = new Set([
  "__proto__", "prototype", "constructor", "approved", "approval",
  "profile_id", "profile_revision", "activation_id", "plan_digest", "plan_hash",
  "principal_id", "owner_pack_id", "catalog_hash",
]);
const threadOverrides = new Set([
  "profile_id", "execution_profile_id", "principal", "principal_ref", "broker_context", "host_context",
  "model", "model_id", "model_profile", "model_profile_id", "model_reference", "model_policy", "model_override",
  "provider", "provider_id", "system_prompt", "tools", "tool_selection", "thinking_level", "reasoning_effort",
  "strategy", "strategy_reference", "approval_mode", "approval_policy", "permissions", "grants", "capabilities",
  "workspace", "workspace_id", "workspace_root", "cwd",
]);
const own = (value: object, key: PropertyKey) => Object.prototype.hasOwnProperty.call(value, key);
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const bounded = (value: unknown, max: number, min = 0): value is string =>
  typeof value === "string" && value.length >= min && value.length <= max;
const identifier = (value: unknown) => bounded(value, 256, 1)
  && /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/.test(value);
const keys = (value: Record<string, unknown>, allowed: string[]) =>
  Object.keys(value).every((key) => allowed.includes(key));
export const validViewPath = (value: unknown): value is string =>
  bounded(value, 256, 1)
  && /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,15}$/.test(value)
  && value.split(".").every((key) => !["__proto__", "prototype", "constructor"].includes(key));

/** Read own JSON properties only; never evaluate expressions or inherited keys. */
export function readViewPath(value: unknown, path: string | undefined): unknown {
  if (!validViewPath(path)) return undefined;
  let current = value;
  for (const key of path.split(".")) {
    if (!record(current) || !own(current, key)) return undefined;
    current = current[key];
  }
  return current;
}

export function validPublicViewInput(value: unknown, depth = 0): boolean {
  if (depth > 16) return false;
  if (record(value)) {
    if (Object.keys(value).length > 64) return false;
    if (!Object.entries(value).every(([key, item]) =>
      key.length <= 128 && (!reserved.has(key) || (key === "profile_id" && depth > 0)) && !key.startsWith("_")
      && validPublicViewInput(item, depth + 1))) return false;
  } else if (Array.isArray(value)) {
    if (value.length > 256 || !value.every((item) =>
      validPublicViewInput(item, depth + 1))) return false;
  } else if (typeof value === "string") {
    if (value.length > 16384) return false;
  } else if (value !== null && typeof value !== "boolean"
    && !(typeof value === "number" && Number.isFinite(value))) return false;
  return depth > 0 || new TextEncoder().encode(JSON.stringify(value)).length <= 65536;
}

/** Thread UI input cannot choose execution/model/tool/workspace authority. */
export function validConversationThreadInput(value: unknown): boolean {
  if (!validPublicViewInput(value)) return false;
  const admissible = (item: unknown): boolean => {
    if (record(item)) return Object.entries(item).every(([key, child]) =>
      !threadOverrides.has(key) && admissible(child));
    return !Array.isArray(item) || item.every(admissible);
  };
  return admissible(value);
}

const operationValid = (value: unknown, withInput = false) => record(value)
  && keys(value, ["contribution_id", "contract_id", "operation_id", ...(withInput ? ["input", "context_bindings"] : [])])
  && identifier(value.contribution_id) && identifier(value.contract_id)
  && identifier(value.operation_id)
  && (!own(value, "input") || (record(value.input) && validPublicViewInput(value.input)))
  && (!own(value, "context_bindings") || validContextBindings(value.context_bindings, value.input));

function validContextBindings(value: unknown, input: unknown): boolean {
  return record(value) && Object.keys(value).length <= 16
    && Object.entries(value).every(([key, context]) =>
      validPublicViewInput({ [key]: null })
      && ["conversation_id", "turn_id"].includes(String(context))
      && (!record(input) || !own(input, key)));
}

function validRecordRequest(value: unknown, save: boolean): boolean {
  if (!record(value) || !keys(value, [
    "operation", "input", "context_bindings", "source_bindings", "record_bindings",
    ...(save ? ["draft_key"] : ["id", "label", "available_when", "disabled_when"]),
  ]) || !operationValid(value.operation)
    || (own(value, "input") && (!record(value.input) || !validPublicViewInput(value.input)))) return false;
  const claimed = new Set(Object.keys(record(value.input) ? value.input : {}));
  for (const key of ["source_bindings", "record_bindings", "context_bindings"]) {
    const bindings = value[key] ?? {};
    if (!record(bindings) || Object.keys(bindings).length > 32) return false;
    if (key === "context_bindings" && !validContextBindings(bindings, value.input)) return false;
    for (const [inputKey, path] of Object.entries(bindings)) {
      if (!validPublicViewInput({ [inputKey]: null }) || claimed.has(inputKey)
        || (key !== "context_bindings" && !validViewPath(path))) return false;
      claimed.add(inputKey);
    }
  }
  if (save) {
    return bounded(value.draft_key, 64, 1) && /^[a-z][a-z0-9_]*$/.test(value.draft_key)
      && validPublicViewInput({ [value.draft_key]: null }) && !claimed.has(value.draft_key);
  }
  if (!identifier(value.id) || !bounded(value.label, 256, 1)) return false;
  return ["available_when", "disabled_when"].every((key) => {
    const condition = value[key];
    if (!own(value, key)) return true;
    return record(condition) && keys(condition, ["path", "equals"])
      && validViewPath(condition.path) && own(condition, "equals")
      && (condition.equals === null || typeof condition.equals === "boolean"
        || bounded(condition.equals, 256)
        || (typeof condition.equals === "number" && Number.isFinite(condition.equals)));
  });
}

export function parseRecordEditor(value: unknown): RecordEditorDefinition | null {
  if (!record(value) || !keys(value, [
    "records_path", "id_path", "title_path", "search_paths", "columns", "fields", "save", "actions",
  ]) || !["records_path", "id_path", "title_path"].every((key) => validViewPath(value[key]))
    || !validRecordRequest(value.save, true)) return null;
  const searches = value.search_paths ?? [];
  const columns = value.columns ?? [];
  const fields = value.fields;
  const actions = value.actions ?? [];
  if (!Array.isArray(searches) || searches.length > 8 || new Set(searches).size !== searches.length
    || !searches.every(validViewPath) || !Array.isArray(columns) || columns.length > 12
    || !columns.every((column) => record(column) && keys(column, ["label", "path", "kind"])
      && bounded(column.label, 256, 1) && validViewPath(column.path)
      && ["text", "status", "datetime"].includes(String(column.kind)))
    || !Array.isArray(fields) || fields.length === 0 || fields.length > 16
    || !fields.every((field) => record(field)
      && keys(field, ["id", "label", "path", "kind", "required", "min", "max", "read_only"])
      && identifier(field.id) && bounded(field.label, 256, 1) && validViewPath(field.path)
      && ["text", "multiline", "integer", "datetime", "json"].includes(String(field.kind))
      && ["required", "read_only"].every((key) => !own(field, key) || typeof field[key] === "boolean")
      && ["min", "max"].every((key) => !own(field, key)
        || (typeof field[key] === "number" && Number.isFinite(field[key])))
      && !(typeof field.min === "number" && typeof field.max === "number" && field.min > field.max))
    || !Array.isArray(actions) || actions.length > 16 || !actions.every((action) => validRecordRequest(action, false))) return null;
  if (new Set(fields.map((field) => field.id)).size !== fields.length
    || new Set(actions.map((action) => action.id)).size !== actions.length
    || fields.some((field, index) => fields.some((other, otherIndex) => index !== otherIndex
      && (field.path === other.path || field.path.startsWith(other.path + "."))))) return null;
  return value as unknown as RecordEditorDefinition;
}

function validThreadRequest(value: unknown, send: boolean, progress = false): boolean {
  if (!record(value) || !keys(value, [
    "operation", "input", "source_bindings", "context_bindings", "turn_id_key",
    ...(send ? ["content_key"] : []),
    ...(progress ? ["cursor_key"] : []),
  ]) || !operationValid(value.operation) || value.turn_id_key !== "turn_id"
    || (send && value.content_key !== "content")
    || (own(value, "input") && (!record(value.input) || !validConversationThreadInput(value.input)))) return false;
  const claimed = new Set(Object.keys(record(value.input) ? value.input : {}));
  for (const key of ["source_bindings", "context_bindings"]) {
    const bindings = value[key] ?? {};
    if (!record(bindings) || Object.keys(bindings).length > (key === "context_bindings" ? 16 : 32)
      || (key === "context_bindings" && !validContextBindings(bindings, value.input))) return false;
    for (const [inputKey, path] of Object.entries(bindings)) {
      if (!/^[a-z][a-z0-9_]{0,63}$/.test(inputKey) || !validConversationThreadInput({ [inputKey]: null })
        || claimed.has(inputKey) || (key === "source_bindings" && !validViewPath(path))) return false;
      claimed.add(inputKey);
    }
  }
  if (progress && (value.cursor_key !== "cursor" || claimed.size !== 1 || !claimed.has("conversation_id")
    || !record(value.operation) || value.operation.contract_id !== "tobkiri.resource.turn.progress.v1"
    || value.operation.operation_id !== "rumi_turn_runtime_pack.turn-progress-resource")) return false;
  return !claimed.has("turn_id") && !claimed.has("content") && (!progress || !claimed.has("cursor"));
}

/** A thread uses canonical data and fixed text/turn keys, never model authority. */
export function parseConversationThread(value: unknown): ConversationThreadDefinition | null {
  if (!record(value) || !keys(value, [
    "conversation_path", "messages_path", "pending_turn_path", "model_reference_path", "send", "stop", "events", "reconcile", "progress",
  ]) || !["conversation_path", "messages_path", "pending_turn_path"].every((key) => validViewPath(value[key]))
    || (own(value, "model_reference_path") && !validViewPath(value.model_reference_path))
    || !validThreadRequest(value.send, true)
    || ["stop", "events", "reconcile"].some((key) => own(value, key) && !validThreadRequest(value[key], false))
    || (own(value, "progress") && !validThreadRequest(value.progress, false, true))) return null;
  return value as unknown as ConversationThreadDefinition;
}

/** Validate the complete view before resolving any shipped renderer. */
export function parseCatalogView(value: unknown): CatalogView | null {
  if (!record(value) || !keys(value, [
    "version", "slot", "renderer", "title", "body", "data_source", "fields", "controls", "record_editor", "conversation_thread", "surface_template",
  ]) || value.version !== VIEW_VERSION
    || !VIEW_SLOTS.includes(value.slot as ViewSlot)
    || !["panel", "status", "entity_picker", "record_editor", "conversation_thread", "surface_template"].includes(String(value.renderer))
    || (own(value, "title") && !bounded(value.title, 256))
    || (own(value, "body") && !bounded(value.body, 4096))
    || (own(value, "data_source") && !operationValid(value.data_source, true))) return null;
  if (value.renderer === "record_editor" ? !parseRecordEditor(value.record_editor)
    : own(value, "record_editor")) return null;
  if (value.renderer === "conversation_thread" ? !own(value, "data_source")
    || !parseConversationThread(value.conversation_thread) : own(value, "conversation_thread")) return null;
  if (value.renderer === "conversation_thread" && record(value.data_source)
    && !validConversationThreadInput(value.data_source.input ?? {})) return null;
  if (value.renderer === "surface_template" ? !parseSurfaceTemplate(value.surface_template)
    : own(value, "surface_template")) return null;
  if (own(value, "fields") && (!Array.isArray(value.fields) || value.fields.length > 32
    || !value.fields.every((field) => record(field)
      && keys(field, ["label", "path", "kind", "total_path"])
      && bounded(field.label, 256, 1) && validViewPath(field.path)
      && ["text", "status", "progress"].includes(String(field.kind))
      && (!own(field, "total_path") || validViewPath(field.total_path))))) return null;
  if (own(value, "controls")) {
    if (!Array.isArray(value.controls) || value.controls.length > 16) return null;
    const ids = new Set<string>();
    for (const control of value.controls) {
      if (!record(control) || !keys(control, [
        "id", "label", "kind", "operation", "input", "input_bindings", "value_key",
        "value_path", "options_path", "id_path", "label_path", "disabled_path", "multiple", "context_bindings",
      ]) || !identifier(control.id) || ids.has(String(control.id))
        || !bounded(control.label, 256, 1)
        || !["button", "toggle", "text", "choice"].includes(String(control.kind))
        || !operationValid(control.operation)
        || (own(control, "input") && (!record(control.input) || !validPublicViewInput(control.input)))
        || (own(control, "multiple") && typeof control.multiple !== "boolean")) return null;
      ids.add(String(control.id));
      const bindings = control.input_bindings ?? {};
      const contexts = control.context_bindings ?? {};
      if (!validContextBindings(contexts, control.input)
        || Object.keys(contexts).some((key) => record(bindings) && own(bindings, key))) return null;
      if (!record(bindings) || Object.keys(bindings).length > 32
        || !Object.entries(bindings).every(([key, path]) =>
          validPublicViewInput({ [key]: null }) && validViewPath(path)
          && !(record(control.input) && own(control.input, key)))) return null;
      if (control.kind !== "button" || own(control, "value_key")) {
        if (!bounded(control.value_key, 64, 1) || !/^[a-z][a-z0-9_]*$/.test(control.value_key)
          || !validPublicViewInput({ [control.value_key]: null })
          || own(bindings, control.value_key)
          || own(contexts, control.value_key)
          || (record(control.input) && own(control.input, control.value_key))) return null;
      }
      for (const key of ["value_path", "options_path", "id_path", "label_path", "disabled_path"]) {
        if (own(control, key) && !validViewPath(control[key])) return null;
      }
      if (control.kind === "choice" && !["options_path", "id_path", "label_path"].every(
        (key) => validViewPath(control[key]))) return null;
    }
  }
  return value as unknown as CatalogView;
}

const isActive = (catalog: FrontendCatalog, item: VerifiedFrontendContribution, plan: string) =>
  catalog.version === "rumi.ui.contribution.v1" && catalog.plan_hash === plan
  && item.resolved_profile_id === catalog.profile_id
  && item.resolved_profile_revision === catalog.profile_revision
  && item.resolved_activation_id === catalog.activation_id
  && item.resolved_plan_hash === plan
  && !catalog.quarantined_pack_ids.includes(item.owner_pack_id);

export function viewReference(
  catalog: FrontendCatalog, item: VerifiedFrontendContribution,
): CatalogViewReference {
  const template = parseSurfaceTemplate(item.view?.surface_template);
  const renderer = template ? resolveSurfaceRenderer(catalog, template) : null;
  return {
    contributionId: item.contribution_id, ownerPackId: item.owner_pack_id,
    descriptorHash: item.descriptor_hash, profileId: catalog.profile_id,
    profileRevision: catalog.profile_revision, activationId: catalog.activation_id,
    planHash: catalog.plan_hash, catalogHash: catalog.catalog_hash,
    ...(renderer?.capture ?? {}),
  };
}

/** Build a new registry for every catalog; disable/unregister leaves no stale entries. */
export function viewsForSlot(
  catalog: FrontendCatalog, slot: ViewSlot, activePlanHash: string,
): RegisteredCatalogView[] {
  return catalog.contributions.flatMap((item) => {
    if (item.kind !== "view" || item.mode !== "declarative"
      || !isActive(catalog, item, activePlanHash)
      || catalog.contributions.filter((other) => other.contribution_id === item.contribution_id).length !== 1) return [];
    const view = parseCatalogView(item.view);
    if (view?.surface_template && !resolveSurfaceRenderer(catalog, view.surface_template)) return [];
    return view?.slot === slot ? [{ item, view, reference: viewReference(catalog, item) }] : [];
  }).sort((left, right) => right.item.priority - left.item.priority
    || left.item.contribution_id.localeCompare(right.item.contribution_id));
}

export function matchesViewReference(
  registered: RegisteredCatalogView, reference: CatalogViewReference,
): boolean {
  const current = registered.reference;
  const keys = new Set([...Object.keys(current), ...Object.keys(reference)]);
  return [...keys].every((key) => {
    if (key === "rendererExpiresAtMs") {
      return Number.isSafeInteger(current.rendererExpiresAtMs) && Number.isSafeInteger(reference.rendererExpiresAtMs)
        && Number(reference.rendererExpiresAtMs) > Date.now()
        && Number(reference.rendererExpiresAtMs) <= Number(current.rendererExpiresAtMs);
    }
    return current[key as keyof CatalogViewReference] === reference[key as keyof CatalogViewReference];
  });
}

/** Select an operation from the captured catalog, never from view-owned authority. */
export function viewOperationRequest(
  catalog: FrontendCatalog, registered: RegisteredCatalogView,
  operation: ViewOperation, payload: Record<string, unknown>,
): CapturedCapabilityInvocation | null {
  if (!isActive(catalog, registered.item, registered.reference.planHash)
    || !matchesViewReference({ ...registered, reference: viewReference(catalog, registered.item) }, registered.reference)
    || catalog.contributions.filter((item) =>
      item.contribution_id === registered.item.contribution_id
      && item.owner_pack_id === registered.item.owner_pack_id
      && item.descriptor_hash === registered.item.descriptor_hash).length !== 1
    || !validPublicViewInput(payload)
    || (registered.view.renderer === "conversation_thread" && !validConversationThreadInput(payload))) return null;
  const declared = [
    registered.view.data_source,
    ...(registered.view.controls ?? []).map((control) => control.operation),
    registered.view.record_editor?.save.operation,
    ...(registered.view.record_editor?.actions ?? []).map((action) => action.operation),
    registered.view.conversation_thread?.send.operation,
    registered.view.conversation_thread?.stop?.operation,
    registered.view.conversation_thread?.events?.operation,
    registered.view.conversation_thread?.reconcile?.operation,
    registered.view.conversation_thread?.progress?.operation,
    ...(registered.view.surface_template ? surfaceOperations(registered.view.surface_template) : []),
  ];
  if (!declared.some((item) => item
    && item.contribution_id === operation.contribution_id
    && item.contract_id === operation.contract_id
    && item.operation_id === operation.operation_id)) return null;
  const targets = catalog.contributions.filter((item) =>
    item.kind === "action" && item.contribution_id === operation.contribution_id
    && item.action_contract === operation.contract_id
    && item.operation_id === operation.operation_id
    && isActive(catalog, item, registered.reference.planHash));
  if (targets.length !== 1 || catalog.contributions.filter((item) =>
    item.contribution_id === operation.contribution_id).length !== 1) return null;
  return {
    contractId: operation.contract_id, payload,
    profileId: catalog.profile_id, profileRevision: catalog.profile_revision,
    activationId: catalog.activation_id, planHash: catalog.plan_hash,
    catalogHash: catalog.catalog_hash, contributionId: targets[0].contribution_id,
    ownerPackId: targets[0].owner_pack_id,
  };
}

/** Automatic reads require Host evidence of a pure/read executable Operation. */
export function viewReadRequest(
  catalog: FrontendCatalog, registered: RegisteredCatalogView,
  operation: ViewOperation, payload: Record<string, unknown>,
): CapturedCapabilityInvocation | null {
  const request = viewOperationRequest(catalog, registered, operation, payload);
  if (!request || !catalog.contributions.some((item) =>
    item.kind === "action" && item.contribution_id === request.contributionId
    && item.action_contract === operation.contract_id && item.operation_id === operation.operation_id
    && item.read_only === true && isActive(catalog, item, registered.reference.planHash))) return null;
  return request;
}

export function controlPayload(
  control: ViewControl, snapshot: unknown, value?: unknown,
  context: ViewInputContext = {},
): Record<string, unknown> | null {
  const payload = requestContextInput(control, context);
  if (!payload) return null;
  for (const [key, path] of Object.entries(control.input_bindings ?? {})) {
    const bound = readViewPath(snapshot, path);
    if (bound === undefined) return null;
    payload[key] = bound;
  }
  if (control.value_key) payload[control.value_key] = value;
  return validPublicViewInput(payload) ? payload : null;
}

export type ViewChoice = { id: string; label: string; disabled: boolean };
export function viewChoices(control: ViewControl, snapshot: unknown): ViewChoice[] {
  const items = readViewPath(snapshot, control.options_path);
  if (!Array.isArray(items) || items.length > 256) return [];
  const choices = items.flatMap((item) => {
    const id = readViewPath(item, control.id_path);
    const label = readViewPath(item, control.label_path);
    return bounded(id, 256, 1) && bounded(label, 256, 1) ? [{
      id, label, disabled: readViewPath(item, control.disabled_path) === true,
    }] : [];
  });
  const ids = choices.map((item) => item.id);
  return choices.filter((item) => ids.filter((id) => id === item.id).length === 1);
}

export function choicePayload(
  control: ViewControl, snapshot: unknown, values: string[],
  context: ViewInputContext = {},
): Record<string, unknown> | null {
  const available = viewChoices(control, snapshot).filter((item) => !item.disabled);
  if (new Set(values).size !== values.length || values.some((value) =>
    !available.some((item) => item.id === value)) || (!control.multiple && values.length !== 1)) return null;
  return controlPayload(control, snapshot, control.multiple ? values : values[0], context);
}

export function requestContextInput(
  request: { input?: Record<string, unknown>; context_bindings?: ContextBindings },
  context: ViewInputContext,
): Record<string, unknown> | null {
  const payload = { ...request.input };
  for (const [key, contextKey] of Object.entries(request.context_bindings ?? {})) {
    const value = context[contextKey];
    if (!bounded(value, 256, 1) || value.trim() !== value || /[\x00-\x1f\x7f]/.test(value)) return null;
    payload[key] = value;
  }
  return validPublicViewInput(payload) ? payload : null;
}

/** Unrelated turn/context changes cannot erase an editor that never consumes them. */
export function viewContextKey(
  registered: RegisteredCatalogView, context: ViewInputContext,
): string {
  const requests = [
    registered.view.data_source,
    ...(registered.view.controls ?? []),
    registered.view.record_editor?.save,
    ...(registered.view.record_editor?.actions ?? []),
    registered.view.conversation_thread?.send,
    registered.view.conversation_thread?.stop,
    registered.view.conversation_thread?.events,
    registered.view.conversation_thread?.reconcile,
    registered.view.conversation_thread?.progress,
    ...(registered.view.surface_template?.nodes.flatMap((node) => [
      ...(node.intents ?? []).map((intent) => intent.request),
      ...(node.resource ? [node.resource.acquire, node.resource.exchange] : []),
    ]) ?? []),
  ];
  const consumed = new Set(requests.flatMap((request) =>
    Object.values(request?.context_bindings ?? {})));
  return JSON.stringify([...consumed].sort().map((key) => [key, context[key] ?? null]));
}
