const STORAGE_KEY = "rumiBrowserCompanionSettings";
const RUNTIME_MESSAGE_TIMEOUT_MS = 20_000;
const MINIMUM_STALE_AFTER_MS = 2 * 60 * 1000;
const DEFAULT_SETTINGS = {
  serverUrl: "http://127.0.0.1:8766",
  pairingToken: "",
  clientLabel: "",
  profileLabel: "",
  pollIntervalMinutes: 1
};

const form = document.getElementById("settings-form");
const saveButton = document.getElementById("save-settings");
const pollNowButton = document.getElementById("poll-now");
const actionPanel = document.getElementById("action-feedback");
const actionStateEl = document.getElementById("action-state");
const actionStateElements = {
  load: document.getElementById("load-state"),
  save: document.getElementById("save-state"),
  poll: document.getElementById("poll-state")
};
const actionStatusEl = document.getElementById("action-status");
const retryButton = document.getElementById("retry-action");
const connectionStateEl = document.getElementById("connection-state");
const statusEl = document.getElementById("status");
const endpointEl = document.getElementById("status-endpoint");
const profileEl = document.getElementById("status-profile");
const lastContactEl = document.getElementById("status-last-contact");
const freshnessEl = document.getElementById("status-freshness");
const updatedEl = document.getElementById("status-updated");
const diagnosticDisclosure = document.getElementById("diagnostic-disclosure");
const diagnosticDetailsEl = document.getElementById("diagnostic-details");
const copyDiagnosticButton = document.getElementById("copy-diagnostic");
const copyFeedbackEl = document.getElementById("copy-feedback");

const inFlight = { load: null, save: null, poll: null };
const actionStates = { load: "idle", save: "idle", poll: "idle" };
let retryAction = null;
let latestBackgroundStatus = null;
let freshnessTimer = null;

document.addEventListener("DOMContentLoaded", () => {
  freshnessTimer = window.setInterval(() => {
    if (latestBackgroundStatus) {
      renderConnectionStatus(latestBackgroundStatus);
    }
  }, 30_000);
  void loadSettings();
});

window.addEventListener("pagehide", () => {
  if (freshnessTimer !== null) {
    window.clearInterval(freshnessTimer);
  }
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void saveSettings();
});

pollNowButton.addEventListener("click", () => {
  void pollNow();
});

retryButton.addEventListener("click", () => {
  const action = retryAction;
  if (action === "load") {
    void loadSettings();
  } else if (action === "save") {
    void saveSettings();
  } else if (action === "poll") {
    void pollNow();
  }
});

copyDiagnosticButton.addEventListener("click", () => {
  void copyDiagnostics();
});

function loadSettings() {
  if (inFlight.load) {
    return inFlight.load;
  }
  const operation = (async () => {
    setActionState("load", "pending", "Loading settings and connection status…");
    try {
      const stored = await chrome.storage.local.get(STORAGE_KEY);
      const settings = {
        ...DEFAULT_SETTINGS,
        ...(stored?.[STORAGE_KEY] || {})
      };
      applySettings(settings);
      const backgroundStatus = await sendRuntimeMessage({ type: "rumi:get-status" });
      latestBackgroundStatus = requireStatusResponse(backgroundStatus);
      renderConnectionStatus(latestBackgroundStatus);
      setActionState("load", "success", "Settings and connection status loaded.");
      return latestBackgroundStatus;
    } catch (error) {
      renderActionError("load", error, "Settings could not be loaded.");
      return null;
    } finally {
      inFlight.load = null;
      updateControls();
    }
  })();
  inFlight.load = operation;
  updateControls();
  return operation;
}

function saveSettings() {
  if (inFlight.save) {
    return inFlight.save;
  }
  const operation = (async () => {
    setActionState("save", "pending", "Saving settings…");
    try {
      const settings = readSettings();
      await chrome.storage.local.set({ [STORAGE_KEY]: settings });
      renderConnectionIdentity(latestBackgroundStatus, settings);
      setActionState("save", "success", "Settings saved. Poll the bridge to verify the connection.");
      return settings;
    } catch (error) {
      renderActionError("save", error, "Settings could not be saved.");
      return null;
    } finally {
      inFlight.save = null;
      updateControls();
    }
  })();
  inFlight.save = operation;
  updateControls();
  return operation;
}

