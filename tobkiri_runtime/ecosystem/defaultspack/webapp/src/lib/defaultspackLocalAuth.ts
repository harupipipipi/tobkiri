const LEGACY_LOCAL_AUTH_STORAGE_KEY = "rumi-defaultspack-local-auth";
const LEGACY_LOCAL_AUTH_URL_KEY = "rumi_local_auth";
const LOCAL_AUTH_SCOPE = "defaultspack-local-ui";
const CHILD_REQUEST_TYPE = "rumi:local-auth:request";
const CHILD_RESPONSE_TYPE = "rumi:local-auth:response";

export type LocalAuthBinding = {
  origin: string;
  window_id: string;
  process_id: string;
  device_id: string;
  nonce: string;
  scope: string;
};

type LocalAuthExchange = LocalAuthBinding & {
  exchange_code: string;
  expires_at: number;
};

type LocalAuthSession = {
  binding: LocalAuthBinding;
  token: string;
  expiresAt: number;
};

type TauriInternals = {
  invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
};

let session: LocalAuthSession | null = null;
let initialization: Promise<void> | null = null;
let nativeExchangeUnavailable = false;

function browserOrigin(): string {
  try {
    return typeof window === "undefined" ? "" : window.location.origin;
  } catch {
    return "";
  }
}

