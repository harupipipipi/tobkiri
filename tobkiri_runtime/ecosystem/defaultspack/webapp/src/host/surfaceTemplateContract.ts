/** Renderer-neutral declarations contain data; catalog capture supplies trust. */
export const SURFACE_TEMPLATE_VERSION = "tobkiri.ui.surface-template.v1";
export const SURFACE_RENDERER_VERSION = "tobkiri.ui.surface-renderer.v1";
export const SURFACE_INTENT_VERSION = "tobkiri.ui.surface-intent.v1";
export const SURFACE_OUTCOME_VERSION = "tobkiri.ui.surface-outcome.v1";
export const SURFACE_RESOURCE_VERSION = "tobkiri.ui.surface-resource.v1";
export const SURFACE_EVENTS = {
  content: ["activate"], problem: ["retry", "dismiss"], notice: ["dismiss"],
  progress: ["cancel"], resource_input: ["submit"], choice: ["select"],
  form: ["submit"], confirmation: ["confirm", "decline"], collection: ["select"], detail: ["close"],
} as const;
export type SurfacePattern = keyof typeof SURFACE_EVENTS;
export type SurfaceOperation = { contribution_id: string; contract_id: string; operation_id: string };
export type SurfaceRequest = {
  operation: SurfaceOperation; input?: Record<string, unknown>;
  source_bindings?: Record<string, string>;
  context_bindings?: Record<string, "conversation_id" | "turn_id">;
};
export type SurfaceIntentDefinition = {
  id: string; label: string; event: string; request: SurfaceRequest;
  intent_key: "surface_intent"; outcome_path?: string;
};
export type SurfaceResourceKind = "file" | "image" | "audio";
export type SurfaceResource = {
  version: typeof SURFACE_RESOURCE_VERSION; selection_id: string;
  kind: SurfaceResourceKind; display_name: string; expires_at_ms: number;
  stage: "selected" | "exchanged";
};
export type SurfaceField = {
  id: string; label: string; type: "text" | "integer" | "boolean";
  required?: boolean; min?: number; max?: number;
};
export type SurfaceNode = {
  id: string; pattern: SurfacePattern; label: string; body?: string;
  value_path?: string; total_path?: string; intents?: SurfaceIntentDefinition[];
  choices?: Array<{ id: string; label: string }>; multiple?: boolean;
  fields?: SurfaceField[]; items_path?: string; id_path?: string; label_path?: string;
  resource?: { kind: SurfaceResourceKind; acquire: SurfaceRequest; exchange: SurfaceRequest };
};
export type SurfaceTemplate = {
  version: typeof SURFACE_TEMPLATE_VERSION; template_id: string;
  renderer_contribution_id: string; renderer_api_version: "1.0.0"; nodes: SurfaceNode[];
};
export type SurfaceRenderer = {
  version: typeof SURFACE_RENDERER_VERSION; api_version: "1.0.0";
  implementation: "semantic_standard" | "semantic_compact";
  patterns: SurfacePattern[]; ttl_ms: number;
};
export type SurfaceIntent = {
  version: typeof SURFACE_INTENT_VERSION; template_id: string; node_id: string;
  intent_id: string; event: string; values: Record<string, unknown>;
};
export type SurfaceOutcome = {
  version: typeof SURFACE_OUTCOME_VERSION; template_id: string; node_id: string;
  intent_id: string; event: string; status: "accepted" | "rejected" | "pending"; message?: string;
};

export const surfaceRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const own = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);
const keys = (value: Record<string, unknown>, allowed: string[]) => Object.keys(value).every((key) => allowed.includes(key));
const text = (value: unknown, maximum = 256): value is string =>
  typeof value === "string" && value.length > 0 && value.length <= maximum;
const identifier = (value: unknown): value is string => text(value) && /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/.test(value);
export const surfacePath = (value: unknown): value is string => text(value)
  && /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,15}$/.test(value)
  && value.split(".").every((part) => !["__proto__", "prototype", "constructor"].includes(part));