function pollNow() {
  if (inFlight.poll) {
    return inFlight.poll;
  }
  const operation = (async () => {
    setActionState("poll", "pending", "Contacting the Tobkiri bridge…");
    try {
      const result = await sendRuntimeMessage({ type: "rumi:poll-now" });
      latestBackgroundStatus = requireStatusResponse(result);
      renderConnectionStatus(latestBackgroundStatus);
      if (!latestBackgroundStatus.ok) {
        setActionState(
          "poll",
          "error",
          connectionCopy(latestBackgroundStatus).action,
          latestBackgroundStatus.diagnostic
        );
        return latestBackgroundStatus;
      }
      setActionState("poll", "success", "Bridge poll completed successfully.");
      return latestBackgroundStatus;
    } catch (error) {
      renderActionError("poll", error, "The bridge poll could not be completed.");
      return null;
    } finally {
      inFlight.poll = null;
      updateControls();
    }
  })();
  inFlight.poll = operation;
  updateControls();
  return operation;
}

function setActionState(action, state, message, diagnostic = null) {
  actionStates[action] = state;
  actionPanel.dataset.action = action;
  actionPanel.dataset.state = state;
  actionStateEl.textContent = stateLabel(state);
  actionStateEl.className = `state-badge ${state}`;
  actionStateElements[action].textContent = stateLabel(state);
  actionStateElements[action].className = `state-badge ${state}`;
  actionStatusEl.textContent = message;
  retryAction = state === "error" ? action : null;
  retryButton.hidden = retryAction === null;
  if (diagnostic) {
    showDiagnostics(action, diagnostic);
  } else if (state === "error") {
    hideDiagnostics();
  } else if (state !== "error" && !latestBackgroundStatus?.diagnostic) {
    hideDiagnostics();
  }
  updateControls();
}

function renderActionError(action, error, fallbackMessage) {
  const diagnostic = diagnosticFromError(error, action);
  const message = error?.code === "RUNTIME_TIMEOUT"
    ? "The extension background worker timed out. Retry after the extension finishes restarting."
    : error?.code === "MALFORMED_RESPONSE"
      ? "The extension background worker returned an invalid response. Reload the extension, then retry."
      : `${fallbackMessage} The extension background worker may be unavailable; retry the action.`;
  setActionState(action, "error", message, diagnostic);
}

function updateControls() {
  const loading = Boolean(inFlight.load);
  const saving = Boolean(inFlight.save);
  const polling = Boolean(inFlight.poll);
  for (const control of form.elements) {
    if (!(control instanceof HTMLElement)) {
      continue;
    }
    if (control === pollNowButton) {
      control.disabled = loading || saving || polling;
    } else {
      control.disabled = loading || saving;
    }
  }
  retryButton.disabled = loading || saving || polling;
  form.setAttribute("aria-busy", String(loading || saving));
  pollNowButton.setAttribute("aria-busy", String(polling));
  saveButton.textContent = saving ? "Saving…" : "Save Settings";
  pollNowButton.textContent = polling ? "Polling…" : "Poll Bridge Now";
  saveButton.dataset.state = actionStates.save;
  pollNowButton.dataset.state = actionStates.poll;
  form.dataset.loadState = actionStates.load;
}

function applySettings(settings) {
  form.serverUrl.value = String(settings.serverUrl || DEFAULT_SETTINGS.serverUrl);
  form.pairingToken.value = String(settings.pairingToken || "");
  form.clientLabel.value = String(settings.clientLabel || "");
  form.profileLabel.value = String(settings.profileLabel || "");
  form.pollIntervalMinutes.value = normalizePollInterval(settings.pollIntervalMinutes);
}

