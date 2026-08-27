import "./search_home_destination_policy.js";
import "./browser_access_policy.js";

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

const STORAGE_KEY = "rumiBrowserCompanionSettings";
const CLIENT_ID_KEY = "rumiBrowserCompanionClientId";
const INSTALLATION_ID_KEY = "rumiBrowserCompanionInstallationId";
const BROWSER_PROFILE_ID_KEY = "rumiBrowserCompanionProfileId";
const LAST_STATUS_KEY = "rumiBrowserCompanionLastStatus";
const RECENT_ACTIVITY_KEY = "rumiBrowserCompanionRecentActivity";
const ALARM_NAME = "rumi-browser-companion-poll";
const BRIDGE_POLL_PATH = "/api/tools/browser-companion/bridge/poll";
const BRIDGE_RESULT_PATH = "/api/tools/browser-companion/bridge/result";
const SEARCH_HOME_ROUTE_STATE_KEY = "rumiSearchHomeRouteStateByTab";
const SEARCH_HOME_ROUTE_MAX_AGE_MS = 1000 * 60 * 60 * 6;
const SEARCH_HOME_MAX_CLOCK_SKEW_MS = 30_000;
const SEARCH_HOME_TRUSTED_ORIGINS = Object.freeze(
  Array.isArray(chrome.runtime.getManifest().x_rumi_search_home_origins)
    ? chrome.runtime.getManifest().x_rumi_search_home_origins
    : []
);

chrome.runtime.onInstalled.addListener(async () => {
  const settings = await ensureSettings();
  await ensureClientIdentity();
  await configurePolling(settings);
  await updateActionBadge({ state: settings.enabled ? "waiting" : "paused" });
  await chrome.runtime.openOptionsPage();
});

