import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import {JSDOM} from 'jsdom';
import {MemoryRouter} from 'react-router';

import {Layout} from './Layout';
import {ViewerVersionLabel} from './ViewerVersionLabel';
import {
  LAUNCHER_VERSION,
  LAUNCHER_VERSION_ACCESSIBLE_LABEL,
  LAUNCHER_VERSION_LABEL,
} from '@/src/lib/launcherMetadata';
import {useAppStore} from '@/src/store';

const frontendRoot = resolve(import.meta.dirname, '..', '..', '..');
const viewerRoot = resolve(frontendRoot, '..');

test('Home version label renders the package version as non-interactive text', () => {
  const markup = renderToStaticMarkup(<ViewerVersionLabel />);

  assert.match(markup, new RegExp(LAUNCHER_VERSION_LABEL.replaceAll('.', '\\.')));
  assert.match(markup, new RegExp(`aria-label="${LAUNCHER_VERSION_ACCESSIBLE_LABEL}"`));
  assert.match(markup, /pointer-events-none/);
  assert.match(markup, /select-none/);
  assert.match(markup, /text-\[10px\]/);
  assert.match(markup, /opacity-45/);
  assert.match(markup, /text-text-muted/);
  assert.match(markup, /overflow-hidden/);
  assert.match(markup, /text-ellipsis/);
  assert.match(markup, /whitespace-nowrap/);
  assert.doesNotMatch(markup, /<(?:a|button)\b/);
});

test('Launcher layout shows the version label on Home only', {concurrency: false}, async () => {
  const previousState = useAppStore.getState();
  const previousGlobals = {
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
    document: Object.getOwnPropertyDescriptor(globalThis, 'document'),
    navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator'),
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    reactAct: Object.getOwnPropertyDescriptor(globalThis, 'IS_REACT_ACT_ENVIRONMENT'),
  };
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/panel/',
  });
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    localStorage: {value: dom.window.localStorage, configurable: true},
    IS_REACT_ACT_ENVIRONMENT: {value: true, configurable: true},
  });
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root = createRoot(container);

  try {
    useAppStore.setState({
      isSetupDone: true,
      runtimeReady: true,
      runtimeStatus: 'runtime_ready',
    });
    const renderAt = async (path: string) => {
      await act(async () => {
        root.render(
          <MemoryRouter key={path} initialEntries={[path]}>
            <Layout />
          </MemoryRouter>,
        );
      });
    };

    await renderAt('/');
    assert.equal(
      container.querySelector(`[aria-label="${LAUNCHER_VERSION_ACCESSIBLE_LABEL}"]`)?.textContent,
      LAUNCHER_VERSION_LABEL,
    );

    await renderAt('/packs');
    assert.equal(
      container.querySelector(`[aria-label="${LAUNCHER_VERSION_ACCESSIBLE_LABEL}"]`),
      null,
    );
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState(previousState, true);
    dom.window.close();
    for (const [key, descriptor] of Object.entries({
      window: previousGlobals.window,
      document: previousGlobals.document,
      navigator: previousGlobals.navigator,
      localStorage: previousGlobals.localStorage,
      IS_REACT_ACT_ENVIRONMENT: previousGlobals.reactAct,
    })) {
      if (descriptor) {
        Object.defineProperty(globalThis, key, descriptor);
      } else {
        Reflect.deleteProperty(globalThis, key);
      }
    }
  }
});

test('viewer package, Tauri, and Cargo versions stay aligned', () => {
  const packageMetadata = JSON.parse(readFileSync(resolve(frontendRoot, 'package.json'), 'utf8'));
  const packageLock = JSON.parse(readFileSync(resolve(frontendRoot, 'package-lock.json'), 'utf8'));
  const tauriConfig = JSON.parse(readFileSync(resolve(viewerRoot, 'src-tauri', 'tauri.conf.json'), 'utf8'));
  const cargoManifest = readFileSync(resolve(viewerRoot, 'src-tauri', 'Cargo.toml'), 'utf8');
  const cargoLock = readFileSync(resolve(viewerRoot, 'src-tauri', 'Cargo.lock'), 'utf8').replaceAll('\r\n', '\n');
  const cargoVersion = cargoManifest.match(/^version = "([^"]+)"$/m)?.[1];
  const cargoLockVersion = cargoLock.match(
    /\[\[package\]\]\nname = "tobkiri-launcher"\nversion = "([^"]+)"/,
  )?.[1];

  assert.equal(LAUNCHER_VERSION, packageMetadata.version);
  assert.equal(packageLock.version, packageMetadata.version);
  assert.equal(packageLock.packages[''].version, packageMetadata.version);
  assert.equal(packageMetadata.engines.node, '>=22.22.0');
  assert.equal(packageLock.packages[''].engines.node, packageMetadata.engines.node);
  assert.equal(tauriConfig.version, packageMetadata.version);
  assert.equal(cargoVersion, packageMetadata.version);
  assert.equal(cargoLockVersion, packageMetadata.version);
});
