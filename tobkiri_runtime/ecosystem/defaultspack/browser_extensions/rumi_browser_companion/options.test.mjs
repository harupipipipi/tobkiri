import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const optionsSource = await readFile(new URL("./options.js", import.meta.url), "utf8");

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) {
      listener({ preventDefault() {}, ...event });
    }
  }
}

class FakeElement extends FakeEventTarget {
  constructor(id) {
    super();
    this.id = id;
    this.textContent = "";
    this.className = "";
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.open = false;
    this.dateTime = "";
    this.attributes = new Map();
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }
}

class FakeDocument extends FakeEventTarget {
  constructor(elements) {
    super();
    this.elements = elements;
  }

  getElementById(id) {
    return this.elements[id];
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function createHarness({
  storageGet = async () => ({}),
  storageSet = async () => undefined,
  sendMessage = async () => ({ ok: true, state: "idle" })
} = {}) {
  const ids = [
    "settings-form",
    "save-settings",
    "poll-now",
    "action-feedback",
    "action-state",
    "load-state",
    "save-state",
    "poll-state",
    "action-status",
    "retry-action",
    "connection-state",
    "status",
    "status-endpoint",
    "status-profile",
    "status-last-contact",
    "status-freshness",
    "status-updated",
    "diagnostic-disclosure",
    "diagnostic-details",
    "copy-diagnostic",
    "copy-feedback"
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));
  const inputs = {
    serverUrl: new FakeElement("server-url"),
    pairingToken: new FakeElement("pairing-token"),
    clientLabel: new FakeElement("client-label"),
    profileLabel: new FakeElement("profile-label"),
    pollIntervalMinutes: new FakeElement("poll-interval")
  };
  const form = elements["settings-form"];
  Object.assign(form, inputs);
  form.elements = [
    ...Object.values(inputs),
    elements["save-settings"],
    elements["poll-now"]
  ];

  const timeouts = new Map();
  let nextTimerId = 1;
  const window = new FakeEventTarget();
  window.setTimeout = (callback) => {
    const id = nextTimerId++;
    timeouts.set(id, callback);
    return id;
  };
  window.clearTimeout = (id) => timeouts.delete(id);
  window.setInterval = () => nextTimerId++;
  window.clearInterval = () => undefined;

  const clipboardWrites = [];
  const document = new FakeDocument(elements);
  const runtimeMessages = [];
  const context = vm.createContext({
    AbortController,
    URL,
    Date,
    Error,
    HTMLElement: FakeElement,
    console,
    document,
    navigator: {
      clipboard: {
        writeText: async (value) => {
          clipboardWrites.push(value);
        }
      }
    },
    window,
    chrome: {
      storage: {
        local: {
          get: (...args) => storageGet(...args),
          set: (...args) => storageSet(...args)
        }
      },
      runtime: {
        sendMessage: (message) => {
          runtimeMessages.push(message);
          return sendMessage(message);
        }
      }
    }
  });
  vm.runInContext(optionsSource, context, { filename: "options.js" });

  return {
    clipboardWrites,
    context,
    document,
    elements,
    inputs,
    runtimeMessages,
    runTimeouts() {
      const callbacks = [...timeouts.values()];
      timeouts.clear();
      callbacks.forEach((callback) => callback());
    }
  };
}

test("background unavailable renders a retryable load error and recovers", async () => {
  let available = false;
  const harness = createHarness({
    storageGet: async () => ({
      rumiBrowserCompanionSettings: {
        serverUrl: "http://127.0.0.1:8766",
        pairingToken: "never-copy-this-token",
        profileLabel: "Work"
      }
    }),
    sendMessage: async () => {
      if (!available) {
        throw new Error("Could not establish connection. Receiving end does not exist.");
      }
      return { ok: true, state: "idle", updatedAt: "2026-08-23T10:00:00Z" };
    }
  });

  harness.document.emit("DOMContentLoaded");
  assert.equal(harness.elements["load-state"].textContent, "In progress");
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.state, "error");
  assert.equal(harness.elements["load-state"].textContent, "Needs attention");
  assert.equal(harness.elements["retry-action"].hidden, false);
  assert.match(harness.elements["action-status"].textContent, /background worker may be unavailable/i);
  assert.doesNotMatch(
    harness.elements["diagnostic-details"].textContent,
    /never-copy-this-token/
  );

  available = true;
  harness.elements["retry-action"].emit("click");
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.state, "success");
  assert.equal(harness.elements["load-state"].textContent, "Succeeded");
  assert.equal(harness.elements["retry-action"].hidden, true);
});

test("storage rejection renders retry for load and save", async () => {
  let rejectLoad = true;
  let rejectSave = true;
  const harness = createHarness({
    storageGet: async () => {
      if (rejectLoad) throw new Error("storage unavailable");
      return {};
    },
    storageSet: async () => {
      if (rejectSave) throw new Error("storage write unavailable");
    }
  });

  harness.document.emit("DOMContentLoaded");
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.action, "load");
  assert.equal(harness.elements["action-feedback"].dataset.state, "error");

  rejectLoad = false;
  harness.elements["retry-action"].emit("click");
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.state, "success");

  harness.elements["settings-form"].emit("submit");
  assert.equal(harness.elements["save-state"].textContent, "In progress");
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.action, "save");
  assert.equal(harness.elements["action-feedback"].dataset.state, "error");
  assert.equal(harness.elements["retry-action"].hidden, false);

  rejectSave = false;
  harness.elements["retry-action"].emit("click");
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.state, "success");
});

