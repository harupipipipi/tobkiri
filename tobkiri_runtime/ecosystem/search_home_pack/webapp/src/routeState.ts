import {
  persistRouteSessionState,
  ROUTE_SESSION_STORAGE_KEY,
  sanitizeRouteDecisionForStorage,
  type RouteCandidate,
  type RouteDecision,
  type RouteSessionState,
} from "./routerTypes";

export const ROUTE_DECISION_STORAGE_KEY = "rumi-search-home-route-decision";

const MAX_ROUTE_STATE_AGE_MS = 6 * 60 * 60 * 1000;
const MAX_CLOCK_SKEW_MS = 30_000;

function isObjectLike(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function asBoundedString(value: unknown, maxLength: number): string {
  return typeof value === "string" ? value.slice(0, maxLength) : "";
}

/** Return whether a restored route record is current and securely identified. */
export function isFreshRestoredRouteState(
  value: unknown,
  now = Date.now(),
): boolean {
  if (!isObjectLike(value)) return false;
  const stateId = String(value.state_id || "");
  const issuedAt = Date.parse(String(value.issued_at || ""));
  const expiresAt = Date.parse(String(value.expires_at || ""));
  return (
    /^[A-Za-z0-9_-]{16,128}$/.test(stateId) &&
    Number.isFinite(issuedAt) &&
    Number.isFinite(expiresAt) &&
    issuedAt <= now + MAX_CLOCK_SKEW_MS &&
    expiresAt > now &&
    expiresAt - issuedAt <= MAX_ROUTE_STATE_AGE_MS
  );
}

export function coerceCandidate(value: unknown): RouteCandidate | null {
  if (!isObjectLike(value)) {
    return null;
  }
  const url = asBoundedString(value.url, 4096);
  const finalUrl = asBoundedString(value.final_url, 4096) || url;
  if (!url && !finalUrl) {
    return null;
  }
  return {
    url: url || finalUrl,
    final_url: finalUrl || url,
    title: asBoundedString(value.title, 512),
    snippet: asBoundedString(value.snippet, 2048),
    domain: asBoundedString(value.domain, 512),
    source: asBoundedString(value.source, 128) || "session",
    status: typeof value.status === "number" ? value.status : null,
    canonical_url: asBoundedString(value.canonical_url, 4096),
    content_type: asBoundedString(value.content_type, 256),
    redirected: Boolean(value.redirected),
    looks_like_login: Boolean(value.looks_like_login),
    looks_like_paywall: Boolean(value.looks_like_paywall),
    looks_like_404: Boolean(value.looks_like_404),
    looks_like_ad_heavy: Boolean(value.looks_like_ad_heavy),
    is_search_results: Boolean(value.is_search_results),
    heuristic_score: typeof value.heuristic_score === "number" ? value.heuristic_score : null,
    screenshot_path: "",
  };
}

export function coerceRouteDecision(value: unknown): RouteDecision | null {
  if (!isObjectLike(value)) {
    return null;
  }
  const query = asBoundedString(value.query, 2048);
  const targetUrl = asBoundedString(value.target_url, 4096);
  const fallbackUrl = asBoundedString(value.fallback_url, 4096);
  const targetCandidates = Array.isArray(value.target_candidates)
    ? value.target_candidates
        .slice(0, 50)
        .map((candidate) => coerceCandidate(candidate))
        .filter((candidate): candidate is RouteCandidate => candidate !== null)
    : [];
  if (!query && !targetUrl && !fallbackUrl && targetCandidates.length === 0) {
    return null;
  }
  return {
    route_type: asBoundedString(value.route_type, 128) || "GOOGLE_REDIRECT",
    query,
    target_url: targetUrl || fallbackUrl,
    target_candidates: targetCandidates,
    selected_index: typeof value.selected_index === "number" ? value.selected_index : 0,
    fallback_url: fallbackUrl || targetUrl,
    resolution_reason: asBoundedString(value.resolution_reason, 512) || "restored_state",
    used_ai_judge: Boolean(value.used_ai_judge),
    used_visual_judge: Boolean(value.used_visual_judge),
    metadata: {},
  };
}

export function decisionFromSessionState(value: unknown): RouteDecision | null {
  if (!isObjectLike(value)) {
    return null;
  }
  const decision = coerceRouteDecision({ ...value, route_type: "GOOGLE_REDIRECT", metadata: {} });
  if (!decision) {
    return null;
  }
  return {
    ...decision,
    resolution_reason: "restored_session_state",
    used_ai_judge: false,
    used_visual_judge: false,
    metadata: {},
  };
}

export function loadDecisionFromSessionStorage(storage: Storage | null): RouteDecision | null {
  if (!storage) {
    return null;
  }
  for (const [key, parser] of [
    [ROUTE_DECISION_STORAGE_KEY, coerceRouteDecision],
    [ROUTE_SESSION_STORAGE_KEY, decisionFromSessionState],
  ] as const) {
    try {
      const raw = storage.getItem(key);
      if (!raw) {
        continue;
      }
      const parsed = JSON.parse(raw);
      if (key === ROUTE_SESSION_STORAGE_KEY && !isFreshRestoredRouteState(parsed)) {
        storage.removeItem(key);
        continue;
      }
      const decision = parser(parsed);
      if (decision) {
        return decision;
      }
      storage.removeItem(key);
    } catch {
      storage.removeItem(key);
    }
  }
  return null;
}

export function saveDecisionToSessionStorage(
  storage: Storage | null,
  decision: RouteDecision,
  selectedIndex: number,
): RouteSessionState | null {
  if (!storage) {
    return null;
  }
  const session = persistRouteSessionState(storage, decision, selectedIndex);
  storage.setItem(
    ROUTE_DECISION_STORAGE_KEY,
    JSON.stringify(sanitizeRouteDecisionForStorage(decision, selectedIndex)),
  );
  return session;
}

export function clearDecisionSessionStorage(storage: Storage | null): void {
  if (!storage) {
    return;
  }
  storage.removeItem(ROUTE_DECISION_STORAGE_KEY);
  storage.removeItem(ROUTE_SESSION_STORAGE_KEY);
}
