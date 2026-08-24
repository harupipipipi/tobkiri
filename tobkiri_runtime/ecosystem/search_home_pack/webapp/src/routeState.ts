import {
  persistRouteSessionState,
  reviewRouteCandidate,
  ROUTE_SESSION_STORAGE_KEY,
  type RouteCandidate,
  type RouteDecision,
  type RouteSessionState,
} from "./routerTypes";

export const ROUTE_DECISION_STORAGE_KEY = "rumi-search-home-route-decision";

function isObjectLike(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function asBoundedString(value: unknown, maxLength: number): string {
  return typeof value === "string" ? value.slice(0, maxLength) : "";
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
  const candidate: RouteCandidate = {
    url: url || finalUrl,
    final_url: finalUrl || url,
    title: asBoundedString(value.title, 512),
    snippet: asBoundedString(value.snippet, 2048),
    domain: "",
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
  const review = reviewRouteCandidate(candidate);
  return {
    ...candidate,
    domain: review.ok ? review.host : "",
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
  if (!isFreshRouteSessionState(value)) {
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

export function isFreshRouteSessionState(
  value: unknown,
  now = Date.now(),
): value is RouteSessionState {
  if (!isObjectLike(value)) {
    return false;
  }
  const issuedAt = Date.parse(String(value.issued_at || ""));
  const expiresAt = Date.parse(String(value.expires_at || ""));
  return (
    /^[A-Za-z0-9_-]{16,128}$/.test(String(value.state_id || "")) &&
    Number.isFinite(issuedAt) &&
    Number.isFinite(expiresAt) &&
    issuedAt <= now + 30_000 &&
    expiresAt > now &&
    expiresAt - issuedAt <= 6 * 60 * 60 * 1000
  );
}

export function loadDecisionFromSessionStorage(storage: Storage | null): RouteDecision | null {
  if (!storage) {
    return null;
  }
  storage.removeItem(ROUTE_DECISION_STORAGE_KEY);
  try {
    const raw = storage.getItem(ROUTE_SESSION_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<RouteSessionState>;
    if (!isFreshRouteSessionState(parsed)) {
      storage.removeItem(ROUTE_SESSION_STORAGE_KEY);
      return null;
    }
    const decision = decisionFromSessionState(parsed);
    if (!decision) {
      storage.removeItem(ROUTE_SESSION_STORAGE_KEY);
    }
    return decision;
  } catch {
    storage.removeItem(ROUTE_SESSION_STORAGE_KEY);
    return null;
  }
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
  storage.removeItem(ROUTE_DECISION_STORAGE_KEY);
  return session;
}

export function clearDecisionSessionStorage(storage: Storage | null): void {
  if (!storage) {
    return;
  }
  storage.removeItem(ROUTE_DECISION_STORAGE_KEY);
  storage.removeItem(ROUTE_SESSION_STORAGE_KEY);
}
