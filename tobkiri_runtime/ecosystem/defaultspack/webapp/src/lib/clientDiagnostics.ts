import { api } from "./api";

export const CLIENT_DIAGNOSTIC_SCHEMA_VERSION = "rumi.client_diagnostic.v2";
export const CLIENT_DIAGNOSTIC_MAX_PAYLOAD_BYTES = 8 * 1024;
export const CLIENT_DIAGNOSTIC_PRIVACY_STORAGE_KEY = "tobkiri.client_diagnostics.privacy.v1";

export type ClientDiagnosticPrivacyMode = "standard" | "local_only" | "private" | "disabled";

export type ClientDiagnosticInput = {
  source?: string;
  category?: string;
  level?: "info" | "warning" | "error" | string;
  message: string;
  fingerprint?: string;
  conversationId?: string | null;
  detail?: unknown;
  privacyMode?: ClientDiagnosticPrivacyMode;
  reportingEnabled?: boolean;
};

export type ClientDiagnosticDetailV2 = {
  error_name?: string;
  error_code?: string;
  route?: string;
  line?: number;
  column?: number;
  stack?: string;
  component_stack?: string;
  reason_type?: string;
  http_status?: number;
  frame_count?: number;
};

export type ClientDiagnosticPayloadV2 = {
  schema_version: typeof CLIENT_DIAGNOSTIC_SCHEMA_VERSION;
  event_id: string;
  session_id: string;
  source: string;
  category: string;
  level: "info" | "warning" | "error";
  message: string;
  fingerprint: string;
  context_id?: string;
  privacy_mode: "standard";
  detail: ClientDiagnosticDetailV2;
};

const DIAGNOSTIC_TTL_MS = 30_000;
const DIAGNOSTIC_RATE_WINDOW_MS = 60_000;
const DIAGNOSTIC_RATE_LIMIT = 20;
const sentDiagnostics = new Map<string, number>();
const recentDiagnosticAttempts: number[] = [];
let listenersInstalled = false;
let diagnosticSessionId = "";

type DiagnosticPreferenceStorage = Pick<Storage, "getItem" | "setItem">;

function browserPreferenceStorage(): DiagnosticPreferenceStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isStoredPrivacyMode(
  value: unknown,
): value is Extract<ClientDiagnosticPrivacyMode, "standard" | "local_only" | "disabled"> {
  return value === "standard" || value === "local_only" || value === "disabled";
}

export function readClientDiagnosticPrivacyMode(
  storage: DiagnosticPreferenceStorage | null = browserPreferenceStorage(),
): ClientDiagnosticPrivacyMode {
  if (!storage) return "local_only";
  try {
    const value = storage.getItem(CLIENT_DIAGNOSTIC_PRIVACY_STORAGE_KEY);
    return isStoredPrivacyMode(value) ? value : "local_only";
  } catch {
    return "local_only";
  }
}

export function writeClientDiagnosticPrivacyMode(
  mode: ClientDiagnosticPrivacyMode,
  storage: DiagnosticPreferenceStorage | null = browserPreferenceStorage(),
): ClientDiagnosticPrivacyMode {
  const normalized = isStoredPrivacyMode(mode) ? mode : "local_only";
  try {
    storage?.setItem(CLIENT_DIAGNOSTIC_PRIVACY_STORAGE_KEY, normalized);
  } catch {
    return "local_only";
  }
  return storage ? normalized : "local_only";
}

const AUTOMATIC_DIAGNOSTIC_CATEGORIES = new Set([
  "window_error",
  "promise_rejection",
  "render_crash",
]);

const APP_FRAME_MARKERS = ["/src/", "/assets/", "/static/", "/webapp/"];

function safeString(value: unknown, fallback = ""): string {
  try {
    return String(value ?? fallback);
  } catch {
    return fallback;
  }
}

function compactText(value: unknown, fallback: string, maxLength = 500): string {
  const text = safeString(value, fallback).trim().replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ");
  return (text || fallback).slice(0, maxLength);
}

function normalizeSlug(value: unknown, fallback: string, maxLength: number): string {
  const normalized = compactText(value, fallback, maxLength)
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return (normalized || fallback).slice(0, maxLength);
}

function normalizedLevel(value: unknown): ClientDiagnosticPayloadV2["level"] {
  return value === "info" || value === "warning" || value === "error" ? value : "error";
}

