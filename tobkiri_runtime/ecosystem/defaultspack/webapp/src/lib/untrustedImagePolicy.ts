import { defaultspackCanonicalRouteKey, type ChatContentBlock } from "./api";

export const MAX_UNTRUSTED_IMAGE_URL_LENGTH = 4096;

export type ImageUrlDisposition = "blocked" | "remote-consent" | "trusted-attachment";

export type ImageUrlPolicy = {
  disposition: ImageUrlDisposition;
  normalizedUrl: string;
  sourceLabel: string;
  reason?: string;
};

type ImagePolicyOptions = {
  appOrigin?: string;
  attachmentId?: string;
  trustedAttachment?: boolean;
};

const BLOCKED_HOSTNAMES = new Set([
  "localhost",
  "localhost.localdomain",
  "ip6-localhost",
  "ip6-loopback",
]);

function blockedIpv4(hostname: string): boolean {
  const parts = hostname.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) {
    return false;
  }
  const [a, b] = parts;
  return (
    a === 0
    || a === 10
    || a === 127
    || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 0)
    || (a === 192 && b === 168)
    || (a === 198 && (b === 18 || b === 19))
    || a >= 224
  );
}

function blockedHostname(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/\.$/, "");
  if (!host || BLOCKED_HOSTNAMES.has(host) || host.endsWith(".localhost") || host.endsWith(".local")) {
    return true;
  }
  if (blockedIpv4(host)) return true;
  if (!host.includes(":")) return false;
  const compact = host.replace(/^\[/, "").replace(/\]$/, "");
  return (
    compact === "::"
    || compact === "::1"
    || compact.startsWith("fc")
    || compact.startsWith("fd")
    || /^fe[89ab]/.test(compact)
    || compact.startsWith("ff")
    || compact.startsWith("::ffff:")
  );
}

function trustedAttachmentPath(url: URL, attachmentId: string): boolean {
  if (!/^[A-Za-z0-9_-]{8,200}$/.test(attachmentId)) return false;
  const encodedId = encodeURIComponent(attachmentId);
  return [
    `${defaultspackCanonicalRouteKey("api/attachments")}/${encodedId}`,
    `${defaultspackCanonicalRouteKey("api/media/attachments")}/${encodedId}`,
    `${defaultspackCanonicalRouteKey("api/blobs")}/${encodedId}`,
  ].some((prefix) => url.pathname === prefix || url.pathname.startsWith(`${prefix}/`));
}

/** Classify a chat image without fetching or resolving its hostname. */
export function classifyUntrustedImageUrl(
  raw: unknown,
  options: ImagePolicyOptions = {},
): ImageUrlPolicy {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (!value) return { disposition: "blocked", normalizedUrl: "", sourceLabel: "unknown", reason: "missing-url" };
  if (value.length > MAX_UNTRUSTED_IMAGE_URL_LENGTH || /[\u0000-\u001f\u007f]/.test(value)) {
    return { disposition: "blocked", normalizedUrl: "", sourceLabel: "invalid URL", reason: "malformed-or-too-long" };
  }

  let url: URL;
  try {
    url = new URL(value, options.appOrigin);
  } catch {
    return { disposition: "blocked", normalizedUrl: "", sourceLabel: "invalid URL", reason: "malformed-url" };
  }
  if (url.username || url.password) {
    return { disposition: "blocked", normalizedUrl: "", sourceLabel: url.hostname || "invalid URL", reason: "embedded-credentials" };
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return { disposition: "blocked", normalizedUrl: "", sourceLabel: url.protocol || "unsafe scheme", reason: "unsafe-scheme" };
  }
  const appOrigin = options.appOrigin ? new URL(options.appOrigin).origin : "";
  if (
    appOrigin
    && url.origin === appOrigin
    && options.trustedAttachment === true
    && options.attachmentId
    && trustedAttachmentPath(url, options.attachmentId)
  ) {
    return {
      disposition: "trusted-attachment",
      normalizedUrl: url.href,
      sourceLabel: "Rumi attachment",
    };
  }
  if (blockedHostname(url.hostname)) {
    return { disposition: "blocked", normalizedUrl: "", sourceLabel: url.hostname, reason: "local-or-private-network" };
  }
  return {
    disposition: "remote-consent",
    normalizedUrl: url.href,
    sourceLabel: url.host,
  };
}

export function extractImageBlockUrl(block: ChatContentBlock): string {
  const nested = block.image_url;
  const raw = block.url ?? (
    typeof nested === "object" && nested !== null && "url" in nested
      ? (nested as { url?: unknown }).url
      : ""
  );
  return typeof raw === "string" ? raw : "";
}

export function imageBlockAttachmentId(block: ChatContentBlock): string {
  for (const key of ["attachment_id", "attachmentId", "blob_id", "blobId", "asset_id", "assetId"]) {
    const value = block[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}
