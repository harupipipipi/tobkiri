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
