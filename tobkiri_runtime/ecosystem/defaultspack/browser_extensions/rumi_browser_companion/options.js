const STORAGE_KEY = "rumiBrowserCompanionSettings";
const DEFAULT_SETTINGS = {
  serverUrl: "http://127.0.0.1:8766",
  pairingToken: "",
  clientLabel: "",
  profileLabel: "",
  pollIntervalMinutes: 1,
  enabled: false,
  consentAcknowledged: false,
  allowedOrigins: [],
  deniedOrigins: []
};

const form = document.getElementById("settings-form");
const statusEl = document.getElementById("status");
const pollNowButton = document.getElementById("poll-now");
const controlStateEl = document.getElementById("control-state");
const authorizedInstanceEl = document.getElementById("authorized-instance");
const activeScopeEl = document.getElementById("active-scope");
const lastPollEl = document.getElementById("last-poll");
const recentCommandEl = document.getElementById("recent-command");
const recentActivityEl = document.getElementById("recent-activity");

document.addEventListener("DOMContentLoaded", () => {
  void loadSettings();
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void saveSettings();
});

pollNowButton.addEventListener("click", () => {
  void pollNow();
});

async function loadSettings() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  const settings = normalizeSettings(stored[STORAGE_KEY]);
  form.serverUrl.value = settings.serverUrl;
  form.pairingToken.value = settings.pairingToken;
  form.clientLabel.value = settings.clientLabel;
  form.profileLabel.value = settings.profileLabel;
  form.pollIntervalMinutes.value = settings.pollIntervalMinutes;
  form.enabled.checked = settings.enabled;
  form.consentAcknowledged.checked = settings.consentAcknowledged;
  form.allowedOrigins.value = settings.allowedOrigins.join("\n");
  form.deniedOrigins.value = settings.deniedOrigins.join("\n");

  const backgroundStatus = await chrome.runtime.sendMessage({
    type: "rumi:get-status"
  });
  renderStatus(backgroundStatus);
}

async function saveSettings() {
  const previousStored = await chrome.storage.local.get(STORAGE_KEY);
  const previous = normalizeSettings(previousStored[STORAGE_KEY]);
  const settings = readFormSettings();
  const pollDecision = TobkiriBrowserAccessPolicy.canPoll(settings);
  if (settings.enabled && !pollDecision.allowed) {
    setStatus(pollDecision.message, false);
    return;
  }

  const denied = new Set(settings.deniedOrigins);
  const requestedOrigins = settings.allowedOrigins.filter(
    (origin) => !denied.has(origin)
  );
  const requestedPatterns = TobkiriBrowserAccessPolicy.permissionPatterns(
    requestedOrigins
  );
  if (requestedPatterns.length > 0) {
    const granted = await chrome.permissions.request({
      origins: requestedPatterns
    });
    if (!granted) {
      setStatus(
        "Permission was not granted. Control remains unchanged. Approve the browser prompt or remove the site from Allowed, then save again.",
        false
      );
      return;
    }
  }

  await chrome.storage.local.set({ [STORAGE_KEY]: settings });
  const removedOrigins = previous.allowedOrigins.filter(
    (origin) => !settings.allowedOrigins.includes(origin)
  );
  const removedPatterns = TobkiriBrowserAccessPolicy.permissionPatterns(
    removedOrigins
  );
  if (removedPatterns.length > 0) {
    await chrome.permissions.remove({ origins: removedPatterns });
  }

  setStatus(
    settings.enabled
      ? "Access settings saved. Polling and control are enabled for the listed sites."
      : "Access settings saved. Polling and control are paused; pairing data is retained.",
    true
  );
  const backgroundStatus = await chrome.runtime.sendMessage({
    type: "rumi:get-status"
  });
  renderStatus(backgroundStatus, { preserveMessage: true });
}

async function pollNow() {
  setStatus("Requesting a bridge poll…", true);
  await chrome.runtime.sendMessage({ type: "rumi:poll-now" });
  const status = await chrome.runtime.sendMessage({ type: "rumi:get-status" });
  renderStatus(status);
}