chrome.runtime.onStartup.addListener(async () => {
  const settings = await ensureSettings();
  await ensureClientIdentity();
  await configurePolling(settings);
  if (TobkiriBrowserAccessPolicy.canPoll(settings).allowed) {
    void pollBridge("onStartup");
  } else {
    await updateActionBadge({ state: "paused" });
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    void pollBridge("alarm");
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void clearSearchHomeRouteState(tabId);
});

chrome.action.onClicked.addListener(() => {
  void chrome.runtime.openOptionsPage();
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local" || !changes[STORAGE_KEY]) {
    return;
  }
  void ensureSettings().then((settings) => {
    void configurePolling(settings);
    if (TobkiriBrowserAccessPolicy.canPoll(settings).allowed) {
      void pollBridge("settingsChanged");
    } else {
      void setStatus({
        ok: true,
        state: "paused",
        trigger: "settingsChanged",
        message: "Browser control and polling are paused."
      });
    }
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) {
    return false;
  }

  (async () => {
    switch (message.type) {
      case "rumi:get-status":
        sendResponse(await getStatus());
        return;
      case "rumi:poll-now":
        sendResponse(await pollBridge("manual"));
        return;
      case "rumi:list-tabs":
        sendResponse({ ok: true, tabs: await getTabsSummary(await getSettings()) });
        return;
      case "rumi:search-home:set-route-state":
        sendResponse(await setSearchHomeRouteState(sender?.tab?.id, message.payload, {
          senderUrl: sender?.url || sender?.tab?.url || "",
          sourceOrigin: message.source_origin
        }));
        return;
      case "rumi:search-home:get-route-state":
        sendResponse(await getSearchHomeRouteState(sender?.tab?.id));
        return;
      case "rumi:search-home:advance-candidate":
        sendResponse(await advanceSearchHomeRouteState(sender?.tab?.id, message.action));
        return;
      default:
        sendResponse({ ok: false, error: `Unknown message type: ${message.type}` });
    }
  })().catch((error) => {
    sendResponse({ ok: false, error: String(error && error.message ? error.message : error) });
  });

  return true;
});

async function ensureSettings() {
  const stored = await readLocalSettingsWithSyncMigration();
  const merged = {
    ...DEFAULT_SETTINGS,
    ...(stored || {})
  };
  merged.pollIntervalMinutes = normalizePollInterval(merged.pollIntervalMinutes);
  merged.enabled = merged.enabled === true;
  merged.consentAcknowledged = merged.consentAcknowledged === true;
  merged.allowedOrigins = TobkiriBrowserAccessPolicy.normalizeOrigins(
    merged.allowedOrigins
  );
  merged.deniedOrigins = TobkiriBrowserAccessPolicy.normalizeOrigins(
    merged.deniedOrigins
  );
  await chrome.storage.local.set({ [STORAGE_KEY]: merged });
  return merged;
}

async function getSettings() {
  const stored = await readLocalSettingsWithSyncMigration();
  const merged = {
    ...DEFAULT_SETTINGS,
    ...(stored || {}),
    pollIntervalMinutes: normalizePollInterval(
      stored?.pollIntervalMinutes ?? DEFAULT_SETTINGS.pollIntervalMinutes
    )
  };
  merged.enabled = merged.enabled === true;
  merged.consentAcknowledged = merged.consentAcknowledged === true;
  merged.allowedOrigins = TobkiriBrowserAccessPolicy.normalizeOrigins(
    merged.allowedOrigins
  );
  merged.deniedOrigins = TobkiriBrowserAccessPolicy.normalizeOrigins(
    merged.deniedOrigins
  );
  return merged;
}

async function readLocalSettingsWithSyncMigration() {
  const localStored = await chrome.storage.local.get(STORAGE_KEY);
  if (localStored[STORAGE_KEY]) {
    return localStored[STORAGE_KEY];
  }
  const syncStored = await chrome.storage.sync.get(STORAGE_KEY);
  if (syncStored[STORAGE_KEY]) {
    await chrome.storage.local.set({ [STORAGE_KEY]: syncStored[STORAGE_KEY] });
    await chrome.storage.sync.remove(STORAGE_KEY);
    return syncStored[STORAGE_KEY];
  }
  return null;
}

async function ensureClientIdentity() {
  const stored = await chrome.storage.local.get([
    CLIENT_ID_KEY,
    INSTALLATION_ID_KEY,
    BROWSER_PROFILE_ID_KEY
  ]);
  let clientId = stringOrEmpty(stored[CLIENT_ID_KEY]);
  let installationId = stringOrEmpty(stored[INSTALLATION_ID_KEY]);
  let browserProfileId = stringOrEmpty(stored[BROWSER_PROFILE_ID_KEY]);

  if (!clientId && browserProfileId) {
    clientId = browserProfileId;
  }
  if (!installationId && clientId) {
    installationId = clientId;
  }
  if (!browserProfileId && clientId) {
    browserProfileId = clientId;
  }
  if (!installationId) {
    installationId = generateStableId("install");
  }
  if (!browserProfileId) {
    browserProfileId = generateStableId("profile");
  }
  if (!clientId) {
    clientId = browserProfileId;
  }

  const updates = {};
  if (stored[CLIENT_ID_KEY] !== clientId) {
    updates[CLIENT_ID_KEY] = clientId;
  }
  if (stored[INSTALLATION_ID_KEY] !== installationId) {
    updates[INSTALLATION_ID_KEY] = installationId;
  }
  if (stored[BROWSER_PROFILE_ID_KEY] !== browserProfileId) {
    updates[BROWSER_PROFILE_ID_KEY] = browserProfileId;
  }
  if (Object.keys(updates).length > 0) {
    await chrome.storage.local.set(updates);
  }
  return {
    client_id: clientId,
    installation_id: installationId,
    browser_profile_id: browserProfileId
  };
}

async function ensureClientId() {
  const identity = await ensureClientIdentity();
  return identity.client_id;
}

function generateStableId(prefix) {
  const raw = self.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${raw}`;
}

function stringOrEmpty(value) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

async function getStatus() {
  const stored = await chrome.storage.local.get([
    LAST_STATUS_KEY,
    RECENT_ACTIVITY_KEY
  ]);
  const settings = await getSettings();
  const lastStatus = stored[LAST_STATUS_KEY] || { ok: true, state: "idle" };
  return {
    ...lastStatus,
    state: settings.enabled ? lastStatus.state : "paused",
    ok: settings.enabled ? lastStatus.ok : true,
    authorizedInstance: settings.serverUrl || "Not configured",
    enabled: settings.enabled,
    consentAcknowledged: settings.consentAcknowledged,
    allowedOrigins: settings.allowedOrigins,
    deniedOrigins: settings.deniedOrigins,
    recentActivity: Array.isArray(stored[RECENT_ACTIVITY_KEY])
      ? stored[RECENT_ACTIVITY_KEY]
      : []
  };
}

async function setStatus(status) {
  const withTimestamp = {
    ...status,
    updatedAt: new Date().toISOString()
  };
  await chrome.storage.local.set({ [LAST_STATUS_KEY]: withTimestamp });
  await updateActionBadge(withTimestamp);
  return withTimestamp;
}

async function configurePolling(settings) {
  if (!TobkiriBrowserAccessPolicy.canPoll(settings).allowed) {
    await chrome.alarms.clear(ALARM_NAME);
    return;
  }
  chrome.alarms.create(ALARM_NAME, {
    periodInMinutes: normalizePollInterval(settings.pollIntervalMinutes)
  });
}

async function updateActionBadge(status) {
  const state = String(status?.state || "idle");
  const badgeText = state === "connected"
    ? "ON"
    : state === "paused"
      ? "OFF"
      : state === "not_configured" || state === "waiting"
        ? "SET"
        : state === "idle"
          ? ""
          : "!";
  const title = state === "connected"
    ? "Tobkiri Browser Companion — control active; open access controls"
    : state === "paused"
      ? "Tobkiri Browser Companion — polling paused; open access controls"
      : "Tobkiri Browser Companion — attention needed; open access controls";
  await chrome.action.setBadgeText({ text: badgeText });
  await chrome.action.setTitle({ title });
}

async function recordActivity(activity) {
  const stored = await chrome.storage.local.get(RECENT_ACTIVITY_KEY);
  const current = Array.isArray(stored[RECENT_ACTIVITY_KEY])
    ? stored[RECENT_ACTIVITY_KEY]
    : [];
  const entry = {
    kind: String(activity.kind || "event"),
    action: String(activity.action || ""),
    ok: activity.ok === true,
    origin: String(activity.origin || ""),
    message: String(activity.message || ""),
    at: new Date().toISOString()
  };
  await chrome.storage.local.set({
    [RECENT_ACTIVITY_KEY]: [entry, ...current].slice(0, 10)
  });
  return entry;
}

function normalizePollInterval(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 1) {
    return 1;
  }
  return Math.max(1, Math.round(numeric));
}

async function pollBridge(trigger) {
  const settings = await getSettings();
  const pollDecision = TobkiriBrowserAccessPolicy.canPoll(settings);
  if (!pollDecision.allowed) {
    return setStatus({
      ok: pollDecision.reason === "paused",
      state: pollDecision.reason === "paused" ? "paused" : "not_configured",
      trigger,
      message: pollDecision.message
    });
  }
  if (!settings.serverUrl) {
    return setStatus({
      ok: false,
      state: "not_configured",
      trigger,
      message: "Set a local server URL in Options."
    });
  }
  const identity = await ensureClientIdentity();

  const metadata = await buildClientMetadata(settings, identity);
  const requestBody = {
    event: "poll",
    trigger,
    pairing_token: settings.pairingToken,
    client: metadata
  };

  try {
    const response = await fetch(joinUrl(settings.serverUrl, BRIDGE_POLL_PATH), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${settings.pairingToken}`
      },
      body: JSON.stringify(requestBody)
    });
    const envelope = await safeJson(response);
    const payload = unwrapBridgePayload(envelope);
    if (!response.ok || envelope.status === "error") {
      throw new Error(`Bridge poll failed (${response.status}): ${JSON.stringify(envelope)}`);
    }

    const commands = normalizeCommands(payload);
    const results = [];
    for (const command of commands) {
      results.push(await executeBridgeCommand(command, settings));
    }

    if (results.length > 0) {
      await postCommandResults(settings, metadata, results);
    }

    await recordActivity({
      kind: "poll",
      action: trigger,
      ok: true,
      message: `${commands.length} command(s) received.`
    });
    return setStatus({
      ok: true,
      state: "connected",
      trigger,
      commandCount: commands.length,
      serverUrl: settings.serverUrl,
      lastPollAt: new Date().toISOString()
    });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    await recordActivity({
      kind: "poll",
      action: trigger,
      ok: false,
      message
    });
    return setStatus({
      ok: false,
      state: "bridge_error",
      trigger,
      serverUrl: settings.serverUrl,
      message,
      lastPollAt: new Date().toISOString()
    });
  }
}

