import assert from 'node:assert/strict';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {translate} from '@/src/lib/i18n';
import {useAppStore} from '@/src/store';
import {Settings} from './Settings';

function createDom(): {dom: JSDOM; container: HTMLElement; root: Root} {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {dom, container, root: createRoot(container)};
}

test('Settings identifies Devtools as a Launcher-local switch', () => {
  const previousState = useAppStore.getState();
  try {
    useAppStore.setState({devtoolsEnabled: false});
    const html = renderToStaticMarkup(<Settings />);
    assert.match(html, /role="switch"/);
    assert.match(html, /aria-checked="false"/);
    assert.match(html, /source: launcher_local/);
    assert.match(html, /does not grant runtime authority/);
    assert.match(html, /alter Pack closure/);
  } finally {
    useAppStore.setState(previousState, true);
  }
});

test('Settings localizes its descriptor and primary controls in Japanese', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  try {
    useAppStore.setState({
      profile: {...previousState.profile, language: 'ja'},
      devtoolsEnabled: false,
    });
    await act(async () => {
      root.render(<Settings />);
    });
    const html = container.innerHTML;

    assert.match(html, /<h1[^>]*>設定<\/h1>/);
    assert.match(html, /外観/);
    assert.match(html, /カラーモード/);
    assert.match(html, /開発ツールを表示/);
    assert.match(html, /ランタイム Profile 設定/);
    assert.match(html, /aria-label="言語"/);
    const technicalDetails = container.querySelector<HTMLDetailsElement>(
      '[data-testid="runtime-settings-technical-details"]',
    );
    assert.ok(technicalDetails);
    assert.equal(technicalDetails.open, false);
    assert.match(technicalDetails.textContent ?? '', /技術的なランタイム詳細を表示/);
    assert.equal(translate('settings.appearance', undefined, 'zh'), 'Appearance');
  } finally {
    act(() => root.unmount());
    dom.window.close();
    useAppStore.setState(previousState, true);
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('Settings checks for Launcher updates and opens the native official release path', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  const commands: string[] = [];
  Object.defineProperty(dom.window, '__TAURI__', {
    configurable: true,
    value: {
      core: {
        invoke: async (command: string) => {
          commands.push(command);
          if (command === 'check_launcher_update') {
            return {
              state: 'available',
              current_version: '1.0.0',
              latest_version: '1.1.0',
            };
          }
          return undefined;
        },
      },
    },
  });
  try {
    useAppStore.setState({profile: {...previousState.profile, language: 'en'}});
    await act(async () => {
      root.render(<Settings />);
    });
    const checkButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('Check for updates'),
    );
    assert.ok(checkButton);
    await act(async () => {
      checkButton.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    assert.match(container.textContent ?? '', /Installed1\.0\.0/);
    assert.match(container.textContent ?? '', /Latest1\.1\.0/);
    assert.ok(container.querySelector('[data-status-icon="update-available"]'));
    assert.equal(container.querySelector('[data-status-icon="up-to-date"]'), null);
    const releaseButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('Open official release page'),
    );
    assert.ok(releaseButton);
    await act(async () => {
      releaseButton.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    assert.deepEqual(commands, ['check_launcher_update', 'open_launcher_update_release']);
  } finally {
    act(() => root.unmount());
    dom.window.close();
    useAppStore.setState(previousState, true);
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify({success: true, data}), {
    headers: {'Content-Type': 'application/json'},
  });
}

test('Settings update failures keep a severity icon and stable copy action', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const {dom, container, root} = createDom();
  Object.defineProperty(dom.window, '__TAURI__', {
    configurable: true,
    value: {
      core: {
        invoke: async (command: string) => {
          if (command === 'check_launcher_update') throw new Error('release channel unavailable');
          return undefined;
        },
      },
    },
  });
  try {
    useAppStore.setState({profile: {...previousState.profile, language: 'en'}});
    await act(async () => {
      root.render(<Settings />);
    });
    const checkButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('Check for updates'),
    );
    assert.ok(checkButton);
    await act(async () => {
      checkButton.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const errorIcon = container.querySelector('[data-error-icon="launcher-update"]');
    const alert = errorIcon?.closest('[role="alert"]');
    assert.ok(alert);
    assert.match(alert.textContent ?? '', /release channel unavailable/);
    assert.ok(errorIcon);
    assert.ok(alert.querySelector('button[aria-label="Copy Launcher update error"]'));
  } finally {
    act(() => root.unmount());
    dom.window.close();
    useAppStore.setState(previousState, true);
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});

test('Settings manages runtime target updates through the v4 host API', async () => {
  const previousState = useAppStore.getState();
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousNavigator = globalThis.navigator;
  const previousFetch = globalThis.fetch;
  const {dom, container, root} = createDom();
  const requests: Array<{method: string; path: string; body: unknown}> = [];
  const updateSettings = {
    auto_update: {tobkiri: false, defaultspack: true},
    check_interval_hours: 24,
    last_checked_at: null,
    last_results: [] as Array<Record<string, unknown>>,
    updated_at: null,
  };
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    requests.push({method, path: url.pathname, body});
    if (method === 'GET' && url.pathname === '/api/v4/updates/settings') {
      return jsonResponse(updateSettings);
    }
    if (method === 'GET' && url.pathname === '/api/v4/updates') {
      return jsonResponse({
        updates: [
          {
            target: 'tobkiri',
            current_version: '1.0.0',
            latest_version: '1.1.0',
            update_available: true,
            release_url: 'https://example.test/release',
            repo: 'tobkiri/tobkiri',
          },
          {
            target: 'defaultspack',
            current_version: '2.0.0',
            latest_version: '2.0.0',
            update_available: false,
            release_url: '',
            repo: 'tobkiri/tobkiri',
          },
        ],
      });
    }
    if (method === 'POST' && url.pathname === '/api/v4/updates/settings') {
      const autoUpdate = body?.auto_update as Record<string, boolean> | undefined;
      return jsonResponse({
        ...updateSettings,
        auto_update: {...updateSettings.auto_update, ...(autoUpdate ?? {})},
      });
    }
    if (method === 'POST' && url.pathname === '/api/v4/updates/apply') {
      return jsonResponse({
        target: 'tobkiri',
        current_version: '1.0.0',
        latest_version: '1.1.0',
        release_url: 'https://example.test/release',
        backup_dir: '/tmp/backup',
        applied_files: ['core_runtime/runtime.py'],
        skipped_files: [],
        applied_count: 1,
        skipped_count: 0,
        restart_required: true,
      });
    }
    return new Response(JSON.stringify({success: false, error: 'not found'}), {
      status: 404,
      headers: {'Content-Type': 'application/json'},
    });
  }) as typeof fetch;
  const flush = async () => {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  };
  try {
    useAppStore.setState({profile: {...previousState.profile, language: 'en'}});
    await act(async () => {
      root.render(<Settings />);
    });
    await flush();

    // Auto-update preferences load on mount; the heavier release check stays manual.
    assert.ok(requests.some((r) => r.method === 'GET' && r.path === '/api/v4/updates/settings'));
    assert.equal(requests.some((r) => r.path === '/api/v4/updates'), false);

    const checkButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('Check runtime updates'),
    );
    assert.ok(checkButton);
    await act(async () => {
      checkButton.click();
    });
    await flush();

    const tobkiriRow = container.querySelector<HTMLElement>('[data-update-target="tobkiri"]');
    const packRow = container.querySelector<HTMLElement>('[data-update-target="defaultspack"]');
    assert.ok(tobkiriRow);
    assert.ok(packRow);
    assert.match(tobkiriRow.textContent ?? '', /Tobkiri/);
    assert.match(tobkiriRow.textContent ?? '', /1\.0\.0/);
    assert.match(tobkiriRow.textContent ?? '', /1\.1\.0/);
    assert.match(tobkiriRow.textContent ?? '', /Available/);
    assert.match(packRow.textContent ?? '', /defaultspack/);
    assert.match(packRow.textContent ?? '', /Current/);

    // Per-target auto-update switch persists through the v4 settings route.
    const autoUpdateSwitch = tobkiriRow.querySelector<HTMLButtonElement>('[role="switch"]');
    assert.ok(autoUpdateSwitch);
    assert.equal(autoUpdateSwitch.getAttribute('aria-checked'), 'false');
    const packSwitch = packRow.querySelector<HTMLButtonElement>('[role="switch"]');
    assert.ok(packSwitch);
    assert.equal(packSwitch.getAttribute('aria-checked'), 'true');
    await act(async () => {
      autoUpdateSwitch.click();
    });
    await flush();
    const settingsPost = requests.find(
      (r) => r.method === 'POST' && r.path === '/api/v4/updates/settings',
    );
    assert.deepEqual(settingsPost?.body, {auto_update: {tobkiri: true}});

    // Apply is offered only for targets with an available update.
    const packApply = Array.from(packRow.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('Update'),
    ) as HTMLButtonElement | undefined;
    assert.ok(packApply);
    assert.equal(packApply.disabled, true);
    const applyButton = Array.from(tobkiriRow.querySelectorAll('button')).find(
      (button) => button.textContent?.includes('Update'),
    );
    assert.ok(applyButton);
    await act(async () => {
      applyButton.click();
    });
    await flush();
    const applyPost = requests.find(
      (r) => r.method === 'POST' && r.path === '/api/v4/updates/apply',
    );
    assert.deepEqual(applyPost?.body, {target: 'tobkiri'});
    const lastToast = useAppStore.getState().toasts.at(-1);
    assert.match(lastToast?.message ?? '', /Update applied/);
    assert.match(lastToast?.message ?? '', /Restart required/);
    assert.ok(
      requests.filter((r) => r.method === 'GET' && r.path === '/api/v4/updates').length >= 2,
    );
  } finally {
    act(() => root.unmount());
    dom.window.close();
    globalThis.fetch = previousFetch;
    useAppStore.setState(previousState, true);
    Object.defineProperties(globalThis, {
      window: {value: previousWindow, configurable: true},
      document: {value: previousDocument, configurable: true},
      navigator: {value: previousNavigator, configurable: true},
    });
  }
});
