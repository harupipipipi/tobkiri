import type { ModelProfile, UICatalog } from "../../lib/api";
import type { ModelSelectOption } from "./modelSelect";

export type ModelSelectorSurface = "composer" | "settings";

export type ModelSelectorLayout = {
  placement: "auto" | "above" | "below";
  group_by: "provider" | "none";
  selected_position: "first" | "natural";
  provider_trigger: string;
  provider_confirm_key: "Tab" | "Enter";
  model_confirm_keys: string[];
  max_visible_options: number;
  show_search: boolean;
  trigger_height_px: number;
  popover_width_px: number;
  popover_max_height_px: number;
  show_provider_count: boolean;
  show_capability_tags: boolean;
};

export type ModelSelectorFilters = {
  include_model_ids: string[];
  exclude_model_ids: string[];
  include_provider_ids: string[];
  exclude_provider_ids: string[];
  require_tags: string[];
  require_tag_mode: "all" | "any";
  exclude_tags: string[];
  hide_unconfigured: boolean;
  hide_unavailable: boolean;
  hide_models_without_provider: boolean;
};

export type ModelSelectorSchema = {
  version: 1;
  policy_scope: "presentation_only";
  dynamic_state_authority: "backend_runtime";
  layout: ModelSelectorLayout;
  filters: ModelSelectorFilters;
  surfaces: Partial<Record<ModelSelectorSurface, {
    layout?: Partial<ModelSelectorLayout>;
    filters?: Partial<ModelSelectorFilters>;
  }>>;
};