function readSettings() {
  return {
    serverUrl: String(form.serverUrl.value || "").trim(),
    pairingToken: String(form.pairingToken.value || "").trim(),
    clientLabel: String(form.clientLabel.value || "").trim(),
    profileLabel: String(form.profileLabel.value || "").trim(),
    pollIntervalMinutes: normalizePollInterval(form.pollIntervalMinutes.value)
  };
}

function normalizePollInterval(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 1 ? Math.max(1, Math.round(number)) : 1;
}

async function sendRuntimeMessage(message) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = window.setTimeout(() => {
      reject(createOptionsError("RUNTIME_TIMEOUT", "Runtime message timed out"));
    }, RUNTIME_MESSAGE_TIMEOUT_MS);
  });
  try {
    return await Promise.race([chrome.runtime.sendMessage(message), timeout]);
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function requireStatusResponse(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.ok !== "boolean") {
    throw createOptionsError("MALFORMED_RESPONSE", "Status response was not a valid object");
  }
  return value;
}

function renderConnectionStatus(status) {
  const copy = connectionCopy(status);
  connectionStateEl.textContent = copy.label;
  connectionStateEl.className = `state-badge ${copy.tone}`;
  statusEl.textContent = copy.message;
  statusEl.className = `status-message ${copy.tone}`;
  renderConnectionIdentity(status, readSettings());
  renderTimestamps(status);
  if (status.diagnostic) {
    showDiagnostics("connection", status.diagnostic);
  }
}

function renderConnectionIdentity(status, settings) {
  endpointEl.textContent = safeEndpoint(status?.serverUrl || settings.serverUrl);
  profileEl.textContent = String(
    status?.profileLabel || settings.profileLabel || settings.clientLabel || "Default browser profile"
  );
}

function renderTimestamps(status) {
  const updatedAt = validDate(status?.updatedAt);
  const lastSuccessfulContactAt = validDate(
    status?.lastSuccessfulContactAt || (status?.ok && status?.state === "connected" ? status.updatedAt : null)
  );
  renderTime(updatedEl, updatedAt, "No status update yet");
  renderTime(lastContactEl, lastSuccessfulContactAt, "No successful contact yet");

  if (!lastSuccessfulContactAt) {
    freshnessEl.textContent = "Stale — no successful contact";
    freshnessEl.className = "stale";
    return;
  }
  const pollMinutes = normalizePollInterval(
    status?.pollIntervalMinutes || form.pollIntervalMinutes.value
  );
  const staleAfterMs = Math.max(MINIMUM_STALE_AFTER_MS, pollMinutes * 2 * 60 * 1000);
  const stale = Date.now() - lastSuccessfulContactAt.getTime() > staleAfterMs;
  freshnessEl.textContent = stale ? "Stale — poll again" : "Fresh";
  freshnessEl.className = stale ? "stale" : "fresh";
}

function renderTime(element, date, emptyLabel) {
  element.textContent = "";
  if (!date) {
    element.textContent = emptyLabel;
    element.removeAttribute("datetime");
    return;
  }
  element.dateTime = date.toISOString();
  element.textContent = date.toLocaleString();
}

function validDate(value) {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date : null;
}

