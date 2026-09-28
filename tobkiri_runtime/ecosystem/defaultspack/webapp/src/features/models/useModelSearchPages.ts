import { useCallback, useEffect, useRef, useState } from "react";

import type { ModelSearchItem } from "../../lib/api";
import { settingsApiResources } from "../settings/resources/settingsApiResources";
import { modelProviderOptions, parseModelProviderQuery, type ModelSelectOption } from "./modelSelect";

const PAGE_SIZE = 50;

type PageState = {
  key: string;
  models: ModelSearchItem[];
  hasMore: boolean;
  loading: boolean;
  error: string;
};

const EMPTY: PageState = { key: "", models: [], hasMore: false, loading: false, error: "" };

/** Fetch bounded catalog pages, including the first page when the query is empty. */
export function useModelSearchPages(open: boolean, query: string, options: ModelSelectOption[], providerTrigger: string) {
  const [state, setState] = useState<PageState>(EMPTY);
  const requestSeq = useRef(0);
  const providerState = parseModelProviderQuery(query, modelProviderOptions(options), providerTrigger);
  const searchQuery = providerState.providerId ? providerState.modelQuery : query.trim();
  const providerId = providerState.providerId;
  const active = providerState.active;
  const key = JSON.stringify([searchQuery, providerId]);

  useEffect(() => {
    requestSeq.current += 1;
    const seq = requestSeq.current;
    if (!open || active) {
      setState(EMPTY);
      return;
    }
    setState({ key, models: [], hasMore: false, loading: true, error: "" });
    const timer = window.setTimeout(() => {
      settingsApiResources.searchModels({ query: searchQuery, provider_id: providerId, max_results: PAGE_SIZE, offset: 0 })
        .then((result) => {
          if (requestSeq.current !== seq) return;
          const models = result.models ?? [];
          setState({ key, models, hasMore: result.has_more ?? models.length === PAGE_SIZE, loading: false, error: "" });
        })
        .catch((error: unknown) => {
          if (requestSeq.current !== seq) return;
          setState({ key, models: [], hasMore: false, loading: false, error: error instanceof Error ? error.message : "モデル検索に失敗しました" });
        });
    }, searchQuery ? 160 : 0);
    return () => {
      window.clearTimeout(timer);
      requestSeq.current += 1;
    };
  }, [active, key, open, providerId, searchQuery]);

  const current = open && !active && state.key === key ? state : EMPTY;
  const loadMore = useCallback(async (): Promise<boolean> => {
    if (!open || active || current.loading || !current.hasMore) return false;
    const seq = ++requestSeq.current;
    const offset = current.models.length;
    setState((previous) => ({ ...previous, loading: true, error: "" }));
    try {
      const result = await settingsApiResources.searchModels({ query: searchQuery, provider_id: providerId, max_results: PAGE_SIZE, offset });
      if (requestSeq.current !== seq) return false;
      const page = result.models ?? [];
      setState((previous) => ({
        ...previous,
        models: [...previous.models, ...page],
        hasMore: page.length > 0 && (result.has_more ?? page.length === PAGE_SIZE),
        loading: false,
      }));
      return page.length > 0;
    } catch (error) {
      if (requestSeq.current !== seq) return false;
      setState((previous) => ({ ...previous, loading: false, error: error instanceof Error ? error.message : "モデル検索に失敗しました" }));
      return false;
    }
  }, [active, current, open, providerId, searchQuery]);

  return { ...current, loadMore };
}
