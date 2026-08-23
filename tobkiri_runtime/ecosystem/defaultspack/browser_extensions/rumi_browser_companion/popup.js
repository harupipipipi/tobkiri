const connectionCard = document.getElementById("connection-card");
const statusSymbol = document.getElementById("status-symbol");
const statusTitle = document.getElementById("status-title");
const statusMessage = document.getElementById("status-message");
const endpoint = document.getElementById("endpoint");
const profile = document.getElementById("profile");
const lastSuccess = document.getElementById("last-success");
const scopeTitle = document.getElementById("scope-title");
const scopeOrigin = document.getElementById("scope-origin");
const scopeDetail = document.getElementById("scope-detail");
const popupError = document.getElementById("popup-error");
const pollNowButton = document.getElementById("poll-now");
const openSettingsButton = document.getElementById("open-settings");
const openHelpButton = document.getElementById("open-help");

const STATUS_PRESENTATION = Object.freeze({
  connected: { symbol: "✓", title: "Connected" },
  connecting: { symbol: "…", title: "Connecting" },
  not_configured: { symbol: "?", title: "Setup required" },
  bridge_error: { symbol: "!", title: "Connection error" }
});

document.addEventListener("DOMContentLoaded", () => {
  void refreshPopup();
});

pollNowButton.addEventListener("click", () => {
  void pollNow();
});

openSettingsButton.addEventListener("click", () => {
  void chrome.runtime.openOptionsPage();
});

openHelpButton.addEventListener("click", () => {
  void chrome.tabs.create({ url: chrome.runtime.getURL("help.html") });
});

async function refreshPopup() {
  clearPopupError();
  try {
    const popupState = await chrome.runtime.sendMessage({ type: "rumi:get-popup-state" });
    if (!popupState?.ok) {
      throw new Error(popupState?.error || "Status is unavailable.");
    }
    renderPopupState(popupState);
  } catch (error) {
    renderUnavailable(error);
  }
}

async function pollNow() {
  pollNowButton.disabled = true;
  renderStatus({
    ok: false,
    state: "connecting",
    message: "Contacting the local bridge."
  });
  clearPopupError();
  try {
    await chrome.runtime.sendMessage({ type: "rumi:poll-now" });
    await refreshPopup();
  } catch (error) {
    renderUnavailable(error);
  } finally {
    pollNowButton.disabled = false;
  }
}

function renderPopupState(popupState) {
  renderStatus(popupState.status || {});
  endpoint.textContent = popupState.endpoint || "Not configured";
  profile.textContent = popupState.profileLabel || "Default browser profile";
  renderScope(popupState.currentTab || {});
}

function renderStatus(status) {
  const state = STATUS_PRESENTATION[status.state] ? status.state : "bridge_error";
  const presentation = STATUS_PRESENTATION[state];
  connectionCard.dataset.state = state;
  statusSymbol.textContent = presentation.symbol;
  statusTitle.textContent = presentation.title;
  statusMessage.textContent = status.message || defaultStatusMessage(state);
  lastSuccess.textContent = formatTimestamp(status.lastSuccessfulPollAt);
}

function renderScope(currentTab) {
  scopeTitle.textContent = currentTab.label || "No active tab";
  scopeOrigin.textContent = currentTab.origin || "";
  scopeOrigin.hidden = !currentTab.origin;
  scopeDetail.textContent =
    currentTab.detail || "Open a web page to inspect its Browser Companion scope.";
}

function renderUnavailable(error) {
  console.warn("Browser Companion popup status unavailable", error);
  renderStatus({
    ok: false,
    state: "bridge_error",
    message: "The extension background service is unavailable."
  });
  popupError.hidden = false;
  popupError.textContent = "Unable to read local status. Open Settings and try again.";
}

function clearPopupError() {
  popupError.hidden = true;
  popupError.textContent = "";
}

function defaultStatusMessage(state) {
  switch (state) {
    case "connected":
      return "The local bridge responded to the latest poll.";
    case "connecting":
      return "Waiting for the local bridge to respond.";
    case "not_configured":
      return "Add a server URL and pairing token in Settings.";
    default:
      return "The local bridge could not be reached. Check Settings and try again.";
  }
}

function formatTimestamp(value) {
  if (!value) {
    return "Never";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "Unknown";
  }
  return parsed.toLocaleString();
}
