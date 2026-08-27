(function (root) {
  "use strict";

  function normalizeOrigin(value) {
    try {
      const url = new URL(String(value || "").trim());
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        return "";
      }
      if (url.username || url.password) {
        return "";
      }
      return url.origin;
    } catch (_error) {
      return "";
    }
  }

  function normalizeOrigins(values) {
    const source = Array.isArray(values)
      ? values
      : String(values || "").split(/[\s,]+/);
    return [...new Set(source.map(normalizeOrigin).filter(Boolean))].sort();
  }

  function permissionPattern(origin) {
    const normalized = normalizeOrigin(origin);
    return normalized ? `${normalized}/*` : "";
  }

  function permissionPatterns(origins) {
    return normalizeOrigins(origins).map(permissionPattern).filter(Boolean);
  }

  function canPoll(settings) {
    if (!settings || settings.enabled !== true) {
      return blocked("paused", "Browser control and polling are paused.");
    }
    if (settings.consentAcknowledged !== true) {
      return blocked(
        "consent_required",
        "Review and acknowledge browser access before enabling control."
      );
    }
    if (!String(settings.pairingToken || "").trim()) {
      return blocked(
        "unpaired",
        "Set a pairing token before enabling browser control."
      );
    }
    return { allowed: true, reason: "allowed", message: "Polling is allowed." };
  }

  function evaluateUrl(value, settings, context = {}) {
    let url;
    try {
      url = new URL(String(value || ""));
    } catch (_error) {
      return blocked("invalid_url", "The target URL is invalid.");
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return blocked(
        "unsupported_scheme",
        "Only HTTP and HTTPS pages can be controlled."
      );
    }
    if (context.incognito === true) {
      return blocked(
        "incognito_blocked",
        "Incognito pages are blocked by the Browser Companion policy."
      );
    }

    const origin = url.origin;
    const denied = new Set(normalizeOrigins(settings?.deniedOrigins));
    if (denied.has(origin)) {
      return blocked(
        "origin_denied",
        `${origin} is on the denied-sites list.`,
        origin
      );
    }
    const allowed = new Set(normalizeOrigins(settings?.allowedOrigins));
    if (!allowed.has(origin)) {
      return blocked(
        "origin_not_allowed",
        `${origin} is not on the allowed-sites list.`,
        origin
      );
    }
    if (context.hasHostPermission === false) {
      return blocked(
        "host_permission_denied",
        `Browser permission for ${origin} was not granted. Open Options to grant it.`,
        origin
      );
    }
    return {
      allowed: true,
      reason: "allowed",
      message: `${origin} is allowed.`,
      origin
    };
  }

  function blocked(reason, message, origin = "") {
    return { allowed: false, reason, message, origin };
  }

  root.TobkiriBrowserAccessPolicy = Object.freeze({
    canPoll,
    evaluateUrl,
    normalizeOrigin,
    normalizeOrigins,
    permissionPattern,
    permissionPatterns
  });
})(typeof globalThis !== "undefined" ? globalThis : self);
