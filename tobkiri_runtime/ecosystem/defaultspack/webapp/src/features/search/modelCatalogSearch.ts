import type { ModelSearchItem, ModelSearchResponse } from "../../lib/api";
import { createModelSearchResources } from "../models/resources/modelSearchResources";

export const MODEL_CATALOG_MAX_RESULTS = 100;
export const MODEL_CATALOG_DEBOUNCE_MS = 120;
export const MODEL_CATALOG_SEARCH_FAILED =
  "モデル一覧を取得できませんでした。再試行してください。";

export type ModelCatalogSearchFunction = (
  filters: Record<string, unknown>,
) => Promise<ModelSearchResponse>;

export type ModelCatalogQuery = {
  text: string;
  providerIds?: string[];
  connectionId?: string;
  /** Cache partition only. Captured Runtime Profile authority stays on the Host. */
  scopeId?: string;
};

export type ModelCatalogSearchState = {
  models: ModelSearchItem[];
  loading: boolean;
  error: string | null;
  /** All pages of this catalog query were returned; not proof of reachability. */
  complete: boolean;
};

const emptyState = (): ModelCatalogSearchState => ({
  models: [], loading: false, error: null, complete: false,
});

export function normalizeModelCatalogQuery(query: ModelCatalogQuery): ModelCatalogQuery {
  return {
    text: query.text.trim(),
    providerIds: [...new Set((query.providerIds ?? []).map((id) => id.trim()).filter(Boolean))].sort(),
    connectionId: query.connectionId?.trim() || undefined,
    scopeId: query.scopeId?.trim() || undefined,
  };
}

export function modelCatalogSearchKey(query: ModelCatalogQuery): string {
  const normalized = normalizeModelCatalogQuery(query);
  return JSON.stringify([
    normalized.scopeId ?? "", normalized.text, normalized.providerIds,
    normalized.connectionId ?? "",
  ]);
}

/** Stable identity preserves separate profiles and providers for the same model. */
export function modelCatalogItemIdentity(item: ModelSearchItem): string {
  return JSON.stringify([item.provider_id ?? "", item.model_id ?? "", item.profile_id]);
}

const stringFields = [
  "provider_id", "provider_display_name", "model_id", "qualified_model_id",
  "label", "speed_tier", "quality_tier", "cost_tier", "default_thinking_level",
] as const;
const booleanFields = [
  "requires_api_key", "api_key_required", "api_key_configured", "configured", "route_configured", "local",
  "supports_vision", "supports_image_input", "supports_audio", "supports_audio_input",
  "supports_tool_calling", "supports_thinking", "supports_fast",
] as const;

/** Copy only picker data; arbitrary metadata, credentials and errors never enter cache. */
export function sanitizeModelCatalogItem(value: unknown): ModelSearchItem | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  const stringValue = (key: string) => typeof source[key] === "string" ? source[key].trim() : "";
  const providerId = stringValue("provider_id");
  const modelId = stringValue("model_id");
  const connectionId = stringValue("connection_id");
  const boundPublicCatalog = Boolean(connectionId)
    && source.provenance === "provider_public_catalog" && source.reachability === "unverified";
  const reachability = boundPublicCatalog ? "unverified" : "unknown";
  const profileId = stringValue("profile_id") || stringValue("qualified_model_id")
    || (providerId && modelId ? `${providerId}/${modelId}` : "");
  if (!profileId) return null;
  const item: ModelSearchItem = {
    profile_id: profileId,
    display_name: stringValue("display_name") || stringValue("label") || profileId,
    // Catalog membership and credentials do not establish connection health.
    health_status: "unverified",
    reachability,
    ...(connectionId ? { connection_id: connectionId } : {}),
    ...(source.provenance === "provider_public_catalog" ? { provenance: "provider_public_catalog" as const } : {}),
  };
  for (const key of stringFields) {
    if (typeof source[key] === "string") item[key] = source[key];
  }
  if (source.default_thinking_level === null) item.default_thinking_level = null;
  for (const key of booleanFields) {
    if (typeof source[key] === "boolean") item[key] = source[key];
  }
  for (const key of ["knowledge_level", "score"] as const) {
    if (typeof source[key] === "number" && Number.isFinite(source[key])) item[key] = source[key];
  }
  for (const key of ["thinking_levels", "capability_tags", "recommended_roles"] as const) {
    if (Array.isArray(source[key])) {
      item[key] = source[key].filter((entry): entry is string => typeof entry === "string");
    }
  }
  const availability: Record<string, unknown> = {
    health_status: "unverified", reachability,
  };
  if (source.availability && typeof source.availability === "object") {
    const supplied = source.availability as Record<string, unknown>;
    for (const key of ["configured", "active", "local", "offline"]) {
      if (typeof supplied[key] === "boolean") availability[key] = supplied[key];
    }
  }
  item.availability = availability;
  return item;
}

function responseComplete(response: ModelSearchResponse): boolean {
  if (typeof response.has_more === "boolean") return !response.has_more;
  return typeof response.total === "number" && Number.isFinite(response.total)
    && response.total >= 0 && response.total <= response.models.length;
}

