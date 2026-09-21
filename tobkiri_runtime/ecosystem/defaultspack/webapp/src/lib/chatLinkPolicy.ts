export type ChatLinkKind = "internal" | "web" | "download" | "local" | "unsupported" | "malformed";

export type ChatLinkDecision = {
  kind: ChatLinkKind;
  allowed: boolean;
  requiresStrongConfirmation: boolean;
  normalizedUrl?: string;
  host?: string;
  reason?: string;
  textMismatch: boolean;
};

const DOWNLOAD_EXTENSIONS = /\.(?:zip|dmg|pkg|exe|msi|apk|deb|rpm|tar|gz|7z|pdf)(?:$|[?#])/i;
const URLISH_TEXT = /^(?:https?:\/\/|www\.)[^\s]+$/i;

function isPrivateIpv4(host: string): boolean {
  const parts = host.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  return parts[0] === 10
    || parts[0] === 127
    || (parts[0] === 169 && parts[1] === 254)
    || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
    || (parts[0] === 192 && parts[1] === 168)
    || parts[0] === 0;
}

function isLocalHost(host: string): boolean {
  const lowered = host.toLowerCase().replace(/^\[|\]$/g, "");
  return lowered === "localhost"
    || lowered.endsWith(".localhost")
    || lowered === "::1"
    // IPv4-mapped IPv6 literals can resolve to a loopback/private IPv4
    // address after URL parsing canonicalizes their final 32 bits. Block the
    // entire representation instead of attempting a lossy reverse conversion.
    || lowered.startsWith("::ffff:")
    || lowered.startsWith("fe80:")
    || lowered.startsWith("fc")
    || lowered.startsWith("fd")
    || isPrivateIpv4(lowered);
}

function visibleTargetHost(text: string): string | undefined {
  const trimmed = text.trim().replace(/[),.;!?]+$/, "");
  if (!URLISH_TEXT.test(trimmed)) return undefined;
  try {
    return new URL(/^www\./i.test(trimmed) ? `https://${trimmed}` : trimmed).hostname.toLowerCase();
  } catch {
    return undefined;
  }
}

export function classifyChatLink(rawHref: string | undefined, visibleText = "", appOrigin?: string): ChatLinkDecision {
  if (!rawHref || rawHref.length > 8192 || /[\u0000-\u001f\u007f]/.test(rawHref)) {
    return { kind: "malformed", allowed: false, requiresStrongConfirmation: false, reason: "Malformed destination", textMismatch: false };
  }
  let parsed: URL;
  let origin: URL | undefined;
  try {
    origin = appOrigin ? new URL(appOrigin) : undefined;
    parsed = new URL(rawHref, origin);
  } catch {
    return { kind: "malformed", allowed: false, requiresStrongConfirmation: false, reason: "Malformed destination", textMismatch: false };
  }
  const protocol = parsed.protocol.toLowerCase();
  if (protocol !== "http:" && protocol !== "https:") {
    return { kind: "unsupported", allowed: false, requiresStrongConfirmation: false, normalizedUrl: parsed.href, reason: `Unsupported ${protocol || "custom"} scheme`, textMismatch: false };
  }
  if (parsed.username || parsed.password) {
    return { kind: "unsupported", allowed: false, requiresStrongConfirmation: true, normalizedUrl: parsed.href, host: parsed.hostname, reason: "Credential-bearing links are blocked", textMismatch: true };
  }
  const host = parsed.hostname.toLowerCase();
  const displayedHost = visibleTargetHost(visibleText);
  const textMismatch = Boolean(displayedHost && displayedHost !== host);
  if (origin && parsed.origin === origin.origin) {
    return { kind: "internal", allowed: true, requiresStrongConfirmation: false, normalizedUrl: `${parsed.pathname}${parsed.search}${parsed.hash}`, host, textMismatch };
  }
  if (isLocalHost(host)) {
    return { kind: "local", allowed: false, requiresStrongConfirmation: true, normalizedUrl: parsed.href, host, reason: "Local and private-network destinations are blocked", textMismatch };
  }
  const download = DOWNLOAD_EXTENSIONS.test(parsed.pathname);
  const suspiciousHost = host.includes("xn--") || /[^\x00-\x7f]/.test(host);
  return {
    kind: download ? "download" : "web",
    allowed: true,
    requiresStrongConfirmation: textMismatch || suspiciousHost || download,
    normalizedUrl: parsed.href,
    host,
    reason: textMismatch ? "The visible link text names a different destination" : suspiciousHost ? "Internationalized destination requires extra review" : download ? "This destination may download a file" : undefined,
    textMismatch,
  };
}

export function openChatLink(decision: ChatLinkDecision, opener: (url: string, target: string, features: string) => Window | null = window.open): boolean {
  if (!decision.allowed || !decision.normalizedUrl) return false;
  if (decision.kind === "internal") {
    window.history.pushState({}, "", decision.normalizedUrl);
    window.dispatchEvent(new PopStateEvent("popstate"));
    return true;
  }
  return opener(decision.normalizedUrl, "_blank", "noopener,noreferrer") !== null;
}