function stableOpaqueHash(value: unknown): string {
  const text = safeString(value);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function opaqueId(prefix: string, value: unknown): string {
  return `${prefix}_${stableOpaqueHash(value)}`;
}

function randomOpaqueId(prefix: string): string {
  try {
    return `${prefix}_${crypto.randomUUID().replace(/-/g, "")}`;
  } catch {
    return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  }
}

function sessionId(): string {
  if (!diagnosticSessionId) diagnosticSessionId = randomOpaqueId("session");
  return diagnosticSessionId;
}

export function redactDiagnosticText(value: unknown, maxLength = 500): string {
  let text = safeString(value)
    .normalize("NFKC")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]+/g, " ");

  text = text
    .replace(/\bdata:[^\s,;]+,[^\s]+/gi, "[data-url]")
    .replace(/\b(?:https?|wss?|file):\/\/[^\s<>'\"\])}]+/gi, "[url]")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[email]")
    .replace(/\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n,;]+/gi, "[auth-header]")
    .replace(/\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi, "[credential]")
    .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, "[credential]")
    .replace(/\b(?:sk|rk|pk|ghp|github_pat|xox[baprs]|ya29)[-_][A-Za-z0-9_-]{8,}\b/gi, "[credential]")
    .replace(/\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|password|passwd|credential|session[_-]?id|csrf[_-]?token)\b\s*[:=]\s*[\"']?[^\s,;\"'\]}]+/gi, "[credential]")
    .replace(/\b[A-Za-z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n\s):]*/g, "[path]")
    .replace(/(?:\/Users\/|\/home\/|\/var\/|\/tmp\/|\/private\/|\/opt\/|\/etc\/)[^\s):\]}]+/g, "[path]")
    .replace(/\b(?:[A-Fa-f0-9]{40,}|[A-Za-z0-9+/=_-]{80,})\b/g, "[opaque]")
    .replace(/\s+/g, " ")
    .trim();

  return text.slice(0, maxLength);
}

function normalizedIdentifier(value: unknown, fallback = ""): string {
  const text = redactDiagnosticText(value, 80).replace(/[^A-Za-z0-9._:-]+/g, "_");
  return text || fallback;
}

function finiteInteger(value: unknown, min = 0, max = 10_000_000): number | undefined {
  const number = Number(value);
  if (!Number.isFinite(number)) return undefined;
  const integer = Math.trunc(number);
  return integer >= min && integer <= max ? integer : undefined;
}

function appRoute(value: unknown): string | undefined {
  const text = safeString(value).trim();
  if (!text) return undefined;
  try {
    const url = new URL(text, "http://rumi.invalid");
    const path = url.pathname;
    if (APP_FRAME_MARKERS.some((marker) => path.includes(marker))) {
      return path.slice(0, 240);
    }
    if (text.startsWith("/") && !text.startsWith("//")) {
      return path.slice(0, 240);
    }
    return "[url]";
  } catch {
    const redacted = redactDiagnosticText(text, 240);
    return redacted || undefined;
  }
}

