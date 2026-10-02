import { defaultspackUrlWithLocalAuthToken } from "./defaultspackLocalAuth";

const PANEL_CSRF_STORAGE_KEY = "rumi-panel-csrf";
const DEFAULTSPACK_CSRF_STORAGE_KEY = "rumi-defaultspack-csrf";
const DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY = "rumi-defaultspack-local-auth";
const DEFAULTSPACK_LOCAL_AUTH_FRAGMENT_KEY = "rumi_local_auth";
let defaultspackLocalAuthMemoryToken = "";
let defaultspackLocalAuthBootstrapped = false;

function isUnsafeHttpMethod(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

function sessionStorageOrNull(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

function generateCsrfToken(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `csrf-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function getDefaultspackCsrfToken(): string {
  const storage = sessionStorageOrNull();
  const panelToken = storage?.getItem(PANEL_CSRF_STORAGE_KEY);
  if (panelToken?.trim()) return panelToken;
  const stored = storage?.getItem(DEFAULTSPACK_CSRF_STORAGE_KEY);
  if (stored) return stored;
  const token = generateCsrfToken();
  try {
    storage?.setItem(DEFAULTSPACK_CSRF_STORAGE_KEY, token);
  } catch {
    // A nonempty per-request token still satisfies the local CSRF guard.
  }
  return token;
}

function consumeDefaultspackLocalAuthFromLocation(): string {
  if (typeof window === "undefined") return "";
  const storage = sessionStorageOrNull();
  try {
    const rawHash = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : window.location.hash;
    if (!rawHash) return defaultspackLocalAuthMemoryToken || storage?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() || "";
    const params = new URLSearchParams(rawHash);
    const token = params.get(DEFAULTSPACK_LOCAL_AUTH_FRAGMENT_KEY)?.trim() ?? "";
    if (!token) return defaultspackLocalAuthMemoryToken || storage?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() || "";
    defaultspackLocalAuthMemoryToken = token;
    storage?.setItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY, token);
    params.delete(DEFAULTSPACK_LOCAL_AUTH_FRAGMENT_KEY);
    const nextHash = params.toString();
    const nextUrl = `${window.location.pathname}${window.location.search}${nextHash ? `#${nextHash}` : ""}`;
    window.history.replaceState(window.history.state, document.title, nextUrl);
    return token;
  } catch {
    return storage?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() ?? "";
  }
}

export function bootstrapDefaultspackLocalAuth(): string {
  const stored = sessionStorageOrNull()?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() || "";
  if (defaultspackLocalAuthBootstrapped) {
    if (defaultspackLocalAuthMemoryToken || stored) return defaultspackLocalAuthMemoryToken || stored;
  }
  defaultspackLocalAuthBootstrapped = true;
  const consumed = consumeDefaultspackLocalAuthFromLocation();
  if (consumed) {
    defaultspackLocalAuthMemoryToken = consumed;
    return consumed;
  }
  defaultspackLocalAuthMemoryToken = stored;
  return defaultspackLocalAuthMemoryToken;
}

function getDefaultspackLocalAuthToken(): string {
  return bootstrapDefaultspackLocalAuth();
}

bootstrapDefaultspackLocalAuth();

export function defaultspackApiHeaders(method: string, headers?: HeadersInit): Headers {
  const nextHeaders = new Headers(headers);
  if (!nextHeaders.has("Content-Type")) {
    nextHeaders.set("Content-Type", "application/json");
  }
  if (!nextHeaders.has("Authorization")) {
    const token = getDefaultspackLocalAuthToken();
    if (token) nextHeaders.set("Authorization", `Bearer ${token}`);
  }
  const csrfHeader = nextHeaders.get("X-Rumi-CSRF");
  if (isUnsafeHttpMethod(method) && (!csrfHeader || !csrfHeader.trim())) {
    nextHeaders.set("X-Rumi-CSRF", getDefaultspackCsrfToken());
  }
  return nextHeaders;
}

export function defaultspackUrlWithLocalAuth(pathOrUrl: string): string {
  return defaultspackUrlWithLocalAuthToken(pathOrUrl, getDefaultspackLocalAuthToken());
}

