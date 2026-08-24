export type DestinationVerdict = "allow" | "confirm" | "block";

export type DestinationPolicyResult = {
  verdict: DestinationVerdict;
  normalized_url: string;
  display_host: string;
  reason: string;
  details: string;
};

const CONTROL_CHARACTER_RE = /[\u0000-\u001f\u007f]/;
const ENCODED_CONTROL_RE = /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i;
const MAX_DESTINATION_LENGTH = 4096;
const WEB_PROTOCOLS = new Set(["http:", "https:"]);
const SECRET_QUERY_KEY_RE = /(?:^|[_-])(token|secret|password|passwd|key|signature|credential|auth|code)(?:$|[_-])/i;
const EXPLICIT_URL_INPUT_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/|\\\\)/i;

function blocked(reason: string, details: string): DestinationPolicyResult {
  return { verdict: "block", normalized_url: "", display_host: "", reason, details };
}

function hostIsUnsafeLocalTarget(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (
    !host ||
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local") ||
    host.endsWith(".lan") ||
    host.endsWith(".home") ||
    host.endsWith(".internal") ||
    host === "local" ||
    host === "home.arpa" ||
    host.endsWith(".home.arpa")
  ) {
    return true;
  }

  const ipv4 = host.split(".").map((part) => Number(part));
  if (ipv4.length === 4 && ipv4.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
    const [a, b] = ipv4;
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
      (a === 198 && b === 51) ||
      (a === 203 && b === 0) ||
      a >= 224
    );
  }

  if (!host.includes(":")) {
    return false;
  }
  const compact = host.replace(/^0+(?=[0-9a-f])/i, "");
  return (
    compact === "::" ||
    compact === "::1" ||
    compact.startsWith("fc") ||
    compact.startsWith("fd") ||
    /^fe[89ab]/i.test(compact) ||
    /^fe[c-f]/i.test(compact) ||
    compact.toLowerCase().startsWith("ff") ||
    compact.toLowerCase().startsWith("::ffff:") ||
    compact.toLowerCase().startsWith("2001:db8:")
  );
}

function rawAuthorityHasIdn(raw: string): boolean {
  const authority = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i.exec(raw)?.[1] ?? "";
  return /[^\u0000-\u007f]/.test(authority);
}

export function evaluateDestination(value: unknown): DestinationPolicyResult {
  if (typeof value !== "string" || !value) {
    return blocked("missing_destination", "No destination URL was provided.");
  }
  if (value.length > MAX_DESTINATION_LENGTH) {
    return blocked("destination_too_long", "The destination URL is too long to review safely.");
  }
  if (value !== value.trim() || CONTROL_CHARACTER_RE.test(value) || ENCODED_CONTROL_RE.test(value)) {
    return blocked("control_characters", "The destination contains hidden or encoded control characters.");
  }
  if (value.includes("\\")) {
    return blocked("ambiguous_syntax", "The destination contains ambiguous URL separators.");
  }

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return blocked("malformed_url", "The destination is not a valid absolute URL.");
  }
  if (!WEB_PROTOCOLS.has(parsed.protocol)) {
    return blocked("unsupported_scheme", `The ${parsed.protocol || "unknown"} scheme is not allowed.`);
  }
  if (parsed.username || parsed.password) {
    return blocked("embedded_credentials", "Destinations containing embedded credentials are blocked.");
  }
  if (!parsed.hostname) {
    return blocked("missing_hostname", "The destination does not contain a valid host.");
  }
  if (hostIsUnsafeLocalTarget(parsed.hostname)) {
    return blocked("unsafe_local_target", "Local, private, reserved, and link-local destinations are blocked.");
  }

  const idn = parsed.hostname.split(".").some((label) => label.startsWith("xn--")) || rawAuthorityHasIdn(value);
  const verdict: DestinationVerdict = parsed.protocol === "http:" || idn ? "confirm" : "allow";
  const reasons = [parsed.protocol === "http:" ? "unencrypted_http" : "", idn ? "idn_hostname" : ""].filter(Boolean);
  const explanation = [
    parsed.protocol === "http:" ? "This destination uses unencrypted HTTP." : "",
    idn ? "The hostname contains an internationalized domain and is shown in normalized form." : "",
  ]
    .filter(Boolean)
    .join(" ");
  return {
    verdict,
    normalized_url: parsed.toString(),
    display_host: parsed.host,
    reason: reasons.join("+") || "safe_https_destination",
    details: explanation || `Destination host: ${parsed.host}`,
  };
}

/**
 * Classify input that explicitly looks like a URL before it is sent to a
 * resolver, search engine, or answer backend. Plain search text returns null.
 */
export function evaluateExplicitDestinationInput(value: unknown): DestinationPolicyResult | null {
  if (typeof value !== "string" || !EXPLICIT_URL_INPUT_RE.test(value)) {
    return null;
  }
  return evaluateDestination(value);
}

export function evaluateRedirectDestination(
  initialValue: unknown,
  finalValue: unknown,
  redirected = false,
): DestinationPolicyResult {
  const initial = evaluateDestination(initialValue);
  if (initial.verdict === "block") {
    return { ...initial, details: `The redirect chain starts with an unsafe destination. ${initial.details}` };
  }
  const final = evaluateDestination(finalValue || initialValue);
  if (final.verdict === "block") {
    return { ...final, details: `The redirect chain ends at an unsafe destination. ${final.details}` };
  }

  const initialOrigin = new URL(initial.normalized_url).origin;
  const finalOrigin = new URL(final.normalized_url).origin;
  const crossOriginRedirect = (redirected || initial.normalized_url !== final.normalized_url) && initialOrigin !== finalOrigin;
  if (crossOriginRedirect) {
    return {
      ...final,
      verdict: "confirm",
      reason: [final.reason === "safe_https_destination" ? "" : final.reason, "cross_origin_redirect"].filter(Boolean).join("+") || "cross_origin_redirect",
      details: `The destination redirects from ${new URL(initial.normalized_url).host} to ${final.display_host}. Confirm the normalized host before continuing.`,
    };
  }
  return initial.verdict === "confirm" && final.verdict === "allow" ? { ...final, verdict: "confirm", reason: initial.reason, details: initial.details } : final;
}

export function safePolicyDetails(result: DestinationPolicyResult): string {
  const host = result.display_host ? ` Host: ${result.display_host}.` : "";
  return `Search Home destination ${result.verdict}: ${result.reason}.${host} ${result.details}`.slice(0, 600);
}

export function urlSafeForPersistence(value: string): string {
  const policy = evaluateDestination(value);
  if (policy.verdict === "block") return "";
  const parsed = new URL(policy.normalized_url);
  if (parsed.username || parsed.password || parsed.hash) return "";
  for (const key of parsed.searchParams.keys()) {
    if (SECRET_QUERY_KEY_RE.test(key)) return "";
  }
  return policy.normalized_url;
}