/** Bounded, in-memory shared cache; no credential/configuration snapshots are stored. */
export function createModelCatalogSearchStore(
  searchModels: ModelCatalogSearchFunction,
  { now = Date.now, cacheTtlMs = 30_000, maxCacheEntries = 64 } = {},
) {
  const resources = createModelSearchResources({ searchModels });
  const cache = new Map<string, { expiresAt: number; state: ModelCatalogSearchState }>();
  const inflight = new Map<string, Promise<ModelCatalogSearchState>>();

  const peek = (query: ModelCatalogQuery): ModelCatalogSearchState | undefined => {
    const key = modelCatalogSearchKey(query);
    const entry = cache.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt <= now()) { cache.delete(key); return undefined; }
    return entry.state;
  };

  const search = (query: ModelCatalogQuery, force = false): Promise<ModelCatalogSearchState> => {
    const normalized = normalizeModelCatalogQuery(query);
    const key = modelCatalogSearchKey(normalized);
    if (!force) {
      const cached = peek(normalized);
      if (cached) return Promise.resolve(cached);
    } else cache.delete(key);
    const pending = inflight.get(key);
    if (pending) return pending;
    const providers = normalized.providerIds?.length ? normalized.providerIds : [undefined];
    const request = Promise.allSettled(providers.map((providerId) => Promise.resolve().then(() =>
      resources.searchModels({
        query: normalized.text, max_results: MODEL_CATALOG_MAX_RESULTS,
        ...(providerId ? { provider_id: providerId } : {}),
        ...(normalized.connectionId ? { connection_id: normalized.connectionId } : {}),
      }),
    ))).then((results) => {
      const models = new Map<string, ModelSearchItem>();
      let failed = false;
      let complete = true;
      for (const [index, result] of results.entries()) {
        if (result.status === "rejected" || !Array.isArray(result.value?.models)) {
          failed = true;
          complete = false;
          continue;
        }
        if (normalized.connectionId && result.value.filters_applied?.connection_id !== normalized.connectionId) {
          failed = true;
          complete = false;
          continue;
        }
        complete = complete && responseComplete(result.value);
        for (const value of result.value.models) {
          const item = sanitizeModelCatalogItem(value);
          const requestedProviderId = providers[index];
          if (item && normalized.connectionId && (
            item.connection_id !== normalized.connectionId
            || item.provenance !== "provider_public_catalog" || item.reachability !== "unverified"
          )) {
            failed = true;
            complete = false;
            continue;
          }
          if (item && requestedProviderId && item.provider_id !== requestedProviderId) {
            // The response must carry its own matching identity; never rebind an
            // unrestricted catalog response to the requested provider.
            failed = true;
            complete = false;
            continue;
          }
          if (item) models.set(modelCatalogItemIdentity(item), item);
          else complete = false;
        }
      }
      const ordered = [...models.values()].sort((left, right) => (
        (right.score ?? 0) - (left.score ?? 0)
        || left.display_name.localeCompare(right.display_name)
        || modelCatalogItemIdentity(left).localeCompare(modelCatalogItemIdentity(right))
      ));
      const state: ModelCatalogSearchState = {
        models: ordered.slice(0, MODEL_CATALOG_MAX_RESULTS), loading: false,
        error: failed ? MODEL_CATALOG_SEARCH_FAILED : null,
        complete: complete && ordered.length <= MODEL_CATALOG_MAX_RESULTS,
      };
      if (!failed) {
        cache.set(key, { expiresAt: now() + cacheTtlMs, state });
        while (cache.size > maxCacheEntries) {
          cache.delete(cache.keys().next().value!);
        }
      }
      return state;
    }).finally(() => { inflight.delete(key); });
    inflight.set(key, request);
    return request;
  };

  return { peek, search, clear: () => cache.clear() };
}

export type ModelCatalogSearchStore = ReturnType<typeof createModelCatalogSearchStore>;

/** Debounce and generation guards are independent of React for deterministic tests. */
export function createModelCatalogSearchController(
  store: ModelCatalogSearchStore,
  {
    debounceMs = MODEL_CATALOG_DEBOUNCE_MS,
    schedule = (callback: () => void, delay: number) => setTimeout(callback, delay),
    cancel = (timer: ReturnType<typeof setTimeout>) => clearTimeout(timer),
  } = {},
) {
  let state = emptyState();
  let key = "";
  let query: ModelCatalogQuery = { text: "" };
  let enabled = false;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const listeners = new Set<() => void>();
  const publish = (next: ModelCatalogSearchState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };
  const stop = () => {
    generation += 1;
    if (timer !== undefined) cancel(timer);
    timer = undefined;
  };
  const run = (force = false) => {
    stop();
    if (!enabled) { publish(emptyState()); return; }
    const cached = force ? undefined : store.peek(query);
    if (cached) { publish(cached); return; }
    publish({ ...emptyState(), loading: true });
    const currentGeneration = generation;
    const currentQuery = query;
    timer = schedule(() => {
      timer = undefined;
      void store.search(currentQuery, force).then((result) => {
        if (generation === currentGeneration) publish(result);
      });
    }, force ? 0 : debounceMs);
  };
  return {
    getSnapshot: () => state,
    getKey: () => key,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    setQuery: (next: ModelCatalogQuery, nextEnabled: boolean) => {
      query = normalizeModelCatalogQuery(next);
      key = modelCatalogSearchKey(query);
      enabled = nextEnabled;
      run();
    },
    retry: () => run(true),
    cancel: stop,
  };
}
