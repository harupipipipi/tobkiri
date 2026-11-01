import { useEffect, useState } from "react";
import { createSearchQueryState, updateSearchQueryState, type SearchQueryState } from "./searchQueryState";

/** Keep explicit confirmations while a caller controls the visible query string. */
export function useSearchQueryState(query: string, onQueryChange: (value: string) => void) {
  const [stored, setStored] = useState(() => createSearchQueryState(query));
  const state = stored.value === query ? stored : updateSearchQueryState(stored, query);
  useEffect(() => {
    setStored((current) => current.value === query ? current : updateSearchQueryState(current, query));
  }, [query]);
  const change = (next: SearchQueryState) => {
    setStored(next);
    onQueryChange(next.value);
  };
  return [state, change] as const;
}
