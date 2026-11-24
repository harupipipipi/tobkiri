import { useVerifiedFrontendHost } from "../../host/VerifiedFrontendHostContext";
import { searchCaptureScope } from "./searchCaptureScope";

/** Derive the same captured scope for global search and its Settings reader. */
export function useSearchCaptureScope(scopeId?: string): string {
  const verifiedHost = useVerifiedFrontendHost();
  const fallbackScope = typeof window === "undefined"
    ? "fixture" : `${window.location.origin}${window.location.pathname}`;
  return searchCaptureScope(
    scopeId ?? verifiedHost?.catalog.profile_id ?? fallbackScope,
    verifiedHost?.catalog,
    verifiedHost?.activePlanHash,
  );
}