async function buildClientMetadata(settings, identity) {
  const browser = detectBrowser();
  const tabs = await getTabsSummary(settings);
  const activeTab = tabs.find((tab) => tab.active) || null;
  const profile = buildClientProfileMetadata(settings, identity, browser);
  return {
    client_id: identity.client_id,
    ...profile,
    label: settings.clientLabel || profile.profile_label || `${browser.name} Companion`,
    client_label: settings.clientLabel || "",
    client_profile: profile,
    extension_version: chrome.runtime.getManifest().version,
    browser_name: browser.name,
    browser_version: browser.version,
    user_agent: navigator.userAgent,
    platform: navigator.platform || "",
    tabs,
    active_tab_id: activeTab ? activeTab.id : null,
    capabilities: {
      multi_browser: true,
      user_session_cookies: true,
      list_tabs: true,
      select_tab: true,
      navigate: true,
      capture_visible_tab: true,
      dom_snapshot: true,
      semantic_dom: true,
      accessible_labels: true,
      semantic_targeting: ["element_id", "selector", "text", "text_query", "accessible_name", "role", "semantic_id", "nearby_text"],
      element_actions: ["click", "type", "press", "scroll", "extract", "highlight", "clear_highlight"]
    },
    generated_at: new Date().toISOString()
  };
}

function buildClientProfileMetadata(settings, identity, browser) {
  const profileLabel =
    stringOrEmpty(settings.profileLabel) ||
    stringOrEmpty(settings.clientLabel) ||
    `${browser.name} Profile`;
  return {
    browser_profile_id: identity.browser_profile_id,
    profile_label: profileLabel,
    installation_id: identity.installation_id,
    extension_id: chrome.runtime.id || "",
    browser_name: browser.name,
    browser_version: browser.version
  };
}

function detectBrowser() {
  const ua = navigator.userAgent;
  const brands = navigator.userAgentData?.brands || [];
  const fromBrands =
    brands.find((brand) => /Chrom(e|ium)|Microsoft Edge|Opera|Brave/i.test(brand.brand)) || null;

  if (fromBrands) {
    return {
      name: fromBrands.brand,
      version: navigator.userAgentData?.getHighEntropyValues
        ? "Chromium"
        : fromBrands.version || "unknown"
    };
  }

  const candidates = [
    { name: "Microsoft Edge", regex: /Edg\/([\d.]+)/ },
    { name: "Opera", regex: /OPR\/([\d.]+)/ },
    { name: "Chrome", regex: /Chrome\/([\d.]+)/ },
    { name: "Chromium", regex: /Chromium\/([\d.]+)/ }
  ];

  for (const candidate of candidates) {
    const match = ua.match(candidate.regex);
    if (match) {
      return { name: candidate.name, version: match[1] };
    }
  }

  return { name: "Unknown Chromium Browser", version: "unknown" };
}

