import type { ModelSearchItem, SettingsSection } from "../../lib/api";

export const MODEL_PICKER_QUERY_RESULT_LIMIT = 60;

export type ModelSelectOption = {
  value: string;
  label: string;
  provider_id?: string;
  provider_display_name?: string;
  model_id?: string;
  qualified_model_id?: string;
  requires_api_key?: boolean;
  api_key_required?: boolean;
  api_key_configured?: boolean;
  configured?: boolean;
  local?: boolean;
  supports_vision?: boolean;
  supports_image_input?: boolean;
  supports_tool_calling?: boolean;
  supports_thinking?: boolean;
  supports_fast?: boolean;
  speed_tier?: string;
  quality_tier?: string;
  cost_tier?: string;
  knowledge_level?: number;
  capability_tags?: string[];
  recommended_roles?: string[];
  notes?: string;
};

export type ModelSelectBadge =
  | "configured"
  | "api-key-needed"
  | "local"
  | "vision"
  | "tools"
  | "thinking"
  | "fast"
  | "cost";

export type ModelSelectBadgeDescriptor = {
  id: ModelSelectBadge;
  label: string;
};

export type ModelSelectDisplay = {
  label: string;
  subtitle: string;
  badges: ModelSelectBadgeDescriptor[];
  providerLabel: string;
  modelLabel: string;
  requiresApiKey: boolean;
  apiKeyConfigured: boolean;
};

export type ModelProviderOption = {
  provider_id: string;
  label: string;
  model_count: number;
};

export type ModelProviderQueryState = {
  active: boolean;
  providerQuery: string;
  providerId: string;
  modelQuery: string;
};

type SettingsFieldOption = NonNullable<SettingsSection["fields"][number]["options"]>[number];

export function modelFieldOptionToModelSelectOption(option: SettingsFieldOption): ModelSelectOption {
  const optionRecord = option as Record<string, unknown>;
  return {
    value: String(option.value ?? ""),
    label: String(option.label ?? option.value ?? ""),
    provider_id: option.provider_id,
    provider_display_name: option.provider_display_name,
    model_id: option.model_id,
    qualified_model_id: option.qualified_model_id,
    requires_api_key: Boolean(optionRecord.requires_api_key),
    api_key_required: Boolean(optionRecord.api_key_required),
    api_key_configured: Boolean(optionRecord.api_key_configured),
    configured: option.configured,
    local: option.local,
    supports_vision: option.supports_vision,
    supports_image_input: option.supports_image_input,
    supports_tool_calling: option.supports_tool_calling,
    supports_thinking: option.supports_thinking,
    supports_fast: option.supports_fast,
    speed_tier: option.speed_tier,
    quality_tier: option.quality_tier,
    cost_tier: option.cost_tier,
    knowledge_level: option.knowledge_level,
    capability_tags: option.capability_tags,
    recommended_roles: option.recommended_roles,
    notes: option.notes,
  };
}

export function modelSearchItemToModelSelectOption(item: ModelSearchItem): ModelSelectOption {
  const providerId = String(item.provider_id ?? "").trim();
  const modelId = String(item.model_id ?? "").trim();
  const fallbackValue = providerId && modelId ? `${providerId}/${modelId}` : "";
  const value = String(item.profile_id ?? item.qualified_model_id ?? fallbackValue).trim();
  return {
    value,
    label: String(item.label ?? item.display_name ?? value).trim() || value,
    provider_id: item.provider_id,
    provider_display_name: item.provider_display_name,
    model_id: item.model_id,
    qualified_model_id: item.qualified_model_id,
    requires_api_key: item.requires_api_key,
    api_key_required: item.api_key_required,
    api_key_configured: item.api_key_configured,
    configured: item.configured,
    local: Boolean(item.local),
    supports_vision: item.supports_vision,
    supports_image_input: item.supports_image_input,
    supports_tool_calling: item.supports_tool_calling,
    supports_thinking: item.supports_thinking,
    supports_fast: item.supports_fast,
    speed_tier: item.speed_tier,
    quality_tier: item.quality_tier,
    cost_tier: item.cost_tier,
    knowledge_level: item.knowledge_level,
    capability_tags: item.capability_tags,
    recommended_roles: item.recommended_roles,
    notes: item.notes,
  };
}

