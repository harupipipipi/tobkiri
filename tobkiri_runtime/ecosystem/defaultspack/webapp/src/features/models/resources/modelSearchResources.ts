import type { ModelSearchResponse } from "../../../lib/api";

export type ModelSearchPayload = {
  query: string;
  max_results: number;
  provider_id?: string;
};

export type ModelSearchApiClient = {
  searchModels(filters: Record<string, unknown>): Promise<ModelSearchResponse>;
};

export function normalizeModelSearchPayload(payload: ModelSearchPayload): ModelSearchPayload {
  const maxResults = Number(payload.max_results);
  const providerId = String(payload.provider_id ?? "").trim();
  return {
    query: String(payload.query ?? "").trim(),
    max_results: Number.isFinite(maxResults) && maxResults > 0 ? Math.floor(maxResults) : 30,
    ...(providerId ? { provider_id: providerId } : {}),
  };
}

export function createModelSearchResources(apiClient: ModelSearchApiClient) {
  return {
    searchModels(payload: ModelSearchPayload) {
      return apiClient.searchModels(normalizeModelSearchPayload(payload));
    },
  };
}