function readFormSettings() {
  return normalizeSettings({
    serverUrl: String(form.serverUrl.value || "").trim(),
    pairingToken: String(form.pairingToken.value || "").trim(),
    clientLabel: String(form.clientLabel.value || "").trim(),
    profileLabel: String(form.profileLabel.value || "").trim(),
    pollIntervalMinutes: Math.max(
      1,
      Number(form.pollIntervalMinutes.value) || 1
    ),
    enabled: form.enabled.checked,
    consentAcknowledged: form.consentAcknowledged.checked,
    allowedOrigins: form.allowedOrigins.value,
    deniedOrigins: form.deniedOrigins.value
  });
}

function normalizeSettings(value) {
  const settings = {
    ...DEFAULT_SETTINGS,
    ...(value || {})
  };
  settings.enabled = settings.enabled === true;
  settings.consentAcknowledged = settings.consentAcknowledged === true;
  settings.allowedOrigins = TobkiriBrowserAccessPolicy.normalizeOrigins(
    settings.allowedOrigins
  );
  settings.deniedOrigins = TobkiriBrowserAccessPolicy.normalizeOrigins(
    settings.deniedOrigins
  );
  return settings;
}

function renderStatus(status, options = {}) {
  if (!status) {
    setStatus("No background status is available yet.", false);
    return;
  }
  const state = String(status.state || "idle");
  const stateLabel = {
    connected: "Enabled and connected",
    paused: "Paused — no polling or control",
    not_configured: "Setup required",
    bridge_error: "Connection needs attention",
    idle: "Waiting for first poll"
  }[state] || state;
  controlStateEl.textContent = stateLabel;
  authorizedInstanceEl.textContent = String(
    status.authorizedInstance || status.serverUrl || "Not configured"
  );

  const allowed = Array.isArray(status.allowedOrigins)
    ? status.allowedOrigins
    : [];
  const denied = Array.isArray(status.deniedOrigins)
    ? status.deniedOrigins
    : [];
  activeScopeEl.textContent = allowed.length > 0
    ? `${allowed.length} allowed: ${allowed.join(", ")}${
        denied.length ? `; ${denied.length} denied` : ""
      }`
    : "No sites allowed (safe default)";
  lastPollEl.textContent = formatTimestamp(status.lastPollAt);

  const activity = Array.isArray(status.recentActivity)
    ? status.recentActivity
    : [];
  const latestCommand = activity.find((item) => item.kind === "command");
  recentCommandEl.textContent = latestCommand
    ? `${latestCommand.action || "command"} — ${
        latestCommand.ok ? "completed" : "blocked or failed"
      } — ${formatTimestamp(latestCommand.at)}`
    : "None";
  renderActivity(activity);

  if (!options.preserveMessage) {
    const message = `${stateLabel}.${status.message ? ` ${status.message}` : ""}`;
    setStatus(message, Boolean(status.ok));
  }
}

function renderActivity(activity) {
  recentActivityEl.replaceChildren();
  if (activity.length === 0) {
    const item = document.createElement("li");
    item.textContent = "No activity recorded.";
    recentActivityEl.append(item);
    return;
  }
  for (const entry of activity) {
    const item = document.createElement("li");
    const outcome = entry.ok ? "completed" : "blocked or failed";
    const subject = entry.action || entry.kind || "event";
    const origin = entry.origin ? ` on ${entry.origin}` : "";
    const detail = entry.message ? ` — ${entry.message}` : "";
    item.textContent = `${formatTimestamp(entry.at)} — ${subject}${origin} — ${outcome}${detail}`;
    recentActivityEl.append(item);
  }
}

function formatTimestamp(value) {
  if (!value) {
    return "Never";
  }
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime())
    ? String(value)
    : timestamp.toLocaleString();
}

function setStatus(message, isOk) {
  statusEl.textContent = message;
  statusEl.classList.toggle("ok", Boolean(isOk));
  statusEl.classList.toggle("error", !isOk);
}
