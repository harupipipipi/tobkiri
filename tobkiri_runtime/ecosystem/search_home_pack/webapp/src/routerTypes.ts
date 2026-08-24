import {
  evaluateDestination,
  evaluateRedirectDestination,
  urlSafeForPersistence,
  type DestinationPolicyResult,
  type DestinationVerdict,
} from "./destinationPolicy";

export type RouteCandidate = {
  url: string;
  final_url?: string;
  title?: string;
  snippet?: string;
  domain?: string;
  source?: string;
  status?: number | null;
  canonical_url?: string;
  content_type?: string;
  redirected?: boolean;
  looks_like_login?: boolean;
  looks_like_paywall?: boolean;
  looks_like_404?: boolean;
  looks_like_ad_heavy?: boolean;
  is_search_results?: boolean;
  heuristic_score?: number | null;
  screenshot_path?: string;
};

export type RouteDecision = {
  route_type?: string;
  query: string;
  target_url: string;
  target_candidates: RouteCandidate[];
  selected_index: number;
  fallback_url: string;
  resolution_reason?: string;
  used_ai_judge?: boolean;
  used_visual_judge?: boolean;
  metadata?: Record<string, unknown>;
};

export type RouteSessionCandidate = {
  url: string;
  final_url: string;
  title: string;
  domain: string;
};

export type RouteSessionState = {
  query: string;
  target_url: string;
  fallback_url: string;
  selected_index: number;
  target_candidates: RouteSessionCandidate[];
  updated_at: string;
  state_id: string;
  issued_at: string;
  expires_at: string;
};

export type RouteDestinationBlockCode = string;

export type RouteDestinationReview =
  | {
      ok: true;
      input: string;
      url: string;
      host: string;
      hostname: string;
      protocol: "https:" | "http:";
      verdict: Exclude<DestinationVerdict, "block">;
      confirmationRequired: boolean;
      reason: string;
      details: string;
      warnings: string[];
    }
  | {
      ok: false;
      input: string;
      code: RouteDestinationBlockCode;
      message: string;
    };

export type BrowserCompanionRouteMessage = {
  type: typeof ROUTE_BROWSER_MESSAGE_TYPE;
  source: typeof ROUTE_BROWSER_MESSAGE_SOURCE;
  payload: RouteSessionState;
};

export const ROUTE_SESSION_STORAGE_KEY = "rumi-search-home-route-state";
export const ROUTE_BROWSER_MESSAGE_TYPE = "rumi:search-home:set-route-state";
export const ROUTE_BROWSER_MESSAGE_SOURCE = "rumi-search-home";

const MAX_ROUTE_QUERY_LENGTH = 2048;
function reviewPolicyResult(input: string, policy: DestinationPolicyResult): RouteDestinationReview {
  if (policy.verdict === "block") {
    return { ok: false, input, code: policy.reason, message: policy.details };
  }
  const parsed = new URL(policy.normalized_url);
  parsed.hash = "";
  const warnings: string[] = [];
  if (policy.reason.includes("unencrypted_http")) {
    warnings.push("暗号化されていないHTTP接続です。確認してから開いてください");
  }
  if (policy.reason.includes("idn_hostname")) {
    warnings.push("国際化ドメイン（Punycode）です。正規化されたホストを確認してください");
  }
  if (policy.reason.includes("cross_origin_redirect")) {
    warnings.push("別のホストへリダイレクトされます。移動先ホストを確認してください");
  }
  if (parsed.port && !((parsed.protocol === "https:" && parsed.port === "443") || (parsed.protocol === "http:" && parsed.port === "80"))) {
    warnings.push(`標準外ポート ${parsed.port} を使用します`);
  }
  return {
    ok: true,
    input,
    url: parsed.toString(),
    host: parsed.host,
    hostname: parsed.hostname,
    protocol: parsed.protocol as "https:" | "http:",
    verdict: policy.verdict,
    confirmationRequired: policy.verdict === "confirm",
    reason: policy.reason,
    details: policy.details,
    warnings,
  };
}

export function reviewRouteDestination(input: string): RouteDestinationReview {
  return reviewPolicyResult(input, evaluateDestination(input));
}

export function reviewRouteCandidate(candidate: RouteCandidate): RouteDestinationReview {
  const initial = candidate.url || candidate.final_url || "";
  const final = candidate.final_url || candidate.url || "";
  return reviewPolicyResult(
    final,
    evaluateRedirectDestination(initial, final, Boolean(candidate.redirected)),
  );
}

export function selectedCandidate(decision: RouteDecision, selectedIndex = decision.selected_index): RouteCandidate | null {
  if (!decision.target_candidates.length) {
    return null;
  }
  const normalizedIndex = normalizeSelectedIndex(decision, selectedIndex);
  return decision.target_candidates[normalizedIndex] ?? null;
}

export function selectedCandidateUrl(decision: RouteDecision, selectedIndex = decision.selected_index): string {
  const candidate = selectedCandidate(decision, selectedIndex);
  if (!candidate) {
    return decision.target_url || decision.fallback_url;
  }
  return candidate.final_url || candidate.url || decision.target_url || decision.fallback_url;
}