const reserved = new Set([
  "__proto__", "prototype", "constructor", "approved", "approval", "profile_id", "profile_revision",
  "activation_id", "plan_digest", "plan_hash", "principal_id", "owner_pack_id", "catalog_hash",
  "permissions", "grants", "broker_context", "host_context", "resource_handle", "file_path",
]);
export function surfacePublicInput(value: unknown, depth = 0): boolean {
  if (depth > 16) return false;
  if (surfaceRecord(value)) {
    if (Object.keys(value).length > 32 || Object.entries(value).some(([key, item]) =>
      key.length > 128 || reserved.has(key) || key.startsWith("_") || !surfacePublicInput(item, depth + 1))) return false;
  } else if (Array.isArray(value)) {
    if (value.length > 256 || value.some((item) => !surfacePublicInput(item, depth + 1))) return false;
  } else if (typeof value === "string") { if (value.length > 16384) return false;
  } else if (typeof value === "number") { if (!Number.isFinite(value)) return false;
  } else if (value !== null && typeof value !== "boolean") return false;
  if (depth === 0 && new TextEncoder().encode(JSON.stringify(value)).length > 65536) return false;
  return true;
}
export function surfaceReadPath(value: unknown, path?: string): unknown {
  if (!path) return value;
  if (!surfacePath(path)) return undefined;
  return path.split(".").reduce<unknown>((current, part) =>
    surfaceRecord(current) && own(current, part) ? current[part] : undefined, value);
}
const unique = (values: unknown[]) => new Set(values).size === values.length;
export function parseSurfaceRequest(value: unknown, fixed?: string): SurfaceRequest | null {
  if (!surfaceRecord(value) || !keys(value, ["operation", "input", "source_bindings", "context_bindings"])
    || !surfaceRecord(value.operation) || !keys(value.operation, ["contribution_id", "contract_id", "operation_id"])
    || !["contribution_id", "contract_id", "operation_id"].every((key) => identifier((value.operation as Record<string, unknown>)[key]))
    || (own(value, "input") && (!surfaceRecord(value.input) || !surfacePublicInput(value.input)))) return null;
  const claimed = new Set(Object.keys(value.input ?? {}));
  for (const key of ["source_bindings", "context_bindings"]) {
    const binding = value[key] ?? {};
    if (!surfaceRecord(binding) || Object.keys(binding).length > (key === "context_bindings" ? 16 : 32)) return null;
    for (const [name, path] of Object.entries(binding)) {
      if (!/^[a-z][a-z0-9_]{0,63}$/.test(name) || !surfacePublicInput({ [name]: null }) || claimed.has(name)
        || (key === "source_bindings" ? !surfacePath(path) : !["conversation_id", "turn_id"].includes(String(path)))) return null;
      claimed.add(name);
    }
  }
  return fixed && claimed.has(fixed) ? null : value as unknown as SurfaceRequest;
}

