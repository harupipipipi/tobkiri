import type { TemplateCatalogMetadataItem, UICatalog } from "./api";

export const STATUS_SURFACE_API_VERSION = "rumi.status_surface.v1";

export const STATUS_SURFACE_SLOTS = [
  "above_composer",
  "below_composer",
  "chat_header",
  "sidebar",
  "workspace_panel",
] as const;

export type StatusSurfaceSlot = typeof STATUS_SURFACE_SLOTS[number];

export type StatusSurfaceDiagnostic = {
  code: string;
  message: string;
  surfaceId: string;
  templateId?: string;
  sourcePackId?: string;
  trustLevel?: string;
  path?: string;
};

export type StatusSurfaceOption = {
  value: string;
  label: string;
  disabled?: boolean;
  disabledReason?: string;
};

export type StatusSurfaceControlKind =
  | "button"
  | "toggle_button"
  | "expand"
  | "model_select"
  | "provider_select"
  | "thinking_select"
  | "select"
  | "menu";

export type ResolvedStatusSurfaceControl = {
  id: string;
  type: StatusSurfaceControlKind;
  label: string;
  icon?: string;
  actionId?: string;
  value?: string | boolean | number | null;
  disabled: boolean;
  disabledReason?: string;
  options: StatusSurfaceOption[];
};

export type ResolvedStatusSurface = {
  id: string;
  apiVersion: typeof STATUS_SURFACE_API_VERSION;
  slot: StatusSurfaceSlot;
  priority: number;
  order: number;
  icon?: string;
  title: string;
  summary?: string;
  status?: string;
  severity: "neutral" | "success" | "warning" | "error";
  startedAt?: string;
  progress?: { current: number; total: number; label?: string };
  count?: number;
  details: Array<{ label?: string; value: string }>;
  controls: ResolvedStatusSurfaceControl[];
  dataSourceId?: string;
  sourceRevision?: string;
  templateId?: string;
  sourcePackId?: string;
  trustLevel?: string;
  diagnostics: StatusSurfaceDiagnostic[];
  unsupported: boolean;
};

export type StatusSurfaceResolution = {
  surfaces: ResolvedStatusSurface[];
  diagnostics: StatusSurfaceDiagnostic[];
};

export type StatusSurfaceActionRequest = {
  surfaceId: string;
  controlId: string;
  actionId: string;
  value?: string | boolean | number | null;
  dataSourceId?: string;
  sourceRevision?: string;
};

type JsonRecord = Record<string, unknown>;

const VALID_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const VALID_PATH_SEGMENT = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/;
const BLOCKED_PATH_SEGMENTS = new Set(["__proto__", "constructor", "prototype"]);
const CONTROL_KINDS = new Set<StatusSurfaceControlKind>([
  "button",
  "toggle_button",
  "expand",
  "model_select",
  "provider_select",
  "thinking_select",
  "select",
  "menu",
]);
const ACTION_CONTROL_KINDS = new Set<StatusSurfaceControlKind>([
  "button",
  "toggle_button",
  "model_select",
  "provider_select",
  "thinking_select",
  "select",
  "menu",
]);
const MAX_TEXT_LENGTH = 500;
const MAX_OPTIONS = 100;
const MAX_DETAILS = 20;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : null;
}

function text(value: unknown, maxLength = MAX_TEXT_LENGTH): string | undefined {
  if (typeof value !== "string" && typeof value !== "number") return undefined;
  const normalized = String(value).trim();
  return normalized ? normalized.slice(0, maxLength) : undefined;
}