test("duplicate poll clicks share one pending runtime request", async () => {
  const pendingPoll = deferred();
  let pollCalls = 0;
  const harness = createHarness({
    sendMessage: async (message) => {
      if (message.type === "rumi:get-status") {
        return { ok: true, state: "idle" };
      }
      pollCalls += 1;
      return pendingPoll.promise;
    }
  });
  harness.document.emit("DOMContentLoaded");
  await flush();

  harness.elements["poll-now"].emit("click");
  harness.elements["poll-now"].emit("click");
  await flush();
  assert.equal(pollCalls, 1);
  assert.equal(harness.elements["poll-now"].disabled, true);
  assert.equal(harness.elements["poll-now"].textContent, "Polling…");
  assert.equal(harness.elements["action-feedback"].dataset.state, "pending");
  assert.equal(harness.elements["poll-state"].textContent, "In progress");

  pendingPoll.resolve({
    ok: true,
    state: "connected",
    commandCount: 0,
    serverUrl: "http://127.0.0.1:8766",
    profileLabel: "Work",
    updatedAt: new Date().toISOString(),
    lastSuccessfulContactAt: new Date().toISOString()
  });
  await flush();
  assert.equal(harness.elements["action-feedback"].dataset.state, "success");
  assert.equal(harness.elements["poll-state"].textContent, "Succeeded");
  assert.equal(harness.elements["poll-now"].disabled, false);
  assert.equal(harness.elements["connection-state"].textContent, "Connected");
});

test("runtime timeout and malformed response are retryable", async () => {
  let mode = "load";
  const never = deferred();
  const harness = createHarness({
    sendMessage: (message) => {
      if (message.type === "rumi:get-status") {
        return { ok: true, state: "idle" };
      }
      if (mode === "timeout") return never.promise;
      if (mode === "malformed") return { unexpected: true };
      return { ok: true, state: "connected", updatedAt: new Date().toISOString() };
    }
  });
  harness.document.emit("DOMContentLoaded");
  await flush();

  mode = "timeout";
  harness.elements["poll-now"].emit("click");
  await flush();
  harness.runTimeouts();
  await flush();
  assert.match(harness.elements["action-status"].textContent, /timed out/i);
  assert.equal(harness.elements["retry-action"].hidden, false);

  mode = "malformed";
  harness.elements["retry-action"].emit("click");
  await flush();
  assert.match(harness.elements["action-status"].textContent, /invalid response/i);
  assert.equal(harness.elements["retry-action"].hidden, false);
});

test("error state maps actionable copy, freshness, and redacted diagnostics", async () => {
  const oldContact = "2026-08-20T10:00:00Z";
  const token = "pairing-token-super-secret";
  const harness = createHarness({
    storageGet: async () => ({
      rumiBrowserCompanionSettings: {
        serverUrl: "http://user:password@127.0.0.1:8766/path?token=secret",
        pairingToken: token,
        profileLabel: "Personal",
        pollIntervalMinutes: 1
      }
    }),
    sendMessage: async () => ({
      ok: false,
      state: "pairing_rejected",
      serverUrl: "http://user:password@127.0.0.1:8766/path?token=secret",
      profileLabel: "Personal",
      updatedAt: "2026-08-23T10:00:00Z",
      lastSuccessfulContactAt: oldContact,
      diagnostic: {
        code: "PAIRING_REJECTED",
        authorization: `Bearer ${token}`,
        reason: `server echoed ${token}; token=prior-token-must-not-escape`
      }
    })
  });
  harness.document.emit("DOMContentLoaded");
  await flush();

  assert.equal(harness.elements["connection-state"].textContent, "Pairing rejected");
  assert.match(harness.elements["status"].textContent, /Generate a new pairing token/);
  assert.equal(harness.elements["status-endpoint"].textContent, "http://127.0.0.1:8766/path");
  assert.equal(harness.elements["status-profile"].textContent, "Personal");
  assert.match(harness.elements["status-freshness"].textContent, /^Stale/);
  assert.equal(harness.elements["diagnostic-disclosure"].hidden, false);
  assert.doesNotMatch(harness.elements["diagnostic-details"].textContent, new RegExp(token));
  assert.doesNotMatch(
    harness.elements["diagnostic-details"].textContent,
    /prior-token-must-not-escape/
  );
  assert.match(harness.elements["diagnostic-details"].textContent, /\[redacted\]/);

  harness.elements["copy-diagnostic"].emit("click");
  await flush();
  assert.equal(harness.clipboardWrites.length, 1);
  assert.doesNotMatch(harness.clipboardWrites[0], new RegExp(token));
});
