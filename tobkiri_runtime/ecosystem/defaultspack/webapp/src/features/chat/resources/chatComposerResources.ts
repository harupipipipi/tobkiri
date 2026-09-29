import { api, type ModelSearchResponse } from "../../../lib/api";
import { createModelSearchResources } from "../../models";

const modelSearchResources = createModelSearchResources(api);

export const chatComposerResources = {
  searchModels(payload: { query: string; max_results: number }) {
    return modelSearchResources.searchModels(payload) as Promise<ModelSearchResponse>;
  },
};