export function selectedDestinationReview(
  decision: RouteDecision,
  selectedIndex = decision.selected_index,
): RouteDestinationReview {
  const candidate = selectedCandidate(decision, selectedIndex);
  return candidate
    ? reviewRouteCandidate(candidate)
    : reviewRouteDestination(decision.target_url || decision.fallback_url);
}

export function normalizeSelectedIndex(decision: RouteDecision, selectedIndex = decision.selected_index): number {
  const total = decision.target_candidates.length;
  if (total <= 0) {
    return -1;
  }
  if (!Number.isInteger(selectedIndex) || selectedIndex < 0 || selectedIndex >= total) {
    return 0;
  }
  return selectedIndex;
}

export function cycleCandidateIndex(decision: RouteDecision, currentIndex: number, delta: number): number {
  const total = decision.target_candidates.length;
  if (total <= 0) {
    return -1;
  }
  const start = normalizeSelectedIndex(decision, currentIndex);
  return (start + delta + total) % total;
}

function safeSessionCandidate(candidate: RouteCandidate): RouteSessionCandidate | null {
  const url = safeUrlForStorage(candidate.url || candidate.final_url || "");
  const finalUrl = safeUrlForStorage(candidate.final_url || candidate.url || "");
  const review = reviewRouteCandidate({ ...candidate, url, final_url: finalUrl });
  if (!url || !finalUrl || !review.ok) {
    return null;
  }
  return {
    url,
    final_url: finalUrl,
    title: candidate.title || "",
    domain: review.host,
  };
}

function safeUrlForStorage(input: string): string {
  return urlSafeForPersistence(input);
}

function safeQueryForStorage(query: string): string {
  const bounded = query.slice(0, MAX_ROUTE_QUERY_LENGTH);
  return bounded.includes("://") && !safeUrlForStorage(bounded) ? "" : bounded;
}

function createRouteStateId(): string {
  const random = globalThis.crypto?.getRandomValues?.(new Uint8Array(16));
  if (!random) throw new Error("Secure randomness is required for route state.");
  return Array.from(random, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function buildRouteSessionState(decision: RouteDecision, selectedIndex = decision.selected_index): RouteSessionState {
  const normalizedOriginalIndex = normalizeSelectedIndex(decision, selectedIndex);
  const selectedRawUrl = selectedCandidateUrl(decision, normalizedOriginalIndex);
  const selectedUrl = safeUrlForStorage(selectedRawUrl);
  const fallbackUrl = safeUrlForStorage(decision.fallback_url);
  const candidates = decision.target_candidates
    .map((candidate) => safeSessionCandidate(candidate))
    .filter((candidate): candidate is RouteSessionCandidate => candidate !== null);
  const safeSelectedIndex = selectedUrl
    ? candidates.findIndex((candidate) => candidate.final_url === selectedUrl)
    : -1;
  const issuedAt = new Date();

  return {
    query: safeQueryForStorage(decision.query),
    target_url: selectedUrl || fallbackUrl,
    fallback_url: fallbackUrl,
    selected_index: safeSelectedIndex,
    target_candidates: candidates,
    updated_at: issuedAt.toISOString(),
    state_id: createRouteStateId(),
    issued_at: issuedAt.toISOString(),
    expires_at: new Date(issuedAt.getTime() + 5 * 60 * 1000).toISOString(),
  };
}

export function sanitizeRouteDecisionForStorage(
  decision: RouteDecision,
  selectedIndex = decision.selected_index,
): RouteDecision {
  const session = buildRouteSessionState(decision, selectedIndex);
  return {
    route_type: decision.route_type,
    query: session.query,
    target_url: session.target_url,
    target_candidates: session.target_candidates.map((candidate) => ({ ...candidate })),
    selected_index: session.selected_index,
    fallback_url: session.fallback_url,
    resolution_reason: decision.resolution_reason,
    used_ai_judge: Boolean(decision.used_ai_judge),
    used_visual_judge: Boolean(decision.used_visual_judge),
    metadata: {},
  };
}

export function persistRouteSessionState(
  storage: Pick<Storage, "setItem"> | null | undefined,
  decision: RouteDecision,
  selectedIndex = decision.selected_index,
): RouteSessionState | null {
  if (!storage) {
    return null;
  }
  const state = buildRouteSessionState(decision, selectedIndex);
  storage.setItem(ROUTE_SESSION_STORAGE_KEY, JSON.stringify(state));
  return state;
}

export function buildBrowserCompanionRouteMessage(
  decision: RouteDecision,
  selectedIndex = decision.selected_index,
): BrowserCompanionRouteMessage {
  return {
    type: ROUTE_BROWSER_MESSAGE_TYPE,
    source: ROUTE_BROWSER_MESSAGE_SOURCE,
    payload: buildRouteSessionState(decision, selectedIndex),
  };
}
