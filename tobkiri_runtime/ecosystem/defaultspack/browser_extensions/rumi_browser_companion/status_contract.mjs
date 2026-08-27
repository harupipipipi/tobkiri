const STATUS_STATES = new Set([
  "idle",
  "loading",
  "dirty",
  "saving",
  "saved_unverified",
  "connecting",
  "connected",
  "paused",
  "offline",
  "unauthorized",
  "permission_blocked",
  "not_configured",
  "error"
]);

const STATUS_ACTIONS = new Set(["none", "retry", "re_pair", "open_permissions"]);
const SAFE_STRING_FIELDS = ["serverOrigin", "clientLabel", "profileLabel"];
const SAFE_DATE_FIELDS = ["updatedAt", "lastAttemptAt", "lastSuccessAt"];

function boundedString(value, maximum = 160) {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
}

function safeDate(value) {
  const candidate = boundedString(value, 40);
  return candidate && Number.isFinite(Date.parse(candidate)) ? candidate : "";
}

export function safeServerOrigin(value) {
  try {
    const url = new URL(String(value || ""));
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    return url.origin;
  } catch (_error) {
    return "";
  }
}

export function sanitizeConnectionStatus(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const state = STATUS_STATES.has(source.state) ? source.state : source.ok ? "connected" : "error";
  const action = STATUS_ACTIONS.has(source.action) ? source.action : defaultActionForState(state);
  const status = {
    ok: state === "connected",
    state,
    code: /^[A-Z0-9_]{1,64}$/.test(String(source.code || ""))
      ? String(source.code)
      : defaultCodeForState(state),
    action
  };

  for (const field of SAFE_STRING_FIELDS) {
    const item = field === "serverOrigin"
      ? safeServerOrigin(source[field])
      : boundedString(source[field]);
    if (item) status[field] = item;
  }
  for (const field of SAFE_DATE_FIELDS) {
    const item = safeDate(source[field]);
    if (item) status[field] = item;
  }

  const pollIntervalMinutes = Number(source.pollIntervalMinutes);
  if (Number.isFinite(pollIntervalMinutes)) {
    status.pollIntervalMinutes = Math.min(1440, Math.max(1, Math.round(pollIntervalMinutes)));
  }
  const commandCount = Number(source.commandCount);
  if (Number.isInteger(commandCount) && commandCount >= 0) {
    status.commandCount = Math.min(commandCount, 10000);
  }
  return status;
}

export function bridgeFailureStatus({ responseStatus = 0, error = null, isOnline = true } = {}) {
  const text = boundedString(error instanceof Error ? error.message : error, 500).toLowerCase();
  if (responseStatus === 401 || responseStatus === 403) {
    return { state: "unauthorized", code: "PAIRING_REQUIRED", action: "re_pair" };
  }
  if (text.includes("permission") || text.includes("not allowed") || text.includes("cannot access")) {
    return {
      state: "permission_blocked",
      code: "BROWSER_PERMISSION_BLOCKED",
      action: "open_permissions"
    };
  }
  if (!isOnline || responseStatus === 0 || text.includes("fetch") || text.includes("network")) {
    return { state: "offline", code: "NETWORK_UNAVAILABLE", action: "retry" };
  }
  return { state: "error", code: "BRIDGE_UNAVAILABLE", action: "retry" };
}

export function buildSafeDiagnostics(value) {
  const status = sanitizeConnectionStatus(value);
  const diagnostics = {
    state: status.state,
    code: status.code,
    updated_at: status.updatedAt || null,
    last_attempt_at: status.lastAttemptAt || null,
    last_success_at: status.lastSuccessAt || null,
    polling_minutes: status.pollIntervalMinutes || null,
    companion_version: "1"
  };
  return JSON.stringify(diagnostics, null, 2);
}

export function nextScheduledAttempt(value) {
  const status = sanitizeConnectionStatus(value);
  if (!status.lastAttemptAt || !status.pollIntervalMinutes || status.state === "paused") return "";
  return new Date(
    Date.parse(status.lastAttemptAt) + status.pollIntervalMinutes * 60 * 1000
  ).toISOString();
}

function defaultActionForState(state) {
  if (state === "unauthorized" || state === "not_configured") return "re_pair";
  if (state === "permission_blocked") return "open_permissions";
  if (state === "offline" || state === "error") return "retry";
  return "none";
}

function defaultCodeForState(state) {
  const codes = {
    idle: "IDLE",
    loading: "SETTINGS_LOADING",
    dirty: "UNSAVED_CHANGES",
    saving: "SETTINGS_SAVING",
    saved_unverified: "SETTINGS_SAVED_UNVERIFIED",
    connecting: "CONNECTION_CHECKING",
    connected: "CONNECTED",
    paused: "POLLING_PAUSED",
    offline: "NETWORK_UNAVAILABLE",
    unauthorized: "PAIRING_REQUIRED",
    permission_blocked: "BROWSER_PERMISSION_BLOCKED",
    not_configured: "PAIRING_REQUIRED",
    error: "BRIDGE_UNAVAILABLE"
  };
  return codes[state] || "BRIDGE_UNAVAILABLE";
}