function numberValue(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function booleanValue(value: unknown): boolean {
  return value === true || value === "true" || value === 1;
}

function safeId(value: unknown): string | undefined {
  const normalized = text(value, 128);
  return normalized && VALID_ID.test(normalized) ? normalized : undefined;
}

function safePath(path: unknown): string[] | null {
  const normalized = text(path, 256);
  if (!normalized) return null;
  const segments = normalized.split(".");
  if (
    segments.length > 12
    || segments.some((segment) => !VALID_PATH_SEGMENT.test(segment) || BLOCKED_PATH_SEGMENTS.has(segment))
  ) return null;
  return segments;
}

export function readStatusSurfacePath(source: unknown, path: unknown): unknown {
  const segments = safePath(path);
  if (!segments) return undefined;
  let current: unknown = source;
  for (const segment of segments) {
    const currentRecord = record(current);
    if (!currentRecord || !Object.prototype.hasOwnProperty.call(currentRecord, segment)) return undefined;
    current = currentRecord[segment];
  }
  return current;
}

function sourceIdentity(item: TemplateCatalogMetadataItem): string[] {
  return [item.data_source, item.source, item.id]
    .map((value) => safeId(value))
    .filter((value): value is string => Boolean(value));
}

function actionIdentity(item: TemplateCatalogMetadataItem): string[] {
  return [item.action_id, item.command_id, item.id, item.name]
    .map((value) => safeId(value))
    .filter((value): value is string => Boolean(value));
}

function executableActionIdentity(item: TemplateCatalogMetadataItem): string[] {
  const nestedCommand = record(item.command);
  const execution = record(item.execution) ?? record(nestedCommand?.execution);
  if (!execution) return [];
  return [
    ...actionIdentity(item),
    ...(nestedCommand ? actionIdentity(nestedCommand) : []),
  ];
}

function dataSourceSnapshot(item: TemplateCatalogMetadataItem | undefined): JsonRecord | null {
  if (!item) return null;
  return record(item.snapshot)
    ?? record(item.value)
    ?? record(item.data)
    ?? record(item.state)
    ?? null;
}

function diagnostic(
  surfaceId: string,
  raw: TemplateCatalogMetadataItem,
  code: string,
  message: string,
  path?: string,
): StatusSurfaceDiagnostic {
  return {
    code,
    message,
    surfaceId,
    templateId: text(raw.template_id, 160),
    sourcePackId: text(raw.source_pack_id ?? record(raw.origin)?.pack_id, 160),
    trustLevel: text(raw.trust_level, 40),
    ...(path ? { path } : {}),
  };
}

function surfacePayload(item: TemplateCatalogMetadataItem): JsonRecord {
  return record(item.surface) ?? item;
}

function valueAtPathOrLiteral(state: JsonRecord, config: JsonRecord, pathKey: string, literalKey: string): unknown {
  const configuredPath = config[pathKey];
  if (configuredPath !== undefined) return readStatusSurfacePath(state, configuredPath);
  return config[literalKey];
}

function normalizeOptions(raw: unknown): StatusSurfaceOption[] {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, MAX_OPTIONS).flatMap((candidate) => {
    if (typeof candidate === "string" || typeof candidate === "number") {
      const value = text(candidate, 160);
      return value ? [{ value, label: value }] : [];
    }
    const item = record(candidate);
    if (!item) return [];
    const value = text(item.value ?? item.id, 160);
    const label = text(item.label ?? item.name ?? value, 200);
    if (!value || !label) return [];
    return [{
      value,
      label,
      disabled: booleanValue(item.disabled),
      disabledReason: text(item.disabled_reason ?? item.disabledReason, 240),
    }];
  });
}