function secureId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(24));
    return `${prefix}-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
  }
  return "";
}

function removeLegacyStorage(): void {
  for (const key of ["sessionStorage", "localStorage"] as const) {
    try {
      const storage = globalThis[key];
      storage?.removeItem(LEGACY_LOCAL_AUTH_STORAGE_KEY);
    } catch {
      console.warn("Defaultspack local auth legacy storage cleanup was unavailable.");
    }
  }
}

function stripLegacyCredentialsFromLocation(): void {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    let changed = false;
    if (url.searchParams.has(LEGACY_LOCAL_AUTH_URL_KEY)) {
      url.searchParams.delete(LEGACY_LOCAL_AUTH_URL_KEY);
      changed = true;
    }
    const hash = new URLSearchParams(url.hash.startsWith("#") ? url.hash.slice(1) : url.hash);
    if (hash.has(LEGACY_LOCAL_AUTH_URL_KEY)) {
      hash.delete(LEGACY_LOCAL_AUTH_URL_KEY);
      url.hash = hash.toString();
      changed = true;
    }
    if (changed) {
      window.history.replaceState(window.history.state, document.title, `${url.pathname}${url.search}${url.hash}`);
    }
  } catch {
    try {
      window.history.replaceState(window.history.state, document.title, window.location.pathname);
    } catch {
      console.warn("Defaultspack local auth removed an unreadable legacy route.");
    }
  }
}

/** Revoke legacy browser persistence before any app code can consume it. */
export function cleanupLegacyDefaultspackLocalAuth(): void {
  removeLegacyStorage();
  stripLegacyCredentialsFromLocation();
}

export function safeDefaultspackLocalPath(pathOrUrl: string): string {
  const input = String(pathOrUrl ?? "").trim();
  const origin = browserOrigin();
  if (
    !input || !origin || !input.startsWith("/") || input.startsWith("//") || input.includes("\\")
    || /[\u0000-\u001F\u007F]/.test(input) || /%(?![0-9A-Fa-f]{2})/.test(input)
  ) {
    throw new TypeError("Defaultspack destination must be an absolute same-origin path");
  }
  let url: URL;
  try {
    url = new URL(input, origin);
  } catch {
    throw new TypeError("Defaultspack destination is invalid");
  }
  if (
    url.origin !== origin
    || !["http:", "https:"].includes(url.protocol)
    || url.username
    || url.password
    || url.hash
    || url.searchParams.has(LEGACY_LOCAL_AUTH_URL_KEY)
  ) {
    throw new TypeError("Defaultspack destination is not supported");
  }
  return `${url.pathname}${url.search}`;
}

/** Compatibility name: the token is intentionally ignored and never added to a URL. */
export function defaultspackUrlWithLocalAuthToken(pathOrUrl: string, _token: string): string {
  return safeDefaultspackLocalPath(pathOrUrl);
}

export function defaultspackUrlWithStoredLocalAuth(pathOrUrl: string): string {
  return safeDefaultspackLocalPath(pathOrUrl);
}

function normalizeExchange(value: unknown): LocalAuthExchange {
  const candidate = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const exchange = {
    exchange_code: String(candidate.exchange_code ?? "").trim(),
    expires_at: Number(candidate.expires_at),
    origin: String(candidate.origin ?? "").trim(),
    window_id: String(candidate.window_id ?? "").trim(),
    process_id: String(candidate.process_id ?? "").trim(),
    device_id: String(candidate.device_id ?? "").trim(),
    nonce: String(candidate.nonce ?? "").trim(),
    scope: String(candidate.scope ?? "").trim(),
  };
  if (
    !exchange.exchange_code || !Number.isFinite(exchange.expires_at)
    || exchange.expires_at * 1000 <= Date.now() || exchange.origin !== browserOrigin()
    || !exchange.window_id || !exchange.process_id || !exchange.device_id || !exchange.nonce
    || exchange.scope !== LOCAL_AUTH_SCOPE
  ) {
    throw new Error("LOCAL_AUTH_EXCHANGE_INVALID");
  }
  return exchange;
}

function bindingHeaders(binding: LocalAuthBinding): Headers {
  return new Headers({
    "X-Rumi-Local-Auth-Window": binding.window_id,
    "X-Rumi-Local-Auth-Process": binding.process_id,
    "X-Rumi-Local-Auth-Device": binding.device_id,
    "X-Rumi-Local-Auth-Nonce": binding.nonce,
    "X-Rumi-Local-Auth-Scope": binding.scope,
  });
}

function exchangeBinding(exchange: LocalAuthExchange): LocalAuthBinding {
  return {
    origin: exchange.origin,
    window_id: exchange.window_id,
    process_id: exchange.process_id,
    device_id: exchange.device_id,
    nonce: exchange.nonce,
    scope: exchange.scope,
  };
}

async function redeemExchange(value: unknown): Promise<void> {
  const exchange = normalizeExchange(value);
  const binding = exchangeBinding(exchange);
  const csrf = secureId("csrf");
  if (!csrf) throw new Error("LOCAL_AUTH_RANDOM_UNAVAILABLE");
  const response = await fetch("/api/local-auth/exchange/redeem", {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Rumi-CSRF": csrf },
    body: JSON.stringify({ ...binding, exchange_code: exchange.exchange_code }),
  });
  const payload = await response.json() as { data?: { session_token?: unknown; expires_at?: unknown } };
  const token = String(payload.data?.session_token ?? "").trim();
  const expiresAt = Number(payload.data?.expires_at) * 1000;
  if (!response.ok || !token || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    throw new Error("LOCAL_AUTH_REDEEM_FAILED");
  }
  session = { binding, token, expiresAt };
}

async function nativeExchange(): Promise<unknown> {
  if (nativeExchangeUnavailable) return null;
  const internals = (globalThis as typeof globalThis & { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  if (typeof internals?.invoke !== "function") return null;
  try {
    return await internals.invoke("defaultspack_local_auth_exchange");
  } catch {
    // The verified Profile Shell is a separate minimal Tauri process. It has
    // already exchanged its one-time bootstrap code for an HttpOnly panel
    // cookie and intentionally exposes no Launcher commands. Auxiliary
    // Launcher-owned windows do expose this command and use the scoped
    // in-memory handoff below.
    nativeExchangeUnavailable = true;
    return null;
  }
}

async function childExchange(): Promise<unknown> {
  if (typeof window === "undefined" || !window.opener || window.opener === window) return null;
  const requestId = secureId("request");
  if (!requestId) throw new Error("LOCAL_AUTH_RANDOM_UNAVAILABLE");
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      window.removeEventListener("message", onMessage);
      reject(new Error("LOCAL_AUTH_PARENT_TIMEOUT"));
    }, 5000);
    const onMessage = (event: MessageEvent) => {
      const data = event.data as Record<string, unknown> | null;
      if (event.origin !== window.location.origin || event.source !== window.opener
          || data?.type !== CHILD_RESPONSE_TYPE || data.request_id !== requestId) return;
      window.clearTimeout(timeout);
      window.removeEventListener("message", onMessage);
      resolve(data.exchange);
    };
    window.addEventListener("message", onMessage);
    window.opener.postMessage({ type: CHILD_REQUEST_TYPE, request_id: requestId }, window.location.origin);
  });
}

async function issueChildExchange(): Promise<LocalAuthExchange> {
  const current = activeLocalAuthSession();
  if (!current) throw new Error("LOCAL_AUTH_SESSION_UNAVAILABLE");
  const nonce = secureId("nonce");
  const windowId = secureId("child-window");
  if (!nonce || !windowId) throw new Error("LOCAL_AUTH_RANDOM_UNAVAILABLE");
  const binding: LocalAuthBinding = {
    ...current.binding,
    window_id: windowId,
    nonce,
  };
  const headers = bindingHeaders(current.binding);
  headers.set("Authorization", `Bearer ${current.token}`);
  headers.set("Content-Type", "application/json");
  headers.set("X-Rumi-CSRF", secureId("csrf"));
  const response = await fetch("/api/local-auth/exchange", {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers,
    body: JSON.stringify(binding),
  });
  const payload = await response.json() as { data?: Record<string, unknown> };
  if (!response.ok) throw new Error("LOCAL_AUTH_CHILD_ISSUE_FAILED");
  return normalizeExchange({ ...binding, ...payload.data });
}

function installChildExchangeBroker(): void {
  if (typeof window === "undefined") return;
  window.addEventListener("message", (event) => {
    const data = event.data as Record<string, unknown> | null;
    if (event.origin !== window.location.origin || !event.source || data?.type !== CHILD_REQUEST_TYPE) return;
    const requestId = String(data.request_id ?? "");
    if (!requestId || requestId.length > 160) return;
    const source = event.source as WindowProxy;
    void issueChildExchange().then((exchange) => {
      source.postMessage({ type: CHILD_RESPONSE_TYPE, request_id: requestId, exchange }, event.origin);
    }).catch(() => {
      source.postMessage({ type: CHILD_RESPONSE_TYPE, request_id: requestId, exchange: null }, event.origin);
    });
  });
}

function activeLocalAuthSession(): LocalAuthSession | null {
  if (!session || session.expiresAt <= Date.now()) {
    session = null;
    return null;
  }
  return session;
}

export async function initializeDefaultspackLocalAuth(): Promise<void> {
  if (activeLocalAuthSession()) return;
  if (!initialization) {
    initialization = (async () => {
      cleanupLegacyDefaultspackLocalAuth();
      const exchange = await nativeExchange() ?? await childExchange();
      if (exchange) await redeemExchange(exchange);
    })().finally(() => {
      initialization = null;
    });
  }
  await initialization;
}

export function applyDefaultspackLocalAuthHeaders(headers: Headers): void {
  const current = activeLocalAuthSession();
  if (!current || headers.has("Authorization")) return;
  headers.set("Authorization", `Bearer ${current.token}`);
  bindingHeaders(current.binding).forEach((value, key) => headers.set(key, value));
}

export function resetDefaultspackLocalAuthForTests(): void {
  session = null;
  initialization = null;
  nativeExchangeUnavailable = false;
}

cleanupLegacyDefaultspackLocalAuth();
installChildExchangeBroker();
