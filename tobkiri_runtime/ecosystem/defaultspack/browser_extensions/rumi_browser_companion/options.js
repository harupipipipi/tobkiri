const STORAGE_KEY = "rumiBrowserCompanionSettings";
const DEFAULT_SETTINGS = {
  serverUrl: "http://127.0.0.1:8766",
  pairingToken: "",
  clientLabel: "",
  profileLabel: "",
  pollIntervalMinutes: 1
};

const form = document.getElementById("settings-form");
const statusEl = document.getElementById("status");
const pollNowButton = document.getElementById("poll-now");

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
  const settings = {
    ...DEFAULT_SETTINGS,
    ...(stored[STORAGE_KEY] || {})
  };
  form.serverUrl.value = settings.serverUrl;
  form.pairingToken.value = settings.pairingToken;
  form.clientLabel.value = settings.clientLabel;
  form.profileLabel.value = settings.profileLabel;
  form.pollIntervalMinutes.value = settings.pollIntervalMinutes;

  const backgroundStatus = await chrome.runtime.sendMessage({ type: "rumi:get-status" });
  renderStatus(backgroundStatus);
}

async function saveSettings() {
  const settings = {
    serverUrl: String(form.serverUrl.value || "").trim(),
    pairingToken: String(form.pairingToken.value || "").trim(),
    clientLabel: String(form.clientLabel.value || "").trim(),
    profileLabel: String(form.profileLabel.value || "").trim(),
    pollIntervalMinutes: Math.max(1, Number(form.pollIntervalMinutes.value) || 1)
  };
  await chrome.storage.local.set({ [STORAGE_KEY]: settings });
  setStatus("Settings saved.", true);
}

async function pollNow() {
  setStatus("Polling bridge...", true);
  const result = await chrome.runtime.sendMessage({ type: "rumi:poll-now" });
  renderStatus(result);
}

function renderStatus(status) {
  if (!status) {
    setStatus("No background status available yet.", false);
    return;
  }
  const message = status.ok
    ? `Status: ${status.state || "ok"}${status.commandCount != null ? `, commands: ${status.commandCount}` : ""}`
    : publicErrorStatus(status);
  setStatus(message, Boolean(status.ok));
}

function publicErrorStatus(status) {
  if (status.state === "not_configured") {
    return "Status: setup required. Add a server URL and pairing token.";
  }
  if (status.state === "connecting") {
    return "Status: connecting to the local bridge.";
  }
  return "Status: connection error. Check the endpoint and poll again.";
}

function setStatus(message, isOk) {
  statusEl.textContent = message;
  statusEl.classList.toggle("ok", Boolean(isOk));
  statusEl.classList.toggle("error", !isOk);
}