async function getTabsSummary(settings = null) {
  const accessSettings = settings || await getSettings();
  const tabs = await chrome.tabs.query({});
  const summaries = [];
  for (const tab of tabs) {
    const decision = await accessDecisionForTab(tab, accessSettings);
    if (decision.allowed) {
      summaries.push(tabSummary(tab));
    }
  }
  return summaries;
}

async function postCommandResults(settings, client, results) {
  const response = await fetch(joinUrl(settings.serverUrl, BRIDGE_RESULT_PATH), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${settings.pairingToken}`
    },
    body: JSON.stringify({
      event: "command_results",
      pairing_token: settings.pairingToken,
      client_id: client.client_id,
      client,
      results
    })
  });
  const envelope = await safeJson(response);
  if (!response.ok || envelope.status === "error") {
    throw new Error(`Bridge result post failed (${response.status}): ${JSON.stringify(envelope)}`);
  }
}

async function executeBridgeCommand(command, settings = null) {
  const startedAt = new Date().toISOString();
  const action = String(command?.action || "");
  const accessSettings = settings || await getSettings();
  try {
    const pollDecision = TobkiriBrowserAccessPolicy.canPoll(accessSettings);
    if (!pollDecision.allowed) {
      throw new Error(pollDecision.message);
    }
    const result = await dispatchCommand(command, accessSettings);
    const semantics = actionResultSemantics(action, result);
    const publicResultFields = topLevelResultFields(result);
    await recordActivity({
      kind: "command",
      action,
      ok: true,
      origin: result?.tab?.url ? safeOrigin(result.tab.url) : ""
    });
    return {
      command_id: command.command_id || null,
      action,
      ok: true,
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      ...semantics,
      ...publicResultFields,
      result
    };
  } catch (error) {
    const semantics = actionResultSemantics(action);
    const message = String(error && error.message ? error.message : error);
    await recordActivity({
      kind: "command",
      action,
      ok: false,
      message
    });
    return {
      command_id: command.command_id || null,
      action,
      ok: false,
      started_at: startedAt,
      finished_at: new Date().toISOString(),
      ...semantics,
      error: message
    };
  }
}

function topLevelResultFields(result) {
  const fields = {};
  if (!result || typeof result !== "object") {
    return fields;
  }
  for (const key of [
    "snapshot",
    "elements",
    "client_profile",
    "browser_profile_id",
    "profile_label",
    "installation_id",
    "tab",
    "tabs",
    "active_tab_id",
    "url"
  ]) {
    if (result[key] !== undefined) {
      fields[key] = result[key];
    }
  }
  return fields;
}

function actionResultSemantics(action, result) {
  if (action === "page.capture") {
    return { requires_foreground: true, can_parallel_user_work: false };
  }
  if (action === "page.snapshot" && result && typeof result === "object" && result.capture) {
    return { requires_foreground: true, can_parallel_user_work: false };
  }
  if (action === "browser.select_tab") {
    return { requires_foreground: true, can_parallel_user_work: false };
  }
  if (
    action === "browser.tabs" ||
    action === "page.navigate" ||
    action === "page.snapshot" ||
    action === "page.click" ||
    action === "page.type" ||
    action === "page.press" ||
    action === "page.scroll" ||
    action === "page.extract" ||
    action === "page.highlight" ||
    action === "page.clear_highlight"
  ) {
    return { requires_foreground: false, can_parallel_user_work: true };
  }
  return {};
}

async function dispatchCommand(command, settings) {
  const action = String(command?.action || "");
  const payload = command && typeof command.payload === "object" ? command.payload : {};
  switch (action) {
    case "browser.tabs":
      return listTabs(settings);
    case "browser.select_tab":
      return selectTab(payload, settings);
    case "page.navigate":
      return navigateTab(payload, settings);
    case "page.capture":
      return captureVisibleTab(payload, settings);
    case "page.snapshot":
      return captureDomSnapshot(payload, settings);
    case "page.click":
    case "page.type":
    case "page.press":
    case "page.scroll":
    case "page.extract":
    case "page.highlight":
    case "page.clear_highlight":
      return sendElementCommand(action, payload, settings);
    default:
      throw new Error(`Unsupported command action: ${action}`);
  }
}

async function listTabs(settings) {
  const tabs = await getTabsSummary(settings);
  const activeTab = tabs.find((tab) => tab.active) || null;
  return {
    tabs,
    active_tab_id: activeTab ? activeTab.id : null,
    requires_foreground: false,
    can_parallel_user_work: true
  };
}

async function selectTab(payload, settings) {
  const { tab, tabId } = await authorizeTargetTab(payload, settings);
  await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  const selected = await chrome.tabs.get(tabId);
  return {
    tab: tabSummary(selected),
    active_tab_id: selected.id,
    requires_foreground: true,
    can_parallel_user_work: false
  };
}

async function navigateTab(payload, settings) {
  if (!payload.url) {
    throw new Error("navigate requires url");
  }
  const { tabId } = await authorizeTargetTab(payload, settings);
  await authorizeUrl(payload.url, settings, { incognito: false });
  const tab = await chrome.tabs.update(tabId, { url: payload.url });
  return {
    tab: tabSummary(tab),
    active_tab_id: tab.id,
    url: tab.url || payload.url,
    requires_foreground: false,
    can_parallel_user_work: true
  };
}

async function captureVisibleTab(payload, settings) {
  const { tab, tabId } = await authorizeTargetTab(payload, settings);
  await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  const format = payload.format === "jpeg" ? "jpeg" : "png";
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format,
    quality: format === "jpeg" ? Math.max(0, Math.min(100, Number(payload.quality) || 90)) : undefined
  });
  const activeTab = await chrome.tabs.get(tabId);
  return {
    tab: tabSummary(activeTab),
    active_tab_id: activeTab.id,
    data_url: dataUrl,
    image_size: imageSizeFromDataUrl(dataUrl),
    requires_foreground: true,
    can_parallel_user_work: false,
    target_window: {
      window_id: tab.windowId
    }
  };
}

async function captureDomSnapshot(payload, settings) {
  const { tabId } = await authorizeTargetTab(payload, settings);
  const identity = await ensureClientIdentity();
  const clientProfile = buildClientProfileMetadata(settings, identity, detectBrowser());
  const snapshotOptions = {};
  if (payload.include_hidden !== undefined) {
    snapshotOptions.includeHidden = Boolean(payload.include_hidden);
  } else if (payload.includeHidden !== undefined) {
    snapshotOptions.includeHidden = Boolean(payload.includeHidden);
  }
  if (payload.include_html !== undefined) {
    snapshotOptions.includeHtml = Boolean(payload.include_html);
  } else if (payload.includeHtml !== undefined) {
    snapshotOptions.includeHtml = Boolean(payload.includeHtml);
  }
  if (payload.include_attributes !== undefined) {
    snapshotOptions.includeAttributes = Boolean(payload.include_attributes);
  } else if (payload.includeAttributes !== undefined) {
    snapshotOptions.includeAttributes = Boolean(payload.includeAttributes);
  }
  if (Array.isArray(payload.attribute_names)) {
    snapshotOptions.attributeNames = payload.attribute_names;
  } else if (Array.isArray(payload.attributeNames)) {
    snapshotOptions.attributeNames = payload.attributeNames;
  }
  if (payload.include_semantics !== undefined) {
    snapshotOptions.includeSemantics = Boolean(payload.include_semantics);
  } else if (payload.includeSemantics !== undefined) {
    snapshotOptions.includeSemantics = Boolean(payload.includeSemantics);
  }

  const snapshotRequest = {
    type: "rumi:dom-snapshot",
    maxNodes: payload.limit,
    clientProfile
  };
  if (Object.keys(snapshotOptions).length > 0) {
    snapshotRequest.options = snapshotOptions;
  }

  const rawSnapshot = await sendToTab(tabId, snapshotRequest);
  const tab = await chrome.tabs.get(tabId);
  const snapshot = enrichSnapshot(rawSnapshot, clientProfile, tab);
  const elements = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
  const result = {
    tab: tabSummary(tab),
    active_tab_id: tab.id,
    snapshot,
    elements,
    client_profile: clientProfile,
    browser_profile_id: clientProfile.browser_profile_id,
    profile_label: clientProfile.profile_label,
    installation_id: clientProfile.installation_id,
    requires_foreground: false,
    can_parallel_user_work: true
  };
  if (payload.include_capture) {
    result.capture = await captureVisibleTab(
      { ...payload, tab_id: tabId },
      settings
    );
    result.requires_foreground = true;
    result.can_parallel_user_work = false;
  }
  return result;
}

async function sendElementCommand(action, payload, settings) {
  const { tabId } = await authorizeTargetTab(payload, settings);
  const identity = await ensureClientIdentity();
  const clientProfile = buildClientProfileMetadata(settings, identity, detectBrowser());
  const result = await sendToTab(tabId, {
    type: "rumi:element-command",
    command: {
      action,
      ...payload
    }
  });
  const tab = await chrome.tabs.get(tabId);
  return {
    ...result,
    tab: tabSummary(tab),
    active_tab_id: tab.id,
    url: tab.url || "",
    client_profile: clientProfile,
    browser_profile_id: clientProfile.browser_profile_id,
    profile_label: clientProfile.profile_label,
    installation_id: clientProfile.installation_id,
    requires_foreground: false,
    can_parallel_user_work: true
  };
}

function enrichSnapshot(snapshot, clientProfile, tab) {
  const value = snapshot && typeof snapshot === "object" ? { ...snapshot } : { ok: false, nodes: [] };
  const elements = Array.isArray(value.nodes) ? value.nodes : Array.isArray(value.elements) ? value.elements : [];
  value.nodes = elements;
  value.elements = elements;
  value.client_profile = clientProfile;
  value.browser_profile_id = clientProfile.browser_profile_id;
  value.profile_label = clientProfile.profile_label;
  value.installation_id = clientProfile.installation_id;
  value.snapshot_metadata = {
    ...(value.snapshot_metadata && typeof value.snapshot_metadata === "object" ? value.snapshot_metadata : {}),
    source: "rumi_browser_companion",
    tab_id: tab.id,
    window_id: tab.windowId,
    browser_profile_id: clientProfile.browser_profile_id,
    profile_label: clientProfile.profile_label,
    installation_id: clientProfile.installation_id
  };
  return value;
}

async function sendToTab(tabId, message) {
  const resolvedTabId = Number(tabId);
  if (!Number.isInteger(resolvedTabId)) {
    throw new Error("A numeric tab_id is required");
  }
  await ensureContentScript(resolvedTabId);
  try {
    return await chrome.tabs.sendMessage(resolvedTabId, message);
  } catch (error) {
    throw new Error(`Tab message failed for ${resolvedTabId}: ${String(error && error.message ? error.message : error)}`);
  }
}

async function ensureContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "rumi:access-ping" });
    return;
  } catch (_error) {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content_script.js"]
    });
  }
}

async function authorizeTargetTab(payload, settings) {
  const tabId = await resolveTabId(payload.tab_id);
  const tab = await chrome.tabs.get(tabId);
  const decision = await accessDecisionForTab(tab, settings);
  if (!decision.allowed) {
    throw new Error(decision.message);
  }
  return { tab, tabId, decision };
}

async function accessDecisionForTab(tab, settings) {
  const origin = safeOrigin(tab?.url || "");
  const pattern = TobkiriBrowserAccessPolicy.permissionPattern(origin);
  const hasHostPermission = pattern
    ? await chrome.permissions.contains({ origins: [pattern] })
    : false;
  return TobkiriBrowserAccessPolicy.evaluateUrl(tab?.url || "", settings, {
    incognito: tab?.incognito === true,
    hasHostPermission
  });
}

async function authorizeUrl(value, settings, context = {}) {
  const origin = safeOrigin(value);
  const pattern = TobkiriBrowserAccessPolicy.permissionPattern(origin);
  const hasHostPermission = pattern
    ? await chrome.permissions.contains({ origins: [pattern] })
    : false;
  const decision = TobkiriBrowserAccessPolicy.evaluateUrl(value, settings, {
    ...context,
    hasHostPermission
  });
  if (!decision.allowed) {
    throw new Error(decision.message);
  }
  return decision;
}

function safeOrigin(value) {
  try {
    return new URL(String(value || "")).origin;
  } catch (_error) {
    return "";
  }
}

async function resolveTabId(candidate) {
  const tabId = Number(candidate);
  if (Number.isInteger(tabId)) {
    return tabId;
  }
  const focusedTabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (focusedTabs[0]?.id != null) {
    return focusedTabs[0].id;
  }
  const activeTabs = await chrome.tabs.query({ active: true });
  if (activeTabs[0]?.id != null) {
    return activeTabs[0].id;
  }
  throw new Error("No active tab is available");
}

async function safeJson(response) {
  const text = await response.text();
  if (!text) {
    return {};
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    return { raw: text, parse_error: String(error && error.message ? error.message : error) };
  }
}

function joinUrl(base, path) {
  return `${String(base || "").replace(/\/+$/, "")}${path}`;
}

function unwrapBridgePayload(value) {
  if (value && typeof value === "object" && value.data && typeof value.data === "object") {
    return value.data;
  }
  return value && typeof value === "object" ? value : {};
}

function normalizeCommands(payload) {
  if (Array.isArray(payload.commands)) {
    return payload.commands.filter((item) => item && typeof item === "object");
  }
  if (payload.command && typeof payload.command === "object") {
    return [payload.command];
  }
  return [];
}

async function loadSearchHomeRouteStates() {
  const stored = await chrome.storage.local.get(SEARCH_HOME_ROUTE_STATE_KEY);
  const value = stored[SEARCH_HOME_ROUTE_STATE_KEY];
  return value && typeof value === "object" ? value : {};
}

async function saveSearchHomeRouteStates(states) {
  await chrome.storage.local.set({ [SEARCH_HOME_ROUTE_STATE_KEY]: states });
}

async function clearSearchHomeRouteState(tabId) {
  if (!Number.isInteger(tabId)) {
    return;
  }
  const states = await loadSearchHomeRouteStates();
  if (!(String(tabId) in states)) {
    return;
  }
  delete states[String(tabId)];
  await saveSearchHomeRouteStates(states);
}

async function setSearchHomeRouteState(tabId, payload, metadata = {}) {
  if (!Number.isInteger(tabId)) {
    return { ok: false, error: "Active tab is required for Search Home route state." };
  }
  const sourceOrigin = trustedSearchHomeSourceOrigin(metadata.senderUrl, metadata.sourceOrigin);
  if (!sourceOrigin) {
    return { ok: false, error: "Search Home route state must come from a trusted Search Home origin." };
  }
  const normalized = normalizeSearchHomeRouteState(payload, { sourceOrigin });
  if (!normalized) {
    return { ok: false, error: "Invalid Search Home route payload." };
  }
  const states = await loadSearchHomeRouteStates();
  const existing = states[String(tabId)];
  if (existing?.state_id && existing.state_id === normalized.state_id) {
    return { ok: false, error: "Search Home route state replay was rejected." };
  }
  states[String(tabId)] = normalized;
  await saveSearchHomeRouteStates(states);
  return {
    ok: true,
    active: true,
    tab_id: tabId,
    selected_index: normalized.selected_index,
    expires_at: searchHomeRouteStateExpiresAt(normalized)
  };
}

async function getSearchHomeRouteState(tabId) {
  if (!Number.isInteger(tabId)) {
    return { ok: true, active: false };
  }
  const states = await loadSearchHomeRouteStates();
  const stateKey = String(tabId);
  const current = normalizeSearchHomeRouteState(states[stateKey]);
  if (!current || !isFreshSearchHomeRouteState(current) || !isTrustedStoredSearchHomeRouteState(current)) {
    if (stateKey in states) {
      delete states[stateKey];
      await saveSearchHomeRouteStates(states);
    }
    return { ok: true, active: false, tab_id: tabId };
  }
  return {
    ok: true,
    active: true,
    tab_id: tabId,
    selected_index: current.selected_index,
    expires_at: searchHomeRouteStateExpiresAt(current)
  };
}

async function advanceSearchHomeRouteState(tabId, action) {
  if (!Number.isInteger(tabId)) {
    return { ok: false, error: "Active tab is required for Search Home navigation." };
  }
  const states = await loadSearchHomeRouteStates();
  const current = normalizeSearchHomeRouteState(states[String(tabId)]);
  if (!current || !isFreshSearchHomeRouteState(current) || !isTrustedStoredSearchHomeRouteState(current)) {
    return { ok: false, error: "No fresh Search Home route state was found for this tab." };
  }
  let url = "";
  let nextIndex = normalizeSearchHomeIndex(current, current.selected_index);
  const normalizedAction = normalizeSearchHomeRouteAction(action);
  if (normalizedAction === "fallback") {
    url = normalizeSearchHomeCandidateUrl(current.fallback_url);
  } else if (normalizedAction === "open") {
    const selectedCandidate = current.target_candidates[nextIndex];
    url = normalizeSearchHomeCandidateUrl(selectedCandidate?.final_url || selectedCandidate?.url || current.target_url);
  } else {
    const delta = normalizedAction === "prev" ? -1 : 1;
    nextIndex = nextSearchHomeIndex(current, delta);
    const nextCandidate = current.target_candidates[nextIndex];
    url = normalizeSearchHomeCandidateUrl(nextCandidate?.final_url || nextCandidate?.url);
  }
  if (!url) {
    return { ok: false, error: "No destination URL was available for the requested Search Home action." };
  }
  delete states[String(tabId)];
  await saveSearchHomeRouteStates(states);
  await chrome.tabs.update(tabId, { url });
  return { ok: true, tab_id: tabId, url, selected_index: nextIndex };
}

function normalizeSearchHomeRouteAction(action) {
  const value = String(action || "").trim().toLowerCase();
  if (value === "previous" || value === "prev" || value === "left") {
    return "prev";
  }
  if (value === "open" || value === "enter") {
    return "open";
  }
  if (value === "fallback") {
    return "fallback";
  }
  return "next";
}

function normalizeSearchHomeRouteState(value, options = {}) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const rawCandidates = Array.isArray(value.target_candidates) ? value.target_candidates : [];
  const candidates = rawCandidates.length
    ? rawCandidates
        .map((candidate) => normalizeSearchHomeCandidate(candidate))
        .filter(Boolean)
    : [];
  if (candidates.length !== rawCandidates.length) {
    return null;
  }
  const fallbackUrl = normalizeSearchHomeCandidateUrl(value.fallback_url);
  const selectedIndex = normalizeSearchHomeIndex({ target_candidates: candidates }, Number(value.selected_index));
  const targetUrl = normalizeSearchHomeCandidateUrl(value.target_url) || fallbackUrl || (candidates[0]?.final_url || candidates[0]?.url || "");
  if (!targetUrl && !fallbackUrl && candidates.length === 0) {
    return null;
  }
  const issuedAt = Date.parse(String(value.issued_at || ""));
  const expiresAt = Date.parse(String(value.expires_at || ""));
  const now = Date.now();
  if (!Number.isFinite(issuedAt) || !Number.isFinite(expiresAt) ||
      issuedAt > now + SEARCH_HOME_MAX_CLOCK_SKEW_MS || expiresAt <= now ||
      expiresAt - issuedAt > SEARCH_HOME_ROUTE_MAX_AGE_MS) {
    return null;
  }
  const stateId = String(value.state_id || "");
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(stateId)) return null;
  return {
    query: sanitizeSearchHomeQuery(value.query),
    target_url: targetUrl,
    fallback_url: fallbackUrl,
    selected_index: selectedIndex,
    target_candidates: candidates,
    source_origin: String(options.sourceOrigin || value.source_origin || ""),
    state_id: stateId,
    issued_at: new Date(issuedAt).toISOString(),
    expires_at: new Date(expiresAt).toISOString()
  };
}

function normalizeSearchHomeCandidate(value) {
  if (!value || typeof value !== "object") {
    return null;
  }
  const policy = RumiSearchHomeDestinationPolicy.evaluateRedirect(
    value.url || value.final_url,
    value.final_url || value.url,
    Boolean(value.redirected)
  );
  if (policy.verdict !== "allow") {
    return null;
  }
  const initial = RumiSearchHomeDestinationPolicy.evaluate(value.url || value.final_url);
  const initialUrl = RumiSearchHomeDestinationPolicy.safeForPersistence(initial.url || policy.url);
  const finalUrl = RumiSearchHomeDestinationPolicy.safeForPersistence(policy.url);
  if (!initialUrl || !finalUrl) return null;
  return {
    url: initialUrl,
    final_url: finalUrl,
    title: String(value.title || ""),
    domain: policy.host,
    redirected: Boolean(value.redirected)
  };
}

function normalizeSearchHomeCandidateUrl(value) {
  const result = RumiSearchHomeDestinationPolicy.evaluate(value);
  return result.verdict === "allow" ? RumiSearchHomeDestinationPolicy.safeForPersistence(result.url) : "";
}

function sanitizeSearchHomeQuery(value) {
  const query = String(value || "");
  if (!query.includes("://")) return query;
  return RumiSearchHomeDestinationPolicy.safeForPersistence(query) ? query : "";
}

function normalizeSearchHomeIndex(state, value) {
  const total = Array.isArray(state?.target_candidates) ? state.target_candidates.length : 0;
  if (total <= 0) {
    return -1;
  }
  return Number.isInteger(value) && value >= 0 && value < total ? value : 0;
}

function nextSearchHomeIndex(state, delta) {
  const total = Array.isArray(state?.target_candidates) ? state.target_candidates.length : 0;
  if (total <= 0) {
    return -1;
  }
  const base = normalizeSearchHomeIndex(state, state.selected_index);
  return (base + delta + total) % total;
}

function isFreshSearchHomeRouteState(state) {
  if (!state || typeof state.issued_at !== "string" || typeof state.expires_at !== "string") {
    return false;
  }
  const issuedAt = Date.parse(state.issued_at);
  const expiresAt = Date.parse(state.expires_at);
  const now = Date.now();
  if (!Number.isFinite(issuedAt) || !Number.isFinite(expiresAt)) {
    return false;
  }
  return issuedAt <= now + SEARCH_HOME_MAX_CLOCK_SKEW_MS && expiresAt > now &&
    expiresAt - issuedAt <= SEARCH_HOME_ROUTE_MAX_AGE_MS;
}

function searchHomeRouteStateExpiresAt(state) {
  const expiresAt = Date.parse(state?.expires_at || "");
  if (!Number.isFinite(expiresAt)) {
    return 0;
  }
  return expiresAt;
}

function isTrustedStoredSearchHomeRouteState(state) {
  return RumiSearchHomeDestinationPolicy.isTrustedSearchHomeOrigin(state?.source_origin, SEARCH_HOME_TRUSTED_ORIGINS);
}

function trustedSearchHomeSourceOrigin(senderUrl, claimedOrigin) {
  let senderOrigin = "";
  try {
    senderOrigin = new URL(String(senderUrl || "")).origin;
  } catch (_error) {
    return "";
  }
  const sourceOrigin = String(claimedOrigin || "");
  if (!sourceOrigin || senderOrigin !== sourceOrigin) {
    return "";
  }
  return RumiSearchHomeDestinationPolicy.isTrustedSearchHomeOrigin(sourceOrigin, SEARCH_HOME_TRUSTED_ORIGINS) ? sourceOrigin : "";
}

function tabSummary(tab) {
  return {
    id: tab.id,
    windowId: tab.windowId,
    active: Boolean(tab.active),
    audible: Boolean(tab.audible),
    discarded: Boolean(tab.discarded),
    favIconUrl: tab.favIconUrl || "",
    pinned: Boolean(tab.pinned),
    status: tab.status || "unknown",
    title: tab.title || "",
    url: tab.url || ""
  };
}

function imageSizeFromDataUrl(dataUrl) {
  const match = /^data:image\/[a-z0-9.+-]+;base64,([A-Za-z0-9+/=]+)$/i.exec(String(dataUrl || ""));
  if (!match) {
    return null;
  }
  try {
    const bytes = Uint8Array.from(atob(match[1]), (char) => char.charCodeAt(0));
    if (bytes.length >= 24 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) {
      const view = new DataView(bytes.buffer);
      return {
        width: view.getUint32(16),
        height: view.getUint32(20)
      };
    }
  } catch (_error) {
    return null;
  }
  return null;
}
