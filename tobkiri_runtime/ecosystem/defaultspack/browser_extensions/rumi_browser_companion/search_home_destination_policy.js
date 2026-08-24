(function initSearchHomeDestinationPolicy(scope) {
  "use strict";

  const CONTROL_RE = /[\u0000-\u001f\u007f]/;
  const ENCODED_CONTROL_RE = /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i;
  const SECRET_QUERY_KEY_RE = /(?:^|[_-])(token|secret|password|passwd|key|signature|credential|auth|code)(?:$|[_-])/i;
  const MAX_DESTINATION_LENGTH = 4096;

  function result(verdict, reason, url = "", host = "") {
    return { verdict, reason, url, host };
  }

  function evaluate(value) {
    if (typeof value !== "string" || !value) return result("block", "missing_destination");
    if (value.length > MAX_DESTINATION_LENGTH) return result("block", "destination_too_long");
    if (value !== value.trim() || CONTROL_RE.test(value) || ENCODED_CONTROL_RE.test(value) || value.includes("\\")) {
      return result("block", "ambiguous_or_control_characters");
    }
    let parsed;
    try {
      parsed = new URL(value);
    } catch (_error) {
      return result("block", "malformed_url");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return result("block", "unsupported_scheme");
    if (parsed.username || parsed.password) return result("block", "embedded_credentials");
    if (isUnsafeLocalHost(parsed.hostname)) return result("block", "unsafe_local_target");
    const idn = parsed.hostname.split(".").some((label) => label.startsWith("xn--"));
    if (parsed.protocol === "http:" || idn) {
      return result("confirm", parsed.protocol === "http:" ? "unencrypted_http" : "idn_hostname", parsed.toString(), parsed.host);
    }
    return result("allow", "safe_https_destination", parsed.toString(), parsed.host);
  }

  function evaluateRedirect(initialValue, finalValue, redirected) {
    const initial = evaluate(initialValue);
    if (initial.verdict === "block") return initial;
    const final = evaluate(finalValue || initialValue);
    if (final.verdict === "block") return final;
    const changedOrigin =
      (redirected || initial.url !== final.url) && new URL(initial.url).origin !== new URL(final.url).origin;
    if (changedOrigin) return result("confirm", "cross_origin_redirect", final.url, final.host);
    return initial.verdict === "confirm" ? result("confirm", initial.reason, final.url, final.host) : final;
  }

  function isUnsafeLocalHost(hostname) {
    const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
    if (
      !host || host === "localhost" || host === "local" || host.endsWith(".localhost") || host.endsWith(".local") ||
      host.endsWith(".lan") || host.endsWith(".home") || host.endsWith(".internal") ||
      host === "home.arpa" || host.endsWith(".home.arpa")
    ) return true;
    const octets = host.split(".").map(Number);
    if (octets.length === 4 && octets.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
      const [a, b] = octets;
      return a === 0 || a === 10 || a === 127 || (a === 100 && b >= 64 && b <= 127) ||
        (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
        (a === 192 && (b === 0 || b === 168)) || (a === 198 && (b === 18 || b === 19 || b === 51)) ||
        (a === 203 && b === 0) || a >= 224;
    }
    if (!host.includes(":")) return false;
    return host === "::" || host === "::1" || host.startsWith("fc") || host.startsWith("fd") ||
      /^fe[89ab]/i.test(host) || /^fe[c-f]/i.test(host) || host.startsWith("ff") ||
      host.startsWith("::ffff:") || host.startsWith("2001:db8:");
  }

  function safeForPersistence(value) {
    const policy = evaluate(value);
    if (policy.verdict === "block") return "";
    const parsed = new URL(policy.url);
    if (parsed.hash) return "";
    for (const key of parsed.searchParams.keys()) {
      if (SECRET_QUERY_KEY_RE.test(key)) return "";
    }
    return policy.url;
  }

  function isTrustedSearchHomeOrigin(value, allowedOrigins = []) {
    let parsed;
    try {
      parsed = new URL(String(value || ""));
    } catch (_error) {
      return false;
    }
    if (parsed.username || parsed.password || parsed.origin !== String(value || "")) return false;
    return allowedOrigins.some((origin) => origin === parsed.origin);
  }

  scope.RumiSearchHomeDestinationPolicy = { evaluate, evaluateRedirect, safeForPersistence, isTrustedSearchHomeOrigin, isUnsafeLocalHost };
})(globalThis);