/** Reject the whole declaration before any renderer or operation is resolved. */
export function parseSurfaceTemplate(value: unknown): SurfaceTemplate | null {
  if (!surfaceRecord(value) || !keys(value, ["version", "template_id", "renderer_contribution_id", "renderer_api_version", "nodes"])
    || value.version !== SURFACE_TEMPLATE_VERSION || !identifier(value.template_id) || !identifier(value.renderer_contribution_id)
    || value.renderer_api_version !== "1.0.0" || !Array.isArray(value.nodes) || value.nodes.length < 1 || value.nodes.length > 32) return null;
  const ids: string[] = [];
  for (const node of value.nodes) {
    if (!surfaceRecord(node) || !identifier(node.id) || !own(SURFACE_EVENTS, String(node.pattern)) || !text(node.label)) return null;
    const pattern = node.pattern as SurfacePattern;
    const extras: Partial<Record<SurfacePattern, string[]>> = {
      progress: ["total_path"], choice: ["choices", "multiple"], form: ["fields"],
      collection: ["items_path", "id_path", "label_path"], resource_input: ["resource"],
    };
    if (!keys(node, ["id", "pattern", "label", "body", "value_path", "intents", ...(extras[pattern] ?? [])])
      || (own(node, "body") && !text(node.body, 4096))) return null;
    ids.push(node.id);
    for (const key of ["value_path", "total_path", "items_path", "id_path", "label_path"]) if (own(node, key) && !surfacePath(node[key])) return null;
    const intents = node.intents ?? [];
    if (!Array.isArray(intents) || intents.length > 8 || !unique(intents.map((intent) => surfaceRecord(intent) ? intent.id : null))) return null;
    if (intents.some((intent) => !surfaceRecord(intent) || !keys(intent, ["id", "label", "event", "request", "intent_key", "outcome_path"])
      || !identifier(intent.id) || !text(intent.label) || !(SURFACE_EVENTS[pattern] as readonly string[]).includes(String(intent.event))
      || intent.intent_key !== "surface_intent" || !parseSurfaceRequest(intent.request, "surface_intent")
      || (own(intent, "outcome_path") && !surfacePath(intent.outcome_path)))) return null;
    if (pattern === "progress" && (!surfacePath(node.value_path) || !surfacePath(node.total_path))) return null;
    if (pattern === "choice") {
      if (!Array.isArray(node.choices) || node.choices.length < 1 || node.choices.length > 64
        || node.choices.some((choice) => !surfaceRecord(choice) || !keys(choice, ["id", "label"]) || !identifier(choice.id) || !text(choice.label))
        || !unique(node.choices.map((choice) => choice.id)) || (own(node, "multiple") && typeof node.multiple !== "boolean")) return null;
    }
    if (pattern === "form") {
      if (!Array.isArray(node.fields) || node.fields.length < 1 || node.fields.length > 16
        || node.fields.some((field) => !surfaceRecord(field) || !keys(field, ["id", "label", "type", "required", "min", "max"])
          || !identifier(field.id) || !surfacePublicInput({ [String(field.id)]: null }) || !text(field.label) || !["text", "integer", "boolean"].includes(String(field.type))
          || (own(field, "required") && typeof field.required !== "boolean")
          || ["min", "max"].some((key) => own(field, key) && (field.type !== "integer" || !Number.isSafeInteger(field[key])))
          || (typeof field.min === "number" && typeof field.max === "number" && field.min > field.max))
        || !unique(node.fields.map((field) => field.id))) return null;
    }
    if (pattern === "collection" && !["items_path", "id_path", "label_path"].every((key) => surfacePath(node[key]))) return null;
    if (pattern === "resource_input") {
      if (!surfaceRecord(node.resource) || !keys(node.resource, ["kind", "acquire", "exchange"])
        || !["file", "image", "audio"].includes(String(node.resource.kind))
        || !parseSurfaceRequest(node.resource.acquire) || !parseSurfaceRequest(node.resource.exchange, "selection_id")) return null;
    }
  }
  if (!unique(ids) || new TextEncoder().encode(JSON.stringify(value)).length > 65536) return null;
  return value as unknown as SurfaceTemplate;
}

export function parseSurfaceRenderer(value: unknown): SurfaceRenderer | null {
  return surfaceRecord(value) && keys(value, ["version", "api_version", "implementation", "patterns", "ttl_ms"])
    && value.version === SURFACE_RENDERER_VERSION && value.api_version === "1.0.0"
    && ["semantic_standard", "semantic_compact"].includes(String(value.implementation))
    && Array.isArray(value.patterns) && value.patterns.length > 0 && value.patterns.length <= 10
    && unique(value.patterns) && value.patterns.every((pattern) => typeof pattern === "string" && own(SURFACE_EVENTS, pattern))
    && Number.isSafeInteger(value.ttl_ms) && Number(value.ttl_ms) >= 1000 && Number(value.ttl_ms) <= 300000
    ? value as unknown as SurfaceRenderer : null;
}

