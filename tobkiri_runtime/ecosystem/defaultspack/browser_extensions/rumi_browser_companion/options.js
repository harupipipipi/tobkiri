import {
  buildSafeDiagnostics,
  nextScheduledAttempt,
  safeServerOrigin,
  sanitizeConnectionStatus
} from "./status_contract.mjs";

const STORAGE_KEY = "rumiBrowserCompanionSettings";
const LAST_STATUS_KEY = "rumiBrowserCompanionLastStatus";
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
const statusPanel = document.getElementById("status-panel");
const statusEl = document.getElementById("status");
const statusBadge = document.getElementById("status-badge");
const retryButton = document.getElementById("retry");
const rePairButton = document.getElementById("re-pair");
const permissionsButton = document.getElementById("open-permissions");
const diagnosticsButton = document.getElementById("copy-diagnostics");

let currentStatus = sanitizeConnectionStatus({ state: "loading" });
let operation = "loading";
let isDirty = false;
let lastAnnouncement = "";

document.addEventListener("DOMContentLoaded", () => {
  localizeDocument();
  loadSettings().catch(() => showOperationError("SETTINGS_LOAD_FAILED"));
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (operation) return;
  saveSettings().catch(() => showOperationError("SETTINGS_SAVE_FAILED"));
});

form.addEventListener("input", () => {
  if (operation === "loading") return;
  isDirty = true;
  currentStatus = sanitizeConnectionStatus({
    ...currentStatus,
    state: "dirty",
    code: "UNSAVED_CHANGES",
    action: "none"
  });
  renderStatus(currentStatus);
});

pollNowButton.addEventListener("click", () => {
  if (operation || isDirty) {
    renderStatus(sanitizeConnectionStatus({
      ...currentStatus,
      state: "dirty",
      code: "UNSAVED_CHANGES",
      action: "none"
    }));
    return;
  }
  pollNow().catch(() => showOperationError("BRIDGE_UNAVAILABLE"));
});

retryButton.addEventListener("click", () => {
  if (!operation && !isDirty) pollNow().catch(() => showOperationError("BRIDGE_UNAVAILABLE"));
});

rePairButton.addEventListener("click", () => {
  const tokenInput = field("pairingToken");
  tokenInput.focus();
  tokenInput.select();
});

permissionsButton.addEventListener("click", () => {
  chrome.tabs.create({ url: `chrome://extensions/?id=${chrome.runtime.id}` }).catch(() => {
    showOperationError("PERMISSIONS_PAGE_UNAVAILABLE");
  });
});

diagnosticsButton.addEventListener("click", () => {
  navigator.clipboard.writeText(buildSafeDiagnostics(currentStatus)).then(
    () => announce(message("diagnosticsCopied")),
    () => showOperationError("DIAGNOSTICS_COPY_FAILED")
  );
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local" || !changes[LAST_STATUS_KEY] || operation || isDirty) return;
  currentStatus = sanitizeConnectionStatus(changes[LAST_STATUS_KEY].newValue);
  renderStatus(currentStatus);
});

async function loadSettings() {
  setOperation("loading");
  try {
    const stored = await chrome.storage.local.get(STORAGE_KEY);
    const settings = {
      ...DEFAULT_SETTINGS,
      ...(stored[STORAGE_KEY] || {})
    };
    populateSettingsForm(settings);
    currentStatus = sanitizeConnectionStatus(
      await chrome.runtime.sendMessage({ type: "rumi:get-status" })
    );
    isDirty = false;
    renderStatus(currentStatus);
  } finally {
    setOperation(null);
  }
}

async function saveSettings() {
  setOperation("saving");
  currentStatus = sanitizeConnectionStatus({
    ...currentStatus,
    state: "saving",
    code: "SETTINGS_SAVING",
    action: "none"
  });
  renderStatus(currentStatus);
  try {
    const settings = readSettings();
    await chrome.storage.local.set({ [STORAGE_KEY]: settings });
    isDirty = false;
    currentStatus = sanitizeConnectionStatus({
      ...currentStatus,
      state: "saved_unverified",
      code: "SETTINGS_SAVED_UNVERIFIED",
      action: "none",
      serverOrigin: safeServerOrigin(settings.serverUrl),
      clientLabel: settings.clientLabel,
      profileLabel: settings.profileLabel,
      pollIntervalMinutes: settings.pollIntervalMinutes,
      updatedAt: new Date().toISOString()
    });
    renderStatus(currentStatus);
  } finally {
    setOperation(null);
  }
}