export function modelSelectOptionSearchText(option: ModelSelectOption): string {
  return [
    option.value,
    option.label,
    option.provider_id,
    option.provider_display_name,
    option.model_id,
    option.qualified_model_id,
    option.speed_tier,
    option.quality_tier,
    option.cost_tier,
    option.notes,
    ...(option.capability_tags ?? []),
    ...(option.recommended_roles ?? []),
  ].filter(Boolean).join(" ").toLowerCase();
}

export function normalizeModelSearchText(value: string): string {
  return value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

export function modelSelectOptionMatchesSearch(option: ModelSelectOption, query: string): boolean {
  const rawText = modelSelectOptionSearchText(option);
  const normalizedText = normalizeModelSearchText(rawText);
  const rawQuery = query.trim().toLowerCase();
  const normalizedQuery = normalizeModelSearchText(rawQuery);
  if (!normalizedQuery) return true;
  if (rawText.includes(rawQuery) || normalizedText.includes(normalizedQuery)) return true;
  return normalizedQuery.split(/\s+/).every((token) => normalizedText.includes(token) || rawText.includes(token));
}

function normalizedProviderId(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

export function modelProviderOptions(options: ModelSelectOption[]): ModelProviderOption[] {
  const providers = new Map<string, ModelProviderOption>();
  for (const option of options) {
    const providerId = String(option.provider_id ?? "").trim();
    if (!providerId) continue;
    const key = normalizedProviderId(providerId);
    const current = providers.get(key);
    if (current) {
      current.model_count += 1;
      continue;
    }
    providers.set(key, {
      provider_id: providerId,
      label: String(option.provider_display_name ?? providerId).trim() || providerId,
      model_count: 1,
    });
  }
  return [...providers.values()].sort((left, right) => left.label.localeCompare(right.label));
}

export function parseModelProviderQuery(
  query: string,
  providers: ModelProviderOption[],
  trigger = "@",
): ModelProviderQueryState {
  const raw = String(query ?? "");
  if (!trigger || !raw.startsWith(trigger)) {
    return { active: false, providerQuery: "", providerId: "", modelQuery: raw.trim() };
  }
  const afterTrigger = raw.slice(trigger.length);
  const whitespaceIndex = afterTrigger.search(/\s/);
  const providerQuery = (whitespaceIndex < 0 ? afterTrigger : afterTrigger.slice(0, whitespaceIndex)).trim();
  const modelQuery = whitespaceIndex < 0 ? "" : afterTrigger.slice(whitespaceIndex).trim();
  const normalizedQuery = normalizedProviderId(providerQuery);
  const exact = providers.find((provider) => (
    normalizedProviderId(provider.provider_id) === normalizedQuery
    || normalizedProviderId(provider.label) === normalizedQuery
  ));
  return {
    active: whitespaceIndex < 0,
    providerQuery,
    providerId: exact?.provider_id ?? (whitespaceIndex >= 0 ? providerQuery : ""),
    modelQuery,
  };
}

export function filterModelProviderOptions(
  providers: ModelProviderOption[],
  query: string,
): ModelProviderOption[] {
  const normalizedQuery = normalizeModelSearchText(query);
  if (!normalizedQuery) return providers;
  return providers.filter((provider) => {
    const text = normalizeModelSearchText(`${provider.provider_id} ${provider.label}`);
    return normalizedQuery.split(/\s+/).every((token) => text.includes(token));
  });
}

export function filterModelOptionsByProvider(
  options: ModelSelectOption[],
  providerId: string,
): ModelSelectOption[] {
  const target = normalizedProviderId(providerId);
  if (!target) return options;
  return options.filter((option) => normalizedProviderId(option.provider_id) === target);
}

export function dedupeModelSelectOptions(options: ModelSelectOption[]): ModelSelectOption[] {
  const seen = new Set<string>();
  const deduped: ModelSelectOption[] = [];
  for (const option of options) {
    if (!option.value || seen.has(option.value)) continue;
    seen.add(option.value);
    deduped.push(option);
  }
  return deduped;
}

export function buildVisibleModelOptions({
  options,
  selected,
  remoteOptions,
  query,
  resultLimit = MODEL_PICKER_QUERY_RESULT_LIMIT,
}: {
  options: ModelSelectOption[];
  selected?: ModelSelectOption | null;
  remoteOptions?: ModelSelectOption[];
  query?: string;
  resultLimit?: number;
}): ModelSelectOption[] {
  const trimmedQuery = String(query ?? "").trim();
  const localMatches = trimmedQuery
    ? options.filter((option) => modelSelectOptionMatchesSearch(option, trimmedQuery))
    : options;
  const merged = dedupeModelSelectOptions([
    ...(selected ? [selected] : []),
    ...localMatches,
    ...(remoteOptions ?? []),
  ]);
  if (!trimmedQuery) return merged;
  return merged.slice(0, resultLimit);
}

export function modelOptionRequiresApiKey(option: ModelSelectOption): boolean {
  return Boolean(option.requires_api_key ?? option.api_key_required);
}

export function modelOptionApiKeyConfigured(option: ModelSelectOption): boolean {
  return Boolean(option.configured ?? option.api_key_configured ?? option.local);
}

export function modelOptionBadges(option: ModelSelectOption): ModelSelectBadgeDescriptor[] {
  const badges: ModelSelectBadgeDescriptor[] = [];
  if (modelOptionApiKeyConfigured(option)) badges.push({ id: "configured", label: "設定済み" });
  if (modelOptionRequiresApiKey(option) && !modelOptionApiKeyConfigured(option)) {
    badges.push({ id: "api-key-needed", label: "API key必要" });
  }
  if (option.local) badges.push({ id: "local", label: "ローカル" });
  if (option.supports_vision || option.supports_image_input) badges.push({ id: "vision", label: "画像" });
  if (option.supports_tool_calling) badges.push({ id: "tools", label: "ツール" });
  if (option.supports_thinking) badges.push({ id: "thinking", label: "推論" });
  if (option.supports_fast || option.speed_tier === "fast") badges.push({ id: "fast", label: "高速" });
  if (option.cost_tier && option.cost_tier !== "unknown") badges.push({ id: "cost", label: option.cost_tier });
  return badges.slice(0, 4);
}

export function modelSelectDisplay(option: ModelSelectOption): ModelSelectDisplay {
  const providerLabel = String(option.provider_display_name ?? option.provider_id ?? "").trim();
  const modelLabel = String(option.model_id ?? option.qualified_model_id ?? option.value).trim();
  return {
    label: option.label || option.value,
    subtitle: [option.provider_id, modelLabel].filter(Boolean).join(" / "),
    badges: modelOptionBadges(option),
    providerLabel,
    modelLabel,
    requiresApiKey: modelOptionRequiresApiKey(option),
    apiKeyConfigured: modelOptionApiKeyConfigured(option),
  };
}

export function findSelectedModelOption(
  options: ModelSelectOption[],
  value: string,
  remoteOptions: ModelSelectOption[] = [],
): ModelSelectOption | null {
  return options.find((option) => option.value === value || option.qualified_model_id === value)
    ?? remoteOptions.find((option) => option.value === value || option.qualified_model_id === value)
    ?? (value ? { value, label: value } : null);
}

export function parseModelAllowlist(value: unknown, fallback?: unknown): string[] {
  const source = value ?? fallback ?? "";
  const items = Array.isArray(source)
    ? source.map((item) => String(item ?? ""))
    : String(source).split(/\r?\n|,/);
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)));
}

export function serializeModelAllowlist(items: string[]): string {
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean))).join("\n");
}