function normalizeControls(
  rawControls: unknown,
  state: JsonRecord,
  registeredActions: Set<string>,
  surfaceId: string,
  raw: TemplateCatalogMetadataItem,
  diagnostics: StatusSurfaceDiagnostic[],
): ResolvedStatusSurfaceControl[] {
  if (rawControls === undefined) return [];
  if (!Array.isArray(rawControls)) {
    diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_controls", "controls must be an array", "controls"));
    return [];
  }

  return rawControls.slice(0, 20).flatMap((candidate, index) => {
    const control = record(candidate);
    if (!control) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_control", "control must be an object", `controls.${index}`));
      return [];
    }
    const kind = text(control.type, 40) as StatusSurfaceControlKind | undefined;
    if (!kind || !CONTROL_KINDS.has(kind)) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.unknown_control", `unsupported control: ${kind ?? "missing"}`, `controls.${index}.type`));
      return [];
    }
    const id = safeId(control.id) ?? `${kind}_${index}`;
    const actionId = safeId(control.action_id ?? control.actionId);
    if (ACTION_CONTROL_KINDS.has(kind) && (!actionId || !registeredActions.has(actionId))) {
      diagnostics.push(diagnostic(
        surfaceId,
        raw,
        "status_surface.unregistered_action",
        `control ${id} references an unregistered action`,
        `controls.${index}.action_id`,
      ));
      return [];
    }
    const valuePath = control.value_path ?? control.valuePath;
    if (valuePath !== undefined && !safePath(valuePath)) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", `control ${id} has an invalid value path`, `controls.${index}.value_path`));
      return [];
    }
    const disabledPath = control.disabled_path ?? control.disabledPath;
    if (disabledPath !== undefined && !safePath(disabledPath)) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", `control ${id} has an invalid disabled path`, `controls.${index}.disabled_path`));
      return [];
    }
    const optionsPath = control.options_path ?? control.optionsPath;
    if (optionsPath !== undefined && !safePath(optionsPath)) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", `control ${id} has an invalid options path`, `controls.${index}.options_path`));
      return [];
    }
    const currentValue = valuePath === undefined ? control.value : readStatusSurfacePath(state, valuePath);
    const options = normalizeOptions(optionsPath === undefined ? control.options : readStatusSurfacePath(state, optionsPath));
    return [{
      id,
      type: kind,
      label: text(control.label, 160) ?? kind.replace(/_/g, " "),
      icon: safeId(control.icon),
      actionId,
      value: typeof currentValue === "boolean" || typeof currentValue === "number"
        ? currentValue
        : text(currentValue, 160) ?? null,
      disabled: booleanValue(control.disabled) || booleanValue(readStatusSurfacePath(state, disabledPath)),
      disabledReason: text(control.disabled_reason ?? control.disabledReason, 240),
      options,
    }];
  });
}

function visibleForState(
  visibleWhen: unknown,
  state: JsonRecord,
  surfaceId: string,
  raw: TemplateCatalogMetadataItem,
  diagnostics: StatusSurfaceDiagnostic[],
): boolean {
  if (visibleWhen === undefined) return true;
  const predicate = record(visibleWhen);
  if (!predicate) {
    diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_visibility", "visible_when must be an object", "visible_when"));
    return true;
  }
  for (const [path, expected] of Object.entries(predicate)) {
    if (!safePath(path)) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", `invalid visibility path: ${path}`, `visible_when.${path}`));
      return true;
    }
    const actual = readStatusSurfacePath(state, path);
    const allowed = Array.isArray(expected) ? expected : [expected];
    if (!allowed.some((value) => Object.is(value, actual))) return false;
  }
  return true;
}

function normalizeDetails(config: JsonRecord, state: JsonRecord, diagnostics: StatusSurfaceDiagnostic[], surfaceId: string, raw: TemplateCatalogMetadataItem) {
  const configured = config.details;
  if (!Array.isArray(configured)) return [];
  return configured.slice(0, MAX_DETAILS).flatMap((candidate, index) => {
    if (typeof candidate === "string" || typeof candidate === "number") {
      const value = text(candidate);
      return value ? [{ value }] : [];
    }
    const detail = record(candidate);
    if (!detail) return [];
    const path = detail.path;
    if (path !== undefined && !safePath(path)) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", "invalid detail path", `details.${index}.path`));
      return [];
    }
    const value = text(path === undefined ? detail.value : readStatusSurfacePath(state, path));
    return value ? [{ label: text(detail.label, 120), value }] : [];
  });
}

function severityFor(config: JsonRecord, state: JsonRecord): ResolvedStatusSurface["severity"] {
  const explicit = text(valueAtPathOrLiteral(state, config, "severity_path", "severity"), 20)?.toLowerCase();
  if (explicit === "error" || explicit === "warning" || explicit === "success" || explicit === "neutral") return explicit;
  const status = text(valueAtPathOrLiteral(state, config, "status_path", "status"), 80)?.toLowerCase();
  if (status && ["error", "failed", "blocked", "cancelled"].includes(status)) return "error";
  if (status && ["warning", "paused", "waiting", "pending"].includes(status)) return "warning";
  if (status && ["success", "complete", "completed", "ready"].includes(status)) return "success";
  return "neutral";
}