async function pollNow() {
  setOperation("connecting");
  currentStatus = sanitizeConnectionStatus({
    ...currentStatus,
    state: "connecting",
    code: "CONNECTION_CHECKING",
    action: "none",
    lastAttemptAt: new Date().toISOString()
  });
  renderStatus(currentStatus);
  try {
    currentStatus = sanitizeConnectionStatus(
      await chrome.runtime.sendMessage({ type: "rumi:poll-now" })
    );
    renderStatus(currentStatus);
  } finally {
    setOperation(null);
  }
}

function readSettings() {
  return {
    serverUrl: String(field("serverUrl").value || "").trim(),
    pairingToken: String(field("pairingToken").value || "").trim(),
    clientLabel: String(field("clientLabel").value || "").trim(),
    profileLabel: String(field("profileLabel").value || "").trim(),
    pollIntervalMinutes: Math.max(1, Number(field("pollIntervalMinutes").value) || 1)
  };
}

function populateSettingsForm(settings) {
  form.serverUrl.value = settings.serverUrl;
  form.pairingToken.value = settings.pairingToken;
  form.clientLabel.value = settings.clientLabel;
  form.profileLabel.value = settings.profileLabel;
  form.pollIntervalMinutes.value = settings.pollIntervalMinutes;
}

function field(name) {
  return form.elements.namedItem(name);
}

function setOperation(nextOperation) {
  operation = nextOperation;
  form.setAttribute("aria-busy", String(Boolean(operation)));
  for (const element of form.elements) {
    element.disabled = Boolean(operation);
  }
  updateActions();
}

function showOperationError(code) {
  operation = null;
  currentStatus = sanitizeConnectionStatus({
    ...currentStatus,
    state: "error",
    code,
    action: "retry",
    updatedAt: new Date().toISOString()
  });
  setOperation(null);
  renderStatus(currentStatus);
}

function renderStatus(value) {
  currentStatus = sanitizeConnectionStatus(value);
  const suffix = stateSuffix(currentStatus.state);
  statusPanel.dataset.state = currentStatus.state;
  setText(statusBadge, message(`state${suffix}`));
  announce(message(`detail${suffix}`));
  setText(document.getElementById("status-instance"), currentStatus.serverOrigin || "—");
  setText(
    document.getElementById("status-identity"),
    currentStatus.clientLabel || currentStatus.profileLabel || "—"
  );
  setText(document.getElementById("status-last-attempt"), formatDate(currentStatus.lastAttemptAt));
  setText(document.getElementById("status-last-success"), formatDate(currentStatus.lastSuccessAt));
  setText(document.getElementById("status-polling"), pollingDescription(currentStatus));
  updateActions();
}

function updateActions() {
  const busy = Boolean(operation);
  saveButton.disabled = busy;
  pollNowButton.disabled = busy || isDirty;
  retryButton.hidden = currentStatus.action !== "retry";
  rePairButton.hidden = currentStatus.action !== "re_pair";
  permissionsButton.hidden = currentStatus.action !== "open_permissions";
  diagnosticsButton.disabled = busy;
}

function pollingDescription(status) {
  if (!status.pollIntervalMinutes) return "—";
  const next = nextScheduledAttempt(status);
  return next
    ? message("pollingEveryNext", [String(status.pollIntervalMinutes), formatDate(next)])
    : message("pollingEvery", [String(status.pollIntervalMinutes)]);
}

function formatDate(value) {
  const timestamp = Date.parse(value || "");
  return Number.isFinite(timestamp)
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(timestamp)
    : "—";
}

function announce(text) {
  if (text === lastAnnouncement) return;
  lastAnnouncement = text;
  setText(statusEl, text);
}

function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

function stateSuffix(state) {
  return state.split("_").map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join("");
}

function localizeDocument() {
  document.title = message("optionsTitle");
  for (const element of document.querySelectorAll("[data-i18n]")) {
    setText(element, message(element.dataset.i18n));
  }
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) {
    element.placeholder = message(element.dataset.i18nPlaceholder);
  }
}

function message(key, substitutions) {
  return chrome.i18n.getMessage(key, substitutions) || key;
}
