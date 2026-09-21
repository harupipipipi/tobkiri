export type OAuthDestinationReview = {
  authorizeUrl: string;
  providerId: string;
  host: string;
  path: string;
};

export type CredentialImportReview = {
  kind: "connection_import" | "oauth_client";
  fields: string[];
  scopes: string[];
  endpoints: string[];
  secretFieldCount: number;
};

const destinationPolicies: Record<string, { hosts: string[]; path: (pathname: string) => boolean }> = {
  google: {
    hosts: ["accounts.google.com"],
    path: (pathname) => pathname.startsWith("/o/oauth2/"),
  },
  github: {
    hosts: ["github.com"],
    path: (pathname) => pathname === "/login/oauth/authorize",
  },
  cloudflare: {
    hosts: ["dash.cloudflare.com"],
    path: (pathname) => pathname.startsWith("/oauth2/"),
  },
  codex: {
    hosts: ["auth.openai.com"],
    path: (pathname) => pathname.startsWith("/oauth/authorize"),
  },
};

const secretFieldPattern = /^(access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|private[_-]?key|password|credential|secret|token)$/i;
const endpointFieldPattern = /(?:^|_)(?:auth|token|issuer|redirect|endpoint|url|uri)(?:_|$)/i;
const scopeFieldPattern = /^(?:scope|scopes|permissions|capabilities)$/i;

function isPrivateOrLocalHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/\.$/, "");
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host === "::1" || host === "[::1]") return true;
  if (/^\[?(?:fc|fd|fe80:)/i.test(host)) return true;
  if (/^127(?:\.\d{1,3}){3}$/.test(host) || /^0\.0\.0\.0$/.test(host)) return true;
  const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!ipv4) return false;
  const [a, b] = ipv4.slice(1).map(Number);
  return a === 10 || a === 127 || a === 0 || a === 169 && b === 254 || a === 192 && b === 168 || a === 172 && b >= 16 && b <= 31;
}

export function reviewOAuthDestination(providerId: string, authorizeUrl: string): OAuthDestinationReview {
  const policy = destinationPolicies[providerId];
  if (!policy) throw new Error("This provider does not have a reviewed OAuth destination policy.");
  if (!authorizeUrl || /^\s*\/\//.test(authorizeUrl)) throw new Error("OAuth destination is malformed.");
  let destination: URL;
  try {
    destination = new URL(authorizeUrl);
  } catch {
    throw new Error("OAuth destination is malformed.");
  }
  if (destination.protocol !== "https:" || destination.username || destination.password || destination.port || isPrivateOrLocalHost(destination.hostname)) {
    throw new Error("OAuth destination is not an approved HTTPS provider address.");
  }
  const host = destination.hostname.toLowerCase();
  if (!policy.hosts.includes(host) || !policy.path(destination.pathname)) {
    throw new Error("OAuth destination does not match the selected provider.");
  }
  return { authorizeUrl, providerId, host, path: destination.pathname };
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") return value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean).slice(0, 20);
  return Array.isArray(value) ? value.map((item) => String(item).trim()).filter(Boolean).slice(0, 20) : [];
}

function safeEndpointHost(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port || isPrivateOrLocalHost(url.hostname)) return null;
    return url.hostname.toLowerCase();
  } catch {
    return null;
  }
}

function flattenedEntries(value: Record<string, unknown>, prefix = "", depth = 0): Array<[string, unknown]> {
  if (depth > 3) return [];
  return Object.entries(value).flatMap(([key, item]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (item && typeof item === "object" && !Array.isArray(item)) return flattenedEntries(item as Record<string, unknown>, path, depth + 1);
    return [[path, item]];
  });
}

export function reviewConnectionDraft(draft: string): CredentialImportReview {
  let value: unknown;
  try {
    value = JSON.parse(draft);
  } catch {
    throw new Error("Paste one valid JSON object before reviewing it.");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Credential data must be a JSON object.");
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  if (keys.length === 0 || keys.length > 80) throw new Error("Credential JSON is empty or has an unsupported shape.");
  const entries = flattenedEntries(record);
  if (entries.length === 0 || entries.length > 100) throw new Error("Credential JSON has an unsupported nested shape.");
  const kind = String(record.schema ?? "") === "rumi.connection.credential_bundle.v1"
    || keys.some((key) => /^(?:access_token|refresh_token|api_token|token)$/i.test(key))
    ? "connection_import"
    : "oauth_client";
  if (kind === "oauth_client" && !keys.some((key) => /^(?:client_id|installed|web)$/i.test(key))) {
    throw new Error("OAuth client JSON is missing a recognizable client identifier.");
  }
  const endpoints = entries
    .filter(([key]) => endpointFieldPattern.test(key.slice(key.lastIndexOf(".") + 1)))
    .map(([, item]) => safeEndpointHost(item))
    .filter((host): host is string => Boolean(host));
  const scopes = entries.filter(([key]) => scopeFieldPattern.test(key.slice(key.lastIndexOf(".") + 1))).flatMap(([, item]) => stringList(item));
  const isSecretField = (key: string) => secretFieldPattern.test(key.slice(key.lastIndexOf(".") + 1));
  return {
    kind,
    fields: entries.map(([key]) => key).filter((key) => !isSecretField(key)).sort().slice(0, 20),
    scopes: [...new Set(scopes)],
    endpoints: [...new Set(endpoints)],
    secretFieldCount: entries.filter(([key]) => isSecretField(key)).length,
  };
}