/** Public token syntax is not authority: the consuming Host must look it up. */
export function parseSurfaceResource(value: unknown, kind: SurfaceResourceKind, stage: SurfaceResource["stage"], now = Date.now()): SurfaceResource | null {
  return surfaceRecord(value) && Object.keys(value).length === 6
    && keys(value, ["version", "selection_id", "kind", "display_name", "expires_at_ms", "stage"])
    && value.version === SURFACE_RESOURCE_VERSION && value.kind === kind && value.stage === stage
    && typeof value.selection_id === "string" && /^[A-Za-z0-9_-]{32,128}$/.test(value.selection_id)
    && text(value.display_name) && Number.isSafeInteger(value.expires_at_ms) && Number(value.expires_at_ms) > now
    ? value as SurfaceResource : null;
}

export function normalizeSurfaceIntent(template: SurfaceTemplate, nodeId: string, intentId: string, values: Record<string, unknown>, now = Date.now()): SurfaceIntent | null {
  if (!parseSurfaceTemplate(template) || !surfacePublicInput(values)) return null;
  const node = template.nodes.find((item) => item.id === nodeId);
  const intent = node?.intents?.find((item) => item.id === intentId);
  if (!node || !intent) return null;
  if (node.pattern === "choice") {
    const selected = values.selected;
    if (!keys(values, ["selected"]) || !Array.isArray(selected) || !selected.length || !unique(selected)
      || selected.length > (node.choices?.length ?? 0) || (!node.multiple && selected.length !== 1)
      || selected.some((id) => !node.choices?.some((choice) => choice.id === id))) return null;
  } else if (node.pattern === "form") {
    const fields = values.fields;
    if (!keys(values, ["fields"]) || !surfaceRecord(fields) || Object.keys(fields).some((id) => !node.fields?.some((field) => field.id === id))) return null;
    for (const field of node.fields ?? []) {
      if (!own(fields, field.id)) { if (field.required) return null; continue; }
      const item = fields[field.id];
      if (field.type === "text" ? typeof item !== "string" : field.type === "boolean" ? typeof item !== "boolean"
        : !Number.isSafeInteger(item) || Number(item) < (field.min ?? -Number.MAX_SAFE_INTEGER) || Number(item) > (field.max ?? Number.MAX_SAFE_INTEGER)) return null;
    }
  } else if (node.pattern === "resource_input") {
    if (!keys(values, ["resource"]) || !node.resource || !parseSurfaceResource(values.resource, node.resource.kind, "exchanged", now)) return null;
  } else if (node.pattern === "collection") {
    if (!keys(values, ["selected_id"]) || !text(values.selected_id)) return null;
  } else if (Object.keys(values).length !== 0) return null;
  return { version: SURFACE_INTENT_VERSION, template_id: template.template_id, node_id: nodeId,
    intent_id: intentId, event: intent.event, values: JSON.parse(JSON.stringify(values)) as Record<string, unknown> };
}

export function parseSurfaceOutcome(value: unknown, intent: SurfaceIntent): SurfaceOutcome | null {
  return surfaceRecord(value) && keys(value, ["version", "template_id", "node_id", "intent_id", "event", "status", "message"])
    && value.version === SURFACE_OUTCOME_VERSION && ["accepted", "rejected", "pending"].includes(String(value.status))
    && ["template_id", "node_id", "intent_id", "event"].every((key) => value[key] === intent[key as keyof SurfaceIntent])
    && (!own(value, "message") || typeof value.message === "string" && value.message.length <= 4096)
    ? value as SurfaceOutcome : null;
}

export const surfaceOperations = (template: SurfaceTemplate): SurfaceOperation[] => template.nodes.flatMap((node) => [
  ...(node.intents ?? []).map((intent) => intent.request.operation),
  ...(node.resource ? [node.resource.acquire.operation, node.resource.exchange.operation] : []),
]);