function fallbackSurface(
  surfaceId: string,
  slot: StatusSurfaceSlot,
  raw: TemplateCatalogMetadataItem,
  diagnostics: StatusSurfaceDiagnostic[],
  priority: number,
  order: number,
): ResolvedStatusSurface {
  return {
    id: surfaceId,
    apiVersion: STATUS_SURFACE_API_VERSION,
    slot,
    priority,
    order,
    icon: "warning",
    title: "Unsupported status surface",
    summary: diagnostics[0]?.message ?? "This status surface could not be rendered safely.",
    severity: "error",
    details: diagnostics.map((item) => ({ label: item.code, value: item.message })),
    controls: [],
    templateId: text(raw.template_id, 160),
    sourcePackId: text(raw.source_pack_id ?? record(raw.origin)?.pack_id, 160),
    trustLevel: text(raw.trust_level, 40),
    diagnostics,
    unsupported: true,
  };
}

function resolveOne(
  raw: TemplateCatalogMetadataItem,
  sources: Map<string, TemplateCatalogMetadataItem>,
  registeredActions: Set<string>,
): ResolvedStatusSurface | null {
  const config = surfacePayload(raw);
  const surfaceId = safeId(config.surface_id ?? config.id ?? raw.id) ?? `invalid_${text(raw.piece_id, 64) ?? "surface"}`;
  const slotValue = text(config.slot ?? raw.slot, 40) ?? "above_composer";
  if (!STATUS_SURFACE_SLOTS.includes(slotValue as StatusSurfaceSlot)) return null;
  const slot = slotValue as StatusSurfaceSlot;
  const priority = numberValue(config.priority) ?? 0;
  const order = numberValue(config.order ?? raw.order) ?? 0;
  const diagnostics: StatusSurfaceDiagnostic[] = [];
  const apiVersion = text(config.api_version ?? config.apiVersion, 80) ?? STATUS_SURFACE_API_VERSION;
  if (apiVersion !== STATUS_SURFACE_API_VERSION) {
    diagnostics.push(diagnostic(surfaceId, raw, "status_surface.incompatible_version", `unsupported API version: ${apiVersion}`, "api_version"));
  }
  if (!safeId(config.surface_id ?? config.id ?? raw.id)) {
    diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_id", "surface ID must be an opaque registry ID", "id"));
  }

  const configuredDataSource = config.data_source ?? config.dataSource;
  const dataSourceId = safeId(configuredDataSource);
  const source = dataSourceId ? sources.get(dataSourceId) : undefined;
  if (configuredDataSource !== undefined && !dataSourceId) {
    diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_data_source", "data source must be an opaque registered ID", "data_source"));
  }
  if (dataSourceId && !source) {
    diagnostics.push(diagnostic(surfaceId, raw, "status_surface.unregistered_data_source", `unregistered data source: ${dataSourceId}`, "data_source"));
  }
  const inlineState = record(config.snapshot) ?? record(config.data) ?? record(config.state);
  const state = dataSourceSnapshot(source) ?? inlineState ?? {};
  const visible = visibleForState(config.visible_when ?? config.visibleWhen, state, surfaceId, raw, diagnostics);
  if (!visible && diagnostics.length === 0) return null;

  const pathKeys = ["title_path", "summary_path", "status_path", "severity_path", "timer_from_path", "count_path"];
  for (const pathKey of pathKeys) {
    if (config[pathKey] !== undefined && !safePath(config[pathKey])) {
      diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", `invalid ${pathKey}`, pathKey));
    }
  }
  const progressConfig = record(config.progress);
  if (progressConfig) {
    for (const pathKey of ["current_path", "total_path", "label_path"]) {
      if (progressConfig[pathKey] !== undefined && !safePath(progressConfig[pathKey])) {
        diagnostics.push(diagnostic(surfaceId, raw, "status_surface.invalid_path", `invalid progress ${pathKey}`, `progress.${pathKey}`));
      }
    }
  }

  const controls = normalizeControls(config.controls, state, registeredActions, surfaceId, raw, diagnostics);
  const details = normalizeDetails(config, state, diagnostics, surfaceId, raw);
  if (diagnostics.length > 0) return fallbackSurface(surfaceId, slot, raw, diagnostics, priority, order);

  const current = numberValue(progressConfig ? readStatusSurfacePath(state, progressConfig.current_path) : undefined);
  const total = numberValue(progressConfig ? readStatusSurfacePath(state, progressConfig.total_path) : undefined);
  const progress = current !== undefined && total !== undefined && total > 0
    ? { current: Math.max(0, current), total, label: text(readStatusSurfacePath(state, progressConfig?.label_path), 120) }
    : undefined;
  const title = text(valueAtPathOrLiteral(state, config, "title_path", "title"), 200) ?? surfaceId;
  const status = text(valueAtPathOrLiteral(state, config, "status_path", "status"), 120);
  const count = numberValue(valueAtPathOrLiteral(state, config, "count_path", "count"));
  return {
    id: surfaceId,
    apiVersion: STATUS_SURFACE_API_VERSION,
    slot,
    priority,
    order,
    icon: safeId(config.icon),
    title,
    summary: text(valueAtPathOrLiteral(state, config, "summary_path", "summary")),
    status,
    severity: severityFor(config, state),
    startedAt: text(valueAtPathOrLiteral(state, config, "timer_from_path", "timer_from"), 80),
    progress,
    count: count === undefined ? undefined : Math.max(0, count),
    details,
    controls,
    dataSourceId,
    sourceRevision: text(source?.revision ?? source?.source_revision ?? state.revision, 160),
    templateId: text(raw.template_id, 160),
    sourcePackId: text(raw.source_pack_id ?? record(raw.origin)?.pack_id, 160),
    trustLevel: text(raw.trust_level, 40),
    diagnostics: [],
    unsupported: false,
  };
}