export const DEFAULT_MODEL_SELECTOR_SCHEMA: ModelSelectorSchema = {
  version: 1,
  policy_scope: "presentation_only",
  dynamic_state_authority: "backend_runtime",
  layout: {
    placement: "auto",
    group_by: "provider",
    selected_position: "first",
    provider_trigger: "@",
    provider_confirm_key: "Tab",
    model_confirm_keys: ["Enter", "Tab"],
    max_visible_options: 60,
    show_search: true,
    trigger_height_px: 44,
    popover_width_px: 420,
    popover_max_height_px: 420,
    show_provider_count: true,
    show_capability_tags: true,
  },
  filters: {
    include_model_ids: [],
    exclude_model_ids: [],
    include_provider_ids: [],
    exclude_provider_ids: [],
    require_tags: [],
    require_tag_mode: "all",
    exclude_tags: [],
    hide_unconfigured: false,
    hide_unavailable: false,
    hide_models_without_provider: true,
  },
  surfaces: {},
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => String(item ?? "").trim()).filter(Boolean))];
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function positiveInteger(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function layout(value: unknown, fallback: ModelSelectorLayout): ModelSelectorLayout {
  const raw = record(value);
  const placement = ["auto", "above", "below"].includes(String(raw.placement))
    ? String(raw.placement) as ModelSelectorLayout["placement"]
    : fallback.placement;
  const groupBy = ["provider", "none"].includes(String(raw.group_by))
    ? String(raw.group_by) as ModelSelectorLayout["group_by"]
    : fallback.group_by;
  const selectedPosition = ["first", "natural"].includes(String(raw.selected_position))
    ? String(raw.selected_position) as ModelSelectorLayout["selected_position"]
    : fallback.selected_position;
  return {
    placement,
    group_by: groupBy,
    selected_position: selectedPosition,
    provider_trigger: String(raw.provider_trigger ?? fallback.provider_trigger).trim() || "@",
    provider_confirm_key: raw.provider_confirm_key === "Enter" ? "Enter" : fallback.provider_confirm_key,
    model_confirm_keys: strings(raw.model_confirm_keys).length > 0
      ? strings(raw.model_confirm_keys)
      : fallback.model_confirm_keys,
    max_visible_options: positiveInteger(raw.max_visible_options, fallback.max_visible_options),
    show_search: bool(raw.show_search, fallback.show_search),
    trigger_height_px: positiveInteger(raw.trigger_height_px, fallback.trigger_height_px),
    popover_width_px: positiveInteger(raw.popover_width_px, fallback.popover_width_px),
    popover_max_height_px: positiveInteger(raw.popover_max_height_px, fallback.popover_max_height_px),
    show_provider_count: bool(raw.show_provider_count, fallback.show_provider_count),
    show_capability_tags: bool(raw.show_capability_tags, fallback.show_capability_tags),
  };
}

function filters(value: unknown, fallback: ModelSelectorFilters): ModelSelectorFilters {
  const raw = record(value);
  return {
    include_model_ids: raw.include_model_ids === undefined ? fallback.include_model_ids : strings(raw.include_model_ids),
    exclude_model_ids: raw.exclude_model_ids === undefined ? fallback.exclude_model_ids : strings(raw.exclude_model_ids),
    include_provider_ids: raw.include_provider_ids === undefined ? fallback.include_provider_ids : strings(raw.include_provider_ids),
    exclude_provider_ids: raw.exclude_provider_ids === undefined ? fallback.exclude_provider_ids : strings(raw.exclude_provider_ids),
    require_tags: raw.require_tags === undefined ? fallback.require_tags : strings(raw.require_tags),
    require_tag_mode: raw.require_tag_mode === "any" ? "any" : fallback.require_tag_mode,
    exclude_tags: raw.exclude_tags === undefined ? fallback.exclude_tags : strings(raw.exclude_tags),
    hide_unconfigured: bool(raw.hide_unconfigured, fallback.hide_unconfigured),
    hide_unavailable: bool(raw.hide_unavailable, fallback.hide_unavailable),
    hide_models_without_provider: bool(raw.hide_models_without_provider, fallback.hide_models_without_provider),
  };
}

function layoutOverride(value: unknown): Partial<ModelSelectorLayout> {
  const raw = record(value);
  const parsed = layout(raw, DEFAULT_MODEL_SELECTOR_SCHEMA.layout);
  const result: Partial<ModelSelectorLayout> = {};
  if ("placement" in raw) result.placement = parsed.placement;
  if ("group_by" in raw) result.group_by = parsed.group_by;
  if ("selected_position" in raw) result.selected_position = parsed.selected_position;
  if ("provider_trigger" in raw) result.provider_trigger = parsed.provider_trigger;
  if ("provider_confirm_key" in raw) result.provider_confirm_key = parsed.provider_confirm_key;
  if ("model_confirm_keys" in raw) result.model_confirm_keys = parsed.model_confirm_keys;
  if ("max_visible_options" in raw) result.max_visible_options = parsed.max_visible_options;
  if ("show_search" in raw) result.show_search = parsed.show_search;
  if ("trigger_height_px" in raw) result.trigger_height_px = parsed.trigger_height_px;
  if ("popover_width_px" in raw) result.popover_width_px = parsed.popover_width_px;
  if ("popover_max_height_px" in raw) result.popover_max_height_px = parsed.popover_max_height_px;
  if ("show_provider_count" in raw) result.show_provider_count = parsed.show_provider_count;
  if ("show_capability_tags" in raw) result.show_capability_tags = parsed.show_capability_tags;
  return result;
}

function filterOverride(value: unknown): Partial<ModelSelectorFilters> {
  const raw = record(value);
  const parsed = filters(raw, DEFAULT_MODEL_SELECTOR_SCHEMA.filters);
  const result: Partial<ModelSelectorFilters> = {};
  if ("include_model_ids" in raw) result.include_model_ids = parsed.include_model_ids;
  if ("exclude_model_ids" in raw) result.exclude_model_ids = parsed.exclude_model_ids;
  if ("include_provider_ids" in raw) result.include_provider_ids = parsed.include_provider_ids;
  if ("exclude_provider_ids" in raw) result.exclude_provider_ids = parsed.exclude_provider_ids;
  if ("require_tags" in raw) result.require_tags = parsed.require_tags;
  if ("require_tag_mode" in raw) result.require_tag_mode = parsed.require_tag_mode;
  if ("exclude_tags" in raw) result.exclude_tags = parsed.exclude_tags;
  if ("hide_unconfigured" in raw) result.hide_unconfigured = parsed.hide_unconfigured;
  if ("hide_unavailable" in raw) result.hide_unavailable = parsed.hide_unavailable;
  if ("hide_models_without_provider" in raw) result.hide_models_without_provider = parsed.hide_models_without_provider;
  return result;
}

export function parseModelSelectorSchema(value: unknown): ModelSelectorSchema {
  const raw = record(value);
  const rawSurfaces = record(raw.surfaces);
  const surfaces: ModelSelectorSchema["surfaces"] = {};
  for (const surface of ["composer", "settings"] as const) {
    const rawSurface = record(rawSurfaces[surface]);
    if (Object.keys(rawSurface).length === 0) continue;
    surfaces[surface] = {
      layout: layoutOverride(rawSurface.layout),
      filters: filterOverride(rawSurface.filters),
    };
  }
  return {
    version: 1,
    policy_scope: "presentation_only",
    dynamic_state_authority: "backend_runtime",
    layout: layout(raw.layout, DEFAULT_MODEL_SELECTOR_SCHEMA.layout),
    filters: filters(raw.filters, DEFAULT_MODEL_SELECTOR_SCHEMA.filters),
    surfaces,
  };
}

export function modelSelectorSchemaFromCatalog(catalog: UICatalog | null): ModelSelectorSchema {
  const template = (catalog?.templates ?? []).find((item) => item.id === "rumi.model_selector.default");
  return parseModelSelectorSchema(record(template?.metadata).selector_schema);
}

export function modelSelectorSchemaForSurface(
  schema: ModelSelectorSchema,
  surface: ModelSelectorSurface,
): ModelSelectorSchema {
  const override = schema.surfaces[surface];
  if (!override) return schema;
  return {
    ...schema,
    layout: { ...schema.layout, ...(override.layout ?? {}) },
    filters: { ...schema.filters, ...(override.filters ?? {}) },
  };
}

function normalized(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

function wildcardMatch(value: string, pattern: string): boolean {
  const escaped = normalized(pattern).replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`, "i").test(value);
}

function matchesAny(values: string[], patterns: string[]): boolean {
  return patterns.some((pattern) => values.some((value) => wildcardMatch(value, pattern)));
}

type SelectableModel = {
  providerIds: string[];
  modelIds: string[];
  tags: string[];
  configured: boolean;
  available: boolean;
};

function selectableProfile(profile: ModelProfile): SelectableModel {
  const availability = record(profile.availability);
  return {
    providerIds: [profile.provider_id, profile.provider_display_name].map(normalized).filter(Boolean),
    modelIds: [profile.profile_id, profile.qualified_model_id, profile.model_id].map(normalized).filter(Boolean),
    tags: [
      ...(profile.capability_tags ?? []),
      ...(profile.recommended_roles ?? []),
      profile.supports_thinking ? "thinking" : "",
      profile.supports_vision || profile.supports_image_input ? "vision" : "",
      profile.supports_tool_calling ? "tools" : "",
      profile.supports_fast ? "fast" : "",
      profile.cost_tier,
      profile.speed_tier,
      profile.quality_tier,
    ].map(normalized).filter(Boolean),
    configured: Boolean(profile.local || availability.configured || availability.active),
    available: availability.available !== false && availability.status !== "unavailable",
  };
}

function selectableOption(option: ModelSelectOption): SelectableModel {
  const availability = record(option.availability);
  return {
    providerIds: [option.provider_id, option.provider_display_name].map(normalized).filter(Boolean),
    modelIds: [option.value, option.qualified_model_id, option.model_id].map(normalized).filter(Boolean),
    tags: [
      ...(option.capability_tags ?? []),
      ...(option.recommended_roles ?? []),
      option.supports_thinking ? "thinking" : "",
      option.supports_vision || option.supports_image_input ? "vision" : "",
      option.supports_tool_calling ? "tools" : "",
      option.supports_fast ? "fast" : "",
      option.cost_tier,
      option.speed_tier,
      option.quality_tier,
    ].map(normalized).filter(Boolean),
    configured: Boolean(option.local || option.configured || option.api_key_configured),
    available: availability.available !== false && availability.status !== "unavailable",
  };
}

function isVisible(model: SelectableModel, filter: ModelSelectorFilters): boolean {
  if (filter.hide_models_without_provider && model.providerIds.length === 0) return false;
  if (filter.include_provider_ids.length > 0 && !matchesAny(model.providerIds, filter.include_provider_ids)) return false;
  if (matchesAny(model.providerIds, filter.exclude_provider_ids)) return false;
  if (filter.include_model_ids.length > 0 && !matchesAny(model.modelIds, filter.include_model_ids)) return false;
  if (matchesAny(model.modelIds, filter.exclude_model_ids)) return false;
  if (filter.exclude_tags.some((tag) => model.tags.includes(normalized(tag)))) return false;
  if (filter.require_tags.length > 0) {
    const matches = filter.require_tags.map((tag) => model.tags.includes(normalized(tag)));
    if (filter.require_tag_mode === "any" ? !matches.some(Boolean) : !matches.every(Boolean)) return false;
  }
  if (filter.hide_unconfigured && !model.configured) return false;
  if (filter.hide_unavailable && !model.available) return false;
  return true;
}

export function filterModelProfilesBySelector(
  profiles: ModelProfile[],
  schema: ModelSelectorSchema,
  surface: ModelSelectorSurface,
): ModelProfile[] {
  const resolved = modelSelectorSchemaForSurface(schema, surface);
  return profiles.filter((profile) => isVisible(selectableProfile(profile), resolved.filters));
}

export function filterModelOptionsBySelector(
  options: ModelSelectOption[],
  schema: ModelSelectorSchema,
  surface: ModelSelectorSurface,
): ModelSelectOption[] {
  const resolved = modelSelectorSchemaForSurface(schema, surface);
  return options.filter((option) => isVisible(selectableOption(option), resolved.filters));
}

export function filterProvidersBySelector<T extends { provider_id: string }>(
  providers: T[],
  schema: ModelSelectorSchema,
  surface: ModelSelectorSurface,
): T[] {
  const resolved = modelSelectorSchemaForSurface(schema, surface);
  return providers.filter((provider) => {
    const ids = [normalized(provider.provider_id)];
    if (resolved.filters.include_provider_ids.length > 0
      && !matchesAny(ids, resolved.filters.include_provider_ids)) return false;
    return !matchesAny(ids, resolved.filters.exclude_provider_ids);
  });
}
