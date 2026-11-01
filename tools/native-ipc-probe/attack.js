// This fixture runs inside the ordinary opaque-origin Pack UI iframe.
// It records only access outcomes and closed IPC result labels. It never
// reads parent data, invoke keys, response bodies, or error messages.
(async function runNativeIpcProbe() {
  const SDK_TIMEOUT_MS = 300;
  const PROFILE_ID = 'prof_native_ipc_probe';

  const commands = [
    { command: 'core_start_profile', args: { profile: PROFILE_ID } },
    { command: 'core_stop_session', args: {} },
    {
      command: 'core_host_output_ack',
      args: { namespace: 'probe_namespace', generation: 1, receiver: 'probe_receiver', ticketId: 1 },
    },
    {
      command: 'pack_ui_invoke',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
        id: 1,
        requestJson: '{}',
      },
    },
    {
      command: 'pack_ui_register',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
        id: 1,
        kind: 'invoke',
        handle: 1,
      },
    },
    {
      command: 'pack_ui_cancel',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
        id: 1,
        kind: 'invoke',
        handle: 1,
      },
    },
    {
      command: 'pack_ui_ack',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
        ticketId: 1,
      },
    },
    {
      command: 'pack_ui_detach',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
      },
    },
    {
      command: 'pack_ui_stream_pull',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
        id: 1,
        handle: 1,
        max: 1,
      },
    },
    {
      command: 'pack_ui_stream_cancel',
      args: {
        namespace: 'probe_namespace',
        uiSessionId: 'probe_ui_session',
        generation: 1,
        id: 1,
        handle: 1,
        abortPull: false,
      },
    },
    { command: 'core_terminal_reply', args: { id: 'probe_terminal_reply', text: 'probe' } },
  ];

  let parentDomBlocked = false;
  try {
    void window.parent.document;
  } catch {
    parentDomBlocked = true;
  }

  let parentInternalsBlocked = false;
  try {
    void window.parent.__TAURI_INTERNALS__;
  } catch {
    parentInternalsBlocked = true;
  }

  let storageBlocked = false;
  try {
    void window.localStorage;
  } catch {
    storageBlocked = true;
  }

  let publicSdkInvoke = null;
  try {
    const publicTauri = window.__TAURI__;
    const publicCore = publicTauri && publicTauri.core;
    const publicInvoke = publicCore && publicCore.invoke;
    if (typeof publicInvoke === 'function') {
      publicSdkInvoke = (command, args) => publicInvoke.call(publicCore, command, args);
    }
  } catch {
    // Surface inspection is reduced to the booleans in the report.
  }
  let internalSdkInvoke = null;
  try {
    const internals = window.__TAURI_INTERNALS__;
    const internalInvoke = internals && internals.invoke;
    if (typeof internalInvoke === 'function') {
      internalSdkInvoke = (command, args) => internalInvoke.call(internals, command, args);
    }
  } catch {
    // Surface inspection is reduced to the booleans in the report.
  }
  const sdkInvoke = publicSdkInvoke || internalSdkInvoke;

  let ipcPostMessage = null;
  try {
    const ipc = window.ipc;
    if (ipc && typeof ipc.postMessage === 'function') {
      ipcPostMessage = (message) => ipc.postMessage(message);
    }
  } catch {
    // Surface inspection is reduced to the booleans in the report.
  }

  let webkitPostMessage = null;
  try {
    const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipc;
    if (handler && typeof handler.postMessage === 'function') {
      webkitPostMessage = (message) => handler.postMessage(message);
    }
  } catch {
    // Surface inspection is reduced to the booleans in the report.
  }

  const attempts = [];
  if (sdkInvoke) {
    for (const item of commands) {
      let timer;
      let result;
      try {
        const pending = sdkInvoke(item.command, item.args);
        result = await Promise.race([
          Promise.resolve(pending).then(
            () => 'returned',
            () => 'rejected',
          ),
          new Promise((resolve) => {
            timer = window.setTimeout(() => resolve('timeout'), SDK_TIMEOUT_MS);
          }),
        ]);
      } catch {
        result = 'error';
      } finally {
        if (timer !== undefined) window.clearTimeout(timer);
      }
      attempts.push({ command: item.command, via: 'sdk', result });
    }
  }

  function postRaw(postMessage, via, item) {
    const request = {
      cmd: item.command,
      callback: 1,
      error: 2,
      options: { customProtocolIpcBlocked: true },
      payload: item.args,
      __TAURI_INVOKE_KEY__: '',
    };
    let result = 'sent';
    try {
      postMessage(JSON.stringify(request));
    } catch {
      result = 'error';
    }
    attempts.push({ command: item.command, via, result });
  }

  if (ipcPostMessage) {
    for (const item of commands) postRaw(ipcPostMessage, 'ipc', item);
  }
  if (webkitPostMessage) {
    for (const item of commands) postRaw(webkitPostMessage, 'webkit', item);
  }

  const report = {
    type: 'tobkiri-native-ipc-probe-child',
    version: 1,
    parent_dom_blocked: parentDomBlocked,
    parent_internals_blocked: parentInternalsBlocked,
    storage_blocked: storageBlocked,
    surfaces: {
      sdk: Boolean(sdkInvoke),
      ipc: Boolean(ipcPostMessage),
      webkit: Boolean(webkitPostMessage),
    },
    attempts,
  };

  try {
    window.parent.postMessage(report, '*');
  } catch {
    // The one bounded report is best effort; no exception text is retained.
  }
})();
