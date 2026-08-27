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

export type RouteDestinationBlockCode =
  | "empty"
  | "too_long"
  | "whitespace"
  | "control_characters"
  | "relative_url"
  | "malformed_url"
  | "unsupported_scheme"
  | "embedded_credentials"
  | "missing_host"
  | "private_network";

export type RouteDestinationReview =
  | {
      ok: true;
      input: string;
      url: string;
      host: string;
      hostname: string;
      protocol: "https:" | "http:";
      warnings: string[];
    }
  | {
      ok: false;
      input: string;
      code: RouteDestinationBlockCode;
      message: string;
    };

const MAX_ROUTE_URL_LENGTH = 4096;
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u001f\u007f]/;
const ENCODED_CONTROL_PATTERN = /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i;
const ABSOLUTE_SCHEME_PATTERN = /^[a-zA-Z][a-zA-Z\d+.-]*:/;

function parseIpv4(hostname: string): [number, number, number, number] | null {
  const parts = hostname.split(".");
  if (parts.length !== 4) {
    return null;
  }
  const values = parts.map((part) => {
    if (!/^\d{1,3}$/.test(part)) {
      return Number.NaN;
    }
    const value = Number(part);
    return value >= 0 && value <= 255 ? value : Number.NaN;
  });
  if (values.some((value) => Number.isNaN(value))) {
    return null;
  }
  return values as [number, number, number, number];
}

function isPrivateIpv4(hostname: string): boolean {
  const address = parseIpv4(hostname);
  if (!address) {
    return false;
  }
  const [a, b] = address;
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 0) ||
    (a === 192 && b === 168) ||
    (a === 198 && (b === 18 || b === 19)) ||
    a >= 224
  );
}

function isPrivateIpv6(hostname: string): boolean {
  const normalized = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!normalized.includes(":")) {
    return false;
  }
  if (normalized === "::" || normalized === "::1") {
    return true;
  }
  if (/^f[cd][0-9a-f]{2}:/.test(normalized) || /^fe[89ab][0-9a-f]:/.test(normalized)) {
    return true;
  }
  if (normalized.startsWith("::ffff:")) {
    return true;
  }
  return false;
}

function isPrivateHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized.endsWith(".local") ||
    normalized.endsWith(".lan") ||
    normalized.endsWith(".home") ||
    normalized.endsWith(".internal")
  ) {
    return true;
  }
  return isPrivateIpv4(normalized) || isPrivateIpv6(normalized);
}

function block(input: string, code: RouteDestinationBlockCode, message: string): RouteDestinationReview {
  return { ok: false, input, code, message };
}

export function reviewRouteDestination(input: string): RouteDestinationReview {
  if (!input) {
    return block(input, "empty", "移動先がありません。検索結果を更新してください。");
  }
  if (input.length > MAX_ROUTE_URL_LENGTH) {
    return block(input, "too_long", "移動先URLが長すぎるため開けません。");
  }
  if (input !== input.trim()) {
    return block(input, "whitespace", "移動先URLの前後に空白が含まれています。");
  }
  if (CONTROL_CHARACTER_PATTERN.test(input) || ENCODED_CONTROL_PATTERN.test(input)) {
    return block(input, "control_characters", "移動先URLに制御文字が含まれています。");
  }
  if (input.includes("\\")) {
    return block(input, "malformed_url", "曖昧な区切り文字を含む移動先URLは開けません。");
  }
  if (!ABSOLUTE_SCHEME_PATTERN.test(input)) {
    return block(input, "relative_url", "絶対URLではない移動先は開けません。");
  }

  let parsed: URL;
  try {
    parsed = new URL(input);
  } catch {
    return block(input, "malformed_url", "移動先URLを解析できません。");
  }

  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return block(input, "unsupported_scheme", `${parsed.protocol || "不明な"} URLは開けません。`);
  }
  if (parsed.username || parsed.password) {
    return block(input, "embedded_credentials", "認証情報を含むURLは開けません。");
  }
  if (!parsed.hostname) {
    return block(input, "missing_host", "移動先のホスト名がありません。");
  }
  if (isPrivateHostname(parsed.hostname)) {
    return block(input, "private_network", "ローカルまたはプライベートネットワークの移動先は開けません。");
  }

  const warnings: string[] = [];
  if (parsed.protocol === "http:") {
    warnings.push("暗号化されていないHTTP接続です");
  }
  if (parsed.hostname.toLowerCase().includes("xn--")) {
    warnings.push("国際化ドメイン（Punycode）を含みます");
  }
  if (parsed.port && !((parsed.protocol === "https:" && parsed.port === "443") || (parsed.protocol === "http:" && parsed.port === "80"))) {
    warnings.push(`標準外ポート ${parsed.port} を使用します`);
  }

  parsed.hash = "";
  return {
    ok: true,
    input,
    url: parsed.toString(),
    host: parsed.host,
    hostname: parsed.hostname,
    protocol: parsed.protocol,
    warnings,
  };
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
