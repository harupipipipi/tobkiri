// Injected only by the scoped native debug observer into the trusted root.
// Child data is observation data. It never chooses a native operation.
(async function runRootProbe() {
  const prefix = 'TOBKIRI_NATIVE_IPC_PROBE:';
  const commands = [
    'core_start_profile', 'core_stop_session', 'core_host_output_ack',
    'pack_ui_invoke', 'pack_ui_register', 'pack_ui_cancel', 'pack_ui_ack',
    'pack_ui_detach', 'pack_ui_stream_pull', 'pack_ui_stream_cancel',
    'core_terminal_reply',
  ];
  const report = {
    version: 1, root_start: false, iframe_loaded: false, root_stop: false,
    parent_dom_blocked: false, parent_internals_blocked: false,
    storage_blocked: false, sdk_exposed: false, sdk_attempts: 0,
    sdk_rejected: 0, raw_surfaces: 0, raw_sent: 0,
    command_count: commands.length, failure: 'none',
  };
  let completed = false;
  const finish = (failure) => {
    if (completed) return;
    completed = true;
    report.failure = failure;
    document.title = prefix + JSON.stringify(report);
  };
  const timer = window.setTimeout(() => finish('timeout'), 20000);
  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const keys = (value, expected) => value && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === expected.slice().sort().join(',');
  let childReport;
  const received = new Promise((resolve) => { childReport = resolve; });
  const listener = (event) => {
    const frames = [...document.querySelectorAll('#pack-ui iframe')];
    if (event.origin !== 'null' || frames.length !== 1
      || event.source !== frames[0].contentWindow) return;
    const value = event.data;
    if (!keys(value, ['type', 'version', 'parent_dom_blocked', 'parent_internals_blocked',
      'storage_blocked', 'surfaces', 'attempts'])
      || value.type !== 'tobkiri-native-ipc-probe-child' || value.version !== 1) return;
    let size;
    try { size = JSON.stringify(value).length; } catch { finish('schema'); return; }
    if (size > 16384
      || !keys(value.surfaces, ['sdk', 'ipc', 'webkit'])
      || !['parent_dom_blocked', 'parent_internals_blocked', 'storage_blocked']
        .every((name) => typeof value[name] === 'boolean')
      || !['sdk', 'ipc', 'webkit'].every((name) => typeof value.surfaces[name] === 'boolean')
      || !Array.isArray(value.attempts) || value.attempts.length > 33) {
      finish('schema'); return;
    }
    const expected = [];
    for (const via of ['sdk', 'ipc', 'webkit']) {
      if (value.surfaces[via]) for (const command of commands) expected.push(via + ':' + command);
    }
    const actual = [];
    for (const attempt of value.attempts) {
      if (!keys(attempt, ['command', 'via', 'result'])
        || !commands.includes(attempt.command)
        || !['sdk', 'ipc', 'webkit'].includes(attempt.via)
        || !['returned', 'rejected', 'timeout', 'error', 'sent'].includes(attempt.result)) {
        finish('schema'); return;
      }
      actual.push(attempt.via + ':' + attempt.command);
    }
    if (actual.slice().sort().join(',') !== expected.sort().join(',')) {
      finish('schema'); return;
    }
    childReport(value);
  };
  window.addEventListener('message', listener);
  try {
    const form = document.getElementById('profile-form');
    const input = document.getElementById('profile-id');
    if (!(form instanceof HTMLFormElement) || !(input instanceof HTMLInputElement)
      || document.querySelector('#pack-ui iframe')) { finish('root-start'); return; }
    input.value = 'prof_native_ipc_probe';
    form.requestSubmit();
    let frame;
    for (let attempt = 0; attempt < 100; attempt += 1) {
      frame = document.querySelector('#pack-ui iframe');
      if (frame) break;
      await wait(50);
    }
    if (!frame || frame.getAttribute('sandbox') !== 'allow-scripts'
      || !/^tobkiri-pack:\/\/[a-f0-9]{64}\//.test(frame.src)) {
      finish('root-start'); return;
    }
    report.root_start = true;
    report.iframe_loaded = true;
    const child = await received;
    report.parent_dom_blocked = child.parent_dom_blocked;
    report.parent_internals_blocked = child.parent_internals_blocked;
    report.storage_blocked = child.storage_blocked;
    report.sdk_exposed = child.surfaces.sdk;
    report.sdk_attempts = child.attempts.filter((item) => item.via === 'sdk').length;
    report.sdk_rejected = child.attempts.filter((item) => item.via === 'sdk'
      && item.result === 'rejected').length;
    report.raw_surfaces = Number(child.surfaces.ipc) + Number(child.surfaces.webkit);
    report.raw_sent = child.attempts.filter((item) => item.via !== 'sdk'
      && item.result === 'sent').length;
    // Let native raw messages settle; the controller independently requires
    // one actual native bad-key rejection for every reported raw send.
    await wait(300);
    const internals = window.__TAURI_INTERNALS__;
    const invoke = internals && internals.invoke;
    if (typeof invoke !== 'function') { finish('stop'); return; }
    try { await invoke.call(internals, 'core_stop_session'); } catch { finish('stop'); return; }
    report.root_stop = true;
    finish('none');
  } catch {
    finish(report.root_start ? 'child-report' : 'root-start');
  } finally {
    window.clearTimeout(timer);
    window.removeEventListener('message', listener);
  }
})();