export function resolveStatusSurfaces(catalog: UICatalog | null | undefined): StatusSurfaceResolution {
  const diagnostics: StatusSurfaceDiagnostic[] = [];
  const sources = new Map<string, TemplateCatalogMetadataItem>();
  for (const source of catalog?.data_sources ?? []) {
    for (const id of sourceIdentity(source)) {
      if (!sources.has(id)) sources.set(id, source);
    }
  }
  const registeredActions = new Set<string>();
  for (const command of catalog?.commands ?? []) {
    for (const id of actionIdentity(command)) registeredActions.add(id);
  }
  for (const action of catalog?.actions ?? []) {
    for (const id of executableActionIdentity(action)) registeredActions.add(id);
  }

  const candidates = (catalog?.status_surfaces ?? [])
    .filter((item) => item.enabled !== false)
    .map((item) => resolveOne(item, sources, registeredActions))
    .filter((item): item is ResolvedStatusSurface => item !== null)
    .sort((left, right) => right.priority - left.priority || left.order - right.order || left.id.localeCompare(right.id));

  const surfaces: ResolvedStatusSurface[] = [];
  const bySlotAndId = new Map<string, ResolvedStatusSurface>();
  for (const surface of candidates) {
    const key = `${surface.slot}:${surface.id}`;
    const existing = bySlotAndId.get(key);
    if (existing) {
      const collision = diagnostic(
        surface.id,
        { template_id: surface.templateId, source_pack_id: surface.sourcePackId, trust_level: surface.trustLevel },
        "status_surface.duplicate_id",
        `duplicate surface ID in slot ${surface.slot}; highest-priority declaration retained`,
      );
      diagnostics.push(collision);
      existing.diagnostics.push(collision);
      continue;
    }
    bySlotAndId.set(key, surface);
    surfaces.push(surface);
    diagnostics.push(...surface.diagnostics);
  }
  return { surfaces, diagnostics };
}

export function statusSurfacesForSlot(
  catalog: UICatalog | null | undefined,
  slot: StatusSurfaceSlot,
): ResolvedStatusSurface[] {
  return resolveStatusSurfaces(catalog).surfaces.filter((surface) => surface.slot === slot);
}
