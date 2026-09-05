import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';

import { RUMI_VIEWER_VERSION, ViewerVersionLabel } from './ViewerVersionLabel';

const frontendRoot = resolve(import.meta.dirname, '..', '..', '..');
const viewerRoot = resolve(frontendRoot, '..');

test('Home version label renders the package version as non-interactive text', () => {
  const markup = renderToStaticMarkup(<ViewerVersionLabel />);

  assert.match(markup, new RegExp(`Tobkiri Launcher v${RUMI_VIEWER_VERSION.replaceAll('.', '\\.')}`));
  assert.match(markup, /pointer-events-none/);
  assert.match(markup, /select-none/);
  assert.match(markup, /text-\[10px\]/);
  assert.match(markup, /opacity-45/);
  assert.doesNotMatch(markup, /<(?:a|button)\b/);
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

  assert.equal(RUMI_VIEWER_VERSION, packageMetadata.version);
  assert.equal(packageLock.version, packageMetadata.version);
  assert.equal(packageLock.packages[''].version, packageMetadata.version);
  assert.equal(packageMetadata.engines.node, '>=22.22.0');
  assert.equal(packageLock.packages[''].engines.node, packageMetadata.engines.node);
  assert.equal(tauriConfig.version, packageMetadata.version);
  assert.equal(cargoVersion, packageMetadata.version);
  assert.equal(cargoLockVersion, packageMetadata.version);
});
