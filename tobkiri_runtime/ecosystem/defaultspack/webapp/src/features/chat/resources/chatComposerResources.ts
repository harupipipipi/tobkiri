import { api, type ModelSearchResponse } from "../../../lib/api";
import { createModelSearchResources } from "../../models";

const modelSearchResources = createModelSearchResources(api);

export const chatComposerResources = {
  searchModels(payload: { query: string; provider_id?: string; max_results: number; offset?: number }) {
    return modelSearchResources.searchModels(payload) as Promise<ModelSearchResponse>;
  },
};
