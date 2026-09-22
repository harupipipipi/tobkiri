export const BUILTIN_API_PROVIDER_IDS: string[] = [
  "anthropic",
  "avian",
  "cerebras",
  "deepseek",
  "deepinfra",
  "fireworks",
  "friendli",
  "gitlawb-opengateway",
  "glm",
  "google",
  "groq",
  "hyperbolic",
  "inference-net",
  "llama_cpp",
  "lmstudio",
  "longcat",
  "mistral",
  "moonshotai",
  "nvidia",
  "nebius",
  "novita",
  "ollama",
  "opencode-go",
  "opencode-zen",
  "openai",
  "openai_compatible",
  "openrouter",
  "perplexity",
  "sambanova",
  "together",
  "upstage",
  "vllm",
  "xai",
  "xiaomi-token-plan-ams",
  "xiaomi-token-plan-cn",
  "xiaomi-token-plan-sgp",
];

export const BUILTIN_EXTERNAL_PROVIDER_IDS: string[] = [
  "cloudflare",
  "codex",
  "discord",
  "generic",
  "github",
  "line",
  "slack",
  "web",
];

export type ApiProviderKind = "llm" | "custom";
export type ApiProviderScope = "all" | "llm" | "non_llm";

export type ApiProviderOption = {
  provider_id: string;
  label: string;
  kind: ApiProviderKind;
  builtin: boolean;
  oauth_supported?: boolean;
  oauth_connected?: boolean;
  oauth_client_configured?: boolean;
};

export type ApiProviderRow = Record<string, unknown> & {
  provider_id?: unknown;
  label?: unknown;
  kind?: unknown;
  builtin?: unknown;
  oauth?: unknown;
};

export type ApiKeySetupDraft = {
  provider_id: string;
  name: string;
  value: string;
  kind?: ApiProviderKind;
  base_url?: string;
  allowed_models?: string | string[];
  default_model?: string;
  quota_label?: string;
  notes?: string;
  credential_mode?: "api_key" | "none";
};

export type ApiKeySaveOptions = {
  apiId: string;
  name: string;
  kind: ApiProviderKind;
  baseUrl?: string;
  allowedModels?: string[];
  defaultModel?: string;
  quotaLabel?: string;
  notes?: string;
  credentialMode?: "api_key" | "none";
};

export type ApiKeySavePayload = {
  provider_id: string;
  value: string;
  options: ApiKeySaveOptions;
};

export type ApiKeySetupDiagnostic = {
  provider_id: string;
  name: string;
  kind: ApiProviderKind;
  has_secret: boolean;
  secret_length: number;
  has_base_url: boolean;
  allowed_model_count: number;
  has_default_model: boolean;
  has_quota_label: boolean;
  has_notes: boolean;
};

export function normalizeProviderKind(value: unknown): ApiProviderKind {
  return String(value ?? "").trim().toLowerCase() === "custom" ? "custom" : "llm";
}

function oauthMetadata(provider: ApiProviderRow): Pick<ApiProviderOption, "oauth_supported" | "oauth_connected" | "oauth_client_configured"> {
  const oauth = provider.oauth;
  if (!oauth || typeof oauth !== "object") return {};
  const oauthRow = oauth as Record<string, unknown>;
  return {
    oauth_supported: true,
    oauth_connected: Boolean(oauthRow.connected),
    oauth_client_configured: Boolean(oauthRow.client_configured),
  };
}

function providerOptionFromRow(provider: ApiProviderRow, builtinIds: string[], defaultKind: ApiProviderKind): ApiProviderOption | null {
  const providerId = String(provider.provider_id ?? "").trim();
  if (!providerId) return null;
  const builtin = Boolean(provider.builtin) || builtinIds.includes(providerId);
  return {
    provider_id: providerId,
    label: String(provider.label ?? providerId),
    kind: normalizeProviderKind(provider.kind ?? defaultKind),
    builtin,
    ...oauthMetadata(provider),
  };
}

export function collectApiProviderOptions(
  providers: ApiProviderRow[],
  options: {
    includeExternalBuiltins?: boolean;
    builtinProviderIds?: string[];
    builtinExternalProviderIds?: string[];
  } = {},
): ApiProviderOption[] {
  const builtinProviderIds = options.builtinProviderIds ?? BUILTIN_API_PROVIDER_IDS;
  const builtinExternalProviderIds = options.builtinExternalProviderIds ?? BUILTIN_EXTERNAL_PROVIDER_IDS;
  const builtinIds = options.includeExternalBuiltins === false
    ? builtinProviderIds
    : [...builtinProviderIds, ...builtinExternalProviderIds];
  const collected = new Map<string, ApiProviderOption>();

  // The backend commonly returns only configured providers.  Seed the complete
  // built-in catalog first, then let returned rows enrich it, so adding one key
  // never makes every other supported provider disappear from Settings.
  for (const providerId of builtinProviderIds) {
    collected.set(providerId, { provider_id: providerId, label: providerId, kind: "llm", builtin: true });
  }
  if (options.includeExternalBuiltins !== false) {
    for (const providerId of builtinExternalProviderIds) {
      collected.set(providerId, { provider_id: providerId, label: providerId, kind: "custom", builtin: true });
    }
  }
  for (const provider of providers) {
    const providerId = String(provider.provider_id ?? "").trim();
    const defaultKind: ApiProviderKind = builtinExternalProviderIds.includes(providerId) ? "custom" : "llm";
    const option = providerOptionFromRow(provider, builtinIds, defaultKind);
    if (option) collected.set(option.provider_id, option);
  }

  return sortProviderOptions(Array.from(collected.values()));
}

