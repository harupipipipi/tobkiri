import { useEffect, useMemo, useSyncExternalStore } from "react";
import { api } from "../../lib/api";
import { useSearchCaptureScope } from "./useSearchCaptureScope";
import {
  createModelCatalogSearchController,
  createModelCatalogSearchStore,
  modelCatalogSearchKey,
  type ModelCatalogQuery,
  type ModelCatalogSearchFunction,
  type ModelCatalogSearchStore,
} from "./modelCatalogSearch";

const stores = new WeakMap<ModelCatalogSearchFunction, ModelCatalogSearchStore>();
function sharedStore(searchModels: ModelCatalogSearchFunction): ModelCatalogSearchStore {
  let store = stores.get(searchModels);
  if (!store) {
    store = createModelCatalogSearchStore(searchModels);
    stores.set(searchModels, store);
  }
  return store;
}

/** One catalog source for Settings, composer and global search consumers. */
export function useModelCatalogSearch({
  text,
  providerIds,
  connectionId,
  scopeId,
  enabled,
  searchModels = api.searchModels,
}: ModelCatalogQuery & { enabled: boolean; searchModels?: ModelCatalogSearchFunction }) {
  // A caller Profile scope never overrides the Host's activation/plan fence.
  const resolvedScope = useSearchCaptureScope(scopeId);
  const key = modelCatalogSearchKey({ text, providerIds, connectionId, scopeId: resolvedScope });
  const controller = useMemo(
    () => createModelCatalogSearchController(sharedStore(searchModels)), [searchModels],
  );
  const state = useSyncExternalStore(
    controller.subscribe, controller.getSnapshot, controller.getSnapshot,
  );
  useEffect(() => {
    const [scope, query, providers, connection] = JSON.parse(key) as [string, string, string[], string];
    controller.setQuery({ text: query, providerIds: providers, connectionId: connection, scopeId: scope }, enabled);
    return controller.cancel;
  }, [controller, key, enabled]);
  // A render with new filters must not show a previous connection/query snapshot.
  const current = controller.getKey() === key && enabled;
  return {
    models: current ? state.models : [],
    loading: enabled && (!current || state.loading),
    error: current ? state.error : null,
    complete: current && state.complete,
    retry: controller.retry,
  };
}