function applicationFrame(line: string): string | null {
  if (/node_modules|chrome-extension:|moz-extension:/i.test(line)) return null;
  let normalized = line.replace(
    /\bhttps?:\/\/[^/\s)]+(\/(?:src|assets|static|webapp)\/[^?\s#)]*)(?:[?#][^\s)]*)?/gi,
    "$1",
  );
  if (!APP_FRAME_MARKERS.some((marker) => normalized.includes(marker))) {
    const component = normalized.match(/^\s*at\s+([A-Za-z0-9_$.[\]-]{1,100})(?:\s|$)/);
    return component ? `at ${component[1]}` : null;
  }
  normalized = redactDiagnosticText(normalized, 240);
  return normalized || null;
}

export function normalizeDiagnosticStack(value: unknown): string | undefined {
  const raw = safeString(value);
  if (!raw) return undefined;
  const frames: string[] = [];
  for (const line of raw.split(/\r?\n/).slice(0, 40)) {
    const frame = applicationFrame(line);
    if (!frame || frames.includes(frame)) continue;
    frames.push(frame);
    if (frames.length >= 12) break;
  }
  return frames.length ? frames.join("\n") : undefined;
}

function normalizeComponentStack(value: unknown): string | undefined {
  const raw = safeString(value);
  if (!raw) return undefined;
  const frames = raw.split(/\r?\n/)
    .map((line) => line.match(/^\s*at\s+([A-Za-z0-9_$.[\]-]{1,100})/)?.[1])
    .filter((name): name is string => Boolean(name))
    .slice(0, 12)
    .map((name) => `at ${name}`);
  return frames.length ? frames.join("\n") : undefined;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function sanitizeDiagnosticDetail(value: unknown): ClientDiagnosticDetailV2 {
  const record = recordValue(value);
  const detail: ClientDiagnosticDetailV2 = {};
  const errorName = normalizedIdentifier(record.error_name ?? record.name);
  const errorCode = normalizedIdentifier(record.error_code ?? record.code);
  const route = appRoute(record.route ?? record.filename);
  const line = finiteInteger(record.line ?? record.lineno);
  const column = finiteInteger(record.column ?? record.colno);
  const stack = normalizeDiagnosticStack(record.stack);
  const componentStack = normalizeComponentStack(record.component_stack ?? record.componentStack);
  const reasonType = normalizedIdentifier(record.reason_type);
  const httpStatus = finiteInteger(record.http_status ?? record.status, 100, 599);
  const frameCount = finiteInteger(record.frame_count, 0, 10_000);

  if (errorName) detail.error_name = errorName;
  if (errorCode) detail.error_code = errorCode;
  if (route) detail.route = route;
  if (line !== undefined) detail.line = line;
  if (column !== undefined) detail.column = column;
  if (stack) detail.stack = stack;
  if (componentStack) detail.component_stack = componentStack;
  if (reasonType) detail.reason_type = reasonType;
  if (httpStatus !== undefined) detail.http_status = httpStatus;
  if (frameCount !== undefined) detail.frame_count = frameCount;
  return detail;
}

function genericAutomaticMessage(category: string): string | null {
  if (category === "window_error") return "Unhandled window error";
  if (category === "promise_rejection") return "Unhandled promise rejection";
  if (category === "render_crash") return "React render failure";
  return null;
}

function safeMessage(input: ClientDiagnosticInput, category: string): string {
  const generic = AUTOMATIC_DIAGNOSTIC_CATEGORIES.has(category)
    ? genericAutomaticMessage(category)
    : null;
  return generic ?? (redactDiagnosticText(input.message || "Frontend diagnostic", 320) || "Frontend diagnostic");
}

function payloadBytes(payload: ClientDiagnosticPayloadV2): number {
  try {
    return new TextEncoder().encode(JSON.stringify(payload)).byteLength;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

function boundPayload(payload: ClientDiagnosticPayloadV2): ClientDiagnosticPayloadV2 {
  if (payloadBytes(payload) <= CLIENT_DIAGNOSTIC_MAX_PAYLOAD_BYTES) return payload;
  const bounded: ClientDiagnosticPayloadV2 = {
    ...payload,
    message: payload.message.slice(0, 160),
    detail: {
      error_name: payload.detail.error_name,
      error_code: payload.detail.error_code,
      route: payload.detail.route,
      line: payload.detail.line,
      column: payload.detail.column,
      reason_type: payload.detail.reason_type,
      http_status: payload.detail.http_status,
      frame_count: payload.detail.frame_count,
    },
  };
  if (payloadBytes(bounded) <= CLIENT_DIAGNOSTIC_MAX_PAYLOAD_BYTES) return bounded;
  return { ...bounded, context_id: undefined, detail: {} };
}

function privacyAllowsRemoteReporting(input: ClientDiagnosticInput): boolean {
  if (input.reportingEnabled === false) return false;
  const browserMode = browserPreferenceStorage()
    ? readClientDiagnosticPrivacyMode()
    : "standard";
  if (browserMode !== "standard") return false;
  return (input.privacyMode ?? "standard") === "standard";
}

export function diagnosticFingerprint(input: ClientDiagnosticInput): string {
  const source = normalizeSlug(input.source, "webapp", 80);
  const category = normalizeSlug(input.category, "frontend", 80);
  const detail = sanitizeDiagnosticDetail(input.detail);
  const message = safeMessage(input, category);
  const contextId = input.conversationId ? opaqueId("ctx", input.conversationId) : "";
  const seed = input.fingerprint || [
    source,
    category,
    message,
    contextId,
    detail.error_name ?? "",
    detail.error_code ?? "",
    detail.route ?? "",
    detail.line ?? "",
    detail.column ?? "",
  ].join(":");
  return opaqueId("diag", seed);
}

function buildClientDiagnosticPayload(input: ClientDiagnosticInput): ClientDiagnosticPayloadV2 {
  const source = normalizeSlug(input.source, "webapp", 80);
  const category = normalizeSlug(input.category, "frontend", 80);
  const payload: ClientDiagnosticPayloadV2 = {
    schema_version: CLIENT_DIAGNOSTIC_SCHEMA_VERSION,
    event_id: randomOpaqueId("event"),
    session_id: sessionId(),
    source,
    category,
    level: normalizedLevel(input.level),
    message: safeMessage(input, category),
    fingerprint: diagnosticFingerprint(input),
    context_id: input.conversationId ? opaqueId("ctx", input.conversationId) : undefined,
    privacy_mode: "standard",
    detail: sanitizeDiagnosticDetail(input.detail),
  };
  return boundPayload(payload);
}

export function prepareClientDiagnostic(input: ClientDiagnosticInput): ClientDiagnosticPayloadV2 | null {
  if (!privacyAllowsRemoteReporting(input)) return null;
  return buildClientDiagnosticPayload(input);
}

export function previewClientDiagnostic(input: ClientDiagnosticInput): ClientDiagnosticPayloadV2 {
  return buildClientDiagnosticPayload({ ...input, privacyMode: "standard" });
}

function pruneDiagnosticState(now: number) {
  for (const [fingerprint, timestamp] of sentDiagnostics.entries()) {
    if (now - timestamp > DIAGNOSTIC_TTL_MS) sentDiagnostics.delete(fingerprint);
  }
  while (recentDiagnosticAttempts.length && now - recentDiagnosticAttempts[0] > DIAGNOSTIC_RATE_WINDOW_MS) {
    recentDiagnosticAttempts.shift();
  }
}

export type ClientDiagnosticReportResult = { recorded: boolean; diagnosticId?: string };

export async function reportClientDiagnosticResult(input: ClientDiagnosticInput): Promise<ClientDiagnosticReportResult> {
  const normalized = prepareClientDiagnostic(input);
  if (!normalized) return { recorded: false };
  const now = Date.now();
  pruneDiagnosticState(now);
  if (recentDiagnosticAttempts.length >= DIAGNOSTIC_RATE_LIMIT) return { recorded: false };
  const lastSentAt = sentDiagnostics.get(normalized.fingerprint);
  if (lastSentAt && now - lastSentAt < DIAGNOSTIC_TTL_MS) return { recorded: false };

  recentDiagnosticAttempts.push(now);
  sentDiagnostics.set(normalized.fingerprint, now);
  try {
    const acknowledgment = await api.reportClientEvent(normalized);
    if (!acknowledgment || typeof acknowledgment !== "object" || acknowledgment.recorded !== true) {
      throw new Error("diagnostic was not acknowledged");
    }
    return { recorded: true, diagnosticId: normalizedIdentifier(acknowledgment.diagnostic_id) || normalized.event_id };
  } catch {
    sentDiagnostics.delete(normalized.fingerprint);
    return { recorded: false };
  }
}

export async function reportClientDiagnostic(input: ClientDiagnosticInput): Promise<boolean> {
  return (await reportClientDiagnosticResult(input)).recorded;
}

export function installGlobalClientDiagnostics(
  context?: () => Partial<Omit<ClientDiagnosticInput, "message">>,
): () => void {
  if (listenersInstalled || typeof window === "undefined") return () => undefined;
  listenersInstalled = true;
  const sharedContext = () => context?.() ?? {};

  const onError = (event: ErrorEvent) => {
    const error = event.error instanceof Error ? event.error : null;
    void reportClientDiagnostic({
      ...sharedContext(),
      source: "window.error",
      category: "window_error",
      level: "error",
      message: "Unhandled window error",
      fingerprint: [error?.name ?? "ErrorEvent", event.filename, event.lineno, event.colno].join(":"),
      detail: {
        error_name: error?.name ?? "ErrorEvent",
        error_code: recordValue(error).code,
        filename: event.filename,
        lineno: event.lineno,
        colno: event.colno,
        stack: error?.stack,
      },
    });
  };

  const onUnhandledRejection = (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    const error = reason instanceof Error ? reason : null;
    void reportClientDiagnostic({
      ...sharedContext(),
      source: "window.unhandledrejection",
      category: "promise_rejection",
      level: "error",
      message: "Unhandled promise rejection",
      fingerprint: `${error?.name ?? typeof reason}:${normalizeDiagnosticStack(error?.stack) ?? "no-app-frame"}`,
      detail: {
        error_name: error?.name,
        error_code: recordValue(error).code,
        stack: error?.stack,
        reason_type: error?.name ?? typeof reason,
      },
    });
  };

  window.addEventListener("error", onError);
  window.addEventListener("unhandledrejection", onUnhandledRejection);
  return () => {
    listenersInstalled = false;
    window.removeEventListener("error", onError);
    window.removeEventListener("unhandledrejection", onUnhandledRejection);
  };
}