export function collectExternalProviderOptions(
  providers: ApiProviderRow[],
  builtinExternalProviderIds: string[] = BUILTIN_EXTERNAL_PROVIDER_IDS,
): ApiProviderOption[] {
  const collected = new Map<string, ApiProviderOption>();
  for (const providerId of builtinExternalProviderIds) {
    collected.set(providerId, { provider_id: providerId, label: providerId, kind: "custom", builtin: true });
  }
  for (const provider of providers) {
    const option = providerOptionFromRow(provider, builtinExternalProviderIds, "custom");
    if (!option) continue;
    collected.set(option.provider_id, { ...option, kind: "custom" });
  }
  return sortProviderOptions(Array.from(collected.values()));
}

export function sortProviderOptions(options: ApiProviderOption[]): ApiProviderOption[] {
  return [...options].sort((a, b) => {
    if (a.builtin !== b.builtin) return a.builtin ? -1 : 1;
    return a.provider_id.localeCompare(b.provider_id);
  });
}

export function filterApiProviderOptions(options: ApiProviderOption[], query: string): ApiProviderOption[] {
  const trimmedQuery = query.trim().toLowerCase();
  if (!trimmedQuery) return options;
  return options.filter((option) =>
    option.provider_id.toLowerCase().includes(trimmedQuery)
    || option.label.toLowerCase().includes(trimmedQuery),
  );
}

export function normalizeApiProviderScope(value: unknown): ApiProviderScope {
  const normalized = String(value ?? "").trim().toLowerCase().replace(/-/g, "_");
  if (normalized === "llm" || normalized === "ai") return "llm";
  if (normalized === "non_llm" || normalized === "external" || normalized === "custom") return "non_llm";
  return "all";
}

export function filterApiProviderOptionsByScope(
  options: ApiProviderOption[],
  scope: ApiProviderScope,
): ApiProviderOption[] {
  if (scope === "all") return options;
  const expectedKind: ApiProviderKind = scope === "llm" ? "llm" : "custom";
  return options.filter((option) => option.kind === expectedKind);
}

export function filterRegisteredApiRowsByScope(
  rows: Array<Record<string, unknown>>,
  options: ApiProviderOption[],
  scope: ApiProviderScope,
): Array<Record<string, unknown>> {
  if (scope === "all") return rows;
  const expectedKind: ApiProviderKind = scope === "llm" ? "llm" : "custom";
  return rows.filter((row) => {
    const providerId = String(row.provider_id ?? "").trim();
    const option = options.find((candidate) => candidate.provider_id === providerId);
    return normalizeProviderKind(row.kind ?? option?.kind) === expectedKind;
  });
}

export function normalizeCustomProviderId(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, "_").replace(/^[-_.]+|[-_.]+$/g, "");
}

export function customProviderRegistrationPayload(option: {
  providerId: string;
  label?: string;
  kind?: ApiProviderKind;
}): { provider_id: string; label: string; kind: ApiProviderKind } | null {
  const providerId = normalizeCustomProviderId(option.providerId);
  if (!providerId) return null;
  return {
    provider_id: providerId,
    label: option.label?.trim() || providerId,
    kind: option.kind ?? "custom",
  };
}

export function parseAllowedModels(value: unknown): string[] {
  const items = Array.isArray(value) ? value : String(value ?? "").split(/\r?\n|,/);
  return Array.from(new Set(items.map((item) => String(item ?? "").trim()).filter(Boolean)));
}

export function buildApiKeySavePayload(draft: ApiKeySetupDraft, fallbackKind: ApiProviderKind = "llm"): ApiKeySavePayload | null {
  const providerId = draft.provider_id.trim();
  const name = draft.name.trim();
  const value = draft.value;
  const credentialMode = draft.credential_mode === "none" ? "none" : "api_key";
  if (!providerId || !name || (credentialMode === "api_key" && !value.trim()) || (credentialMode === "none" && !draft.base_url?.trim())) return null;
  const allowedModels = parseAllowedModels(draft.allowed_models);
  return {
    provider_id: providerId,
    value,
    options: {
      apiId: name,
      name,
      kind: draft.kind ?? fallbackKind,
      baseUrl: draft.base_url?.trim() || undefined,
      allowedModels: allowedModels.length ? allowedModels : undefined,
      defaultModel: draft.default_model?.trim() || undefined,
      quotaLabel: draft.quota_label?.trim() || undefined,
      notes: draft.notes?.trim() || undefined,
      credentialMode,
    },
  };
}

export function summarizeApiKeySetupForDiagnostics(
  draftOrPayload: ApiKeySetupDraft | ApiKeySavePayload,
  fallbackKind: ApiProviderKind = "llm",
): ApiKeySetupDiagnostic {
  const isPayload = "options" in draftOrPayload;
  const providerId = isPayload ? draftOrPayload.provider_id : draftOrPayload.provider_id;
  const secret = isPayload ? draftOrPayload.value : draftOrPayload.value;
  const saveOptions = isPayload ? draftOrPayload.options : buildApiKeySavePayload(draftOrPayload, fallbackKind)?.options;
  const allowedModels = isPayload ? draftOrPayload.options.allowedModels ?? [] : parseAllowedModels(draftOrPayload.allowed_models);
  return {
    provider_id: providerId.trim(),
    name: String(saveOptions?.name ?? (isPayload ? "" : draftOrPayload.name)).trim(),
    kind: saveOptions?.kind ?? fallbackKind,
    has_secret: secret.trim().length > 0,
    secret_length: secret.length,
    has_base_url: Boolean(saveOptions?.baseUrl),
    allowed_model_count: allowedModels.length,
    has_default_model: Boolean(saveOptions?.defaultModel),
    has_quota_label: Boolean(saveOptions?.quotaLabel),
    has_notes: Boolean(saveOptions?.notes),
  };
}