function connectionCopy(status) {
  const state = String(status?.state || (status?.ok ? "connected" : "bridge_error"));
  const copies = {
    idle: {
      label: "Not contacted",
      tone: "idle",
      message: "The bridge has not been contacted yet.",
      action: "The bridge has not been contacted yet. Poll when you are ready."
    },
    not_configured: {
      label: "Not configured",
      tone: "error",
      message: "Add a server URL and pairing token, save, then poll again.",
      action: "The bridge is not configured. Add the required settings, save, then retry."
    },
    bridge_offline: {
      label: "Tobkiri offline",
      tone: "error",
      message: "Start Tobkiri and confirm the server URL, then retry.",
      action: "Tobkiri appears offline. Start Tobkiri, confirm the server URL, then retry."
    },
    pairing_rejected: {
      label: "Pairing rejected",
      tone: "error",
      message: "Generate a new pairing token in Tobkiri, save it here, then retry.",
      action: "Pairing was rejected. Generate a new token in Tobkiri, save it, then retry."
    },
    version_incompatible: {
      label: "Version incompatible",
      tone: "error",
      message: "Update Tobkiri and this extension to compatible versions, then retry.",
      action: "The versions are incompatible. Update Tobkiri and this extension, then retry."
    },
    timeout: {
      label: "Connection timed out",
      tone: "error",
      message: "Confirm Tobkiri is running and the URL is correct, then retry.",
      action: "The bridge timed out. Confirm Tobkiri is running and retry."
    },
    malformed_response: {
      label: "Invalid response",
      tone: "error",
      message: "The bridge returned an invalid response. Update or restart Tobkiri, then retry.",
      action: "The bridge returned an invalid response. Update or restart Tobkiri, then retry."
    },
    connected: {
      label: "Connected",
      tone: "success",
      message: status?.commandCount != null
        ? `Connected. ${Number(status.commandCount)} command${Number(status.commandCount) === 1 ? "" : "s"} received.`
        : "Connected to the Tobkiri bridge.",
      action: "Bridge poll completed successfully."
    },
    bridge_error: {
      label: "Connection error",
      tone: "error",
      message: "The bridge could not complete the request. Review diagnostics and retry.",
      action: "The bridge could not complete the request. Review diagnostics and retry."
    }
  };
  return copies[state] || copies.bridge_error;
}

function stateLabel(state) {
  return {
    idle: "Idle",
    pending: "In progress",
    success: "Succeeded",
    error: "Needs attention"
  }[state] || "Idle";
}

function safeEndpoint(value) {
  try {
    const url = new URL(String(value || ""));
    const path = url.pathname === "/" ? "" : url.pathname.replace(/\/+$/, "");
    return `${url.protocol}//${url.host}${path}`;
  } catch (_error) {
    return value ? "Invalid server URL" : "Not configured";
  }
}

function diagnosticFromError(error, action) {
  return {
    code: String(error?.code || "EXTENSION_API_ERROR"),
    action,
    reason: String(error?.message || "Extension API request failed")
  };
}

function showDiagnostics(source, diagnostic) {
  const safe = sanitizeDiagnostic({ source, ...objectOrEmpty(diagnostic) });
  diagnosticDetailsEl.textContent = JSON.stringify(safe, null, 2);
  diagnosticDisclosure.hidden = false;
  copyFeedbackEl.textContent = "";
}

function hideDiagnostics() {
  diagnosticDisclosure.hidden = true;
  diagnosticDisclosure.open = false;
  diagnosticDetailsEl.textContent = "";
  copyFeedbackEl.textContent = "";
}

async function copyDiagnostics() {
  const text = diagnosticDetailsEl.textContent;
  if (!text) {
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    copyFeedbackEl.textContent = "Diagnostics copied.";
  } catch (_error) {
    copyFeedbackEl.textContent = "Copy failed. Select the diagnostic text and copy it manually.";
  }
}

function sanitizeDiagnostic(value) {
  const token = String(form.pairingToken.value || "");
  const blockedKey = /(token|authorization|cookie|secret|password)/i;
  const visit = (item, key = "") => {
    if (blockedKey.test(key)) {
      return "[redacted]";
    }
    if (Array.isArray(item)) {
      return item.map((entry) => visit(entry));
    }
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.entries(item).map(([name, entry]) => [name, visit(entry, name)]));
    }
    if (typeof item === "string") {
      let safe = item.replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [redacted]");
      safe = safe.replace(
        /(\b(?:pairing[_-]?token|token|secret|password)=)[^&#\s;,}]*/gi,
        "$1[redacted]"
      );
      safe = safe.replace(
        /("(?:pairing[_-]?token|token|secret|password)"\s*:\s*")[^"]*"/gi,
        "$1[redacted]\""
      );
      safe = safe.replace(
        /(authorization\s*[:=]\s*)[^\s,}]+/gi,
        "$1[redacted]"
      );
      if (token) {
        safe = safe.split(token).join("[redacted]");
      }
      return safe.slice(0, 1_000);
    }
    return item;
  };
  return visit(objectOrEmpty(value));
}

function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function createOptionsError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

updateControls();
