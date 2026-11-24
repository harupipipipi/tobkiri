import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';

const srcRoot = resolve(import.meta.dirname, '..');

function source(path: string): string {
  return readFileSync(resolve(srcRoot, path), 'utf8');
}

test('viewer popover trigger exposes keyboard and menu semantics', () => {
  const popover = source('components/ui/Popover.tsx');

  assert.match(popover, /aria-haspopup=\{props\['aria-haspopup'\] \?\? 'menu'\}/);
  assert.match(popover, /event\.key === "Escape"/);
  assert.match(popover, /pointerdown/);
  assert.match(popover, /firstFocusable\?\.focus\(\)/);
  assert.match(popover, /triggerRef\.current\?\.focus\(\)/);
  assert.match(popover, /createPortal/);
  assert.match(popover, /document\.body/);
  assert.match(popover, /position: "fixed"/);
  assert.match(popover, /onClose\?\.\(\)/);
});

test('viewer shell has a mobile navigation fallback and persistent desktop sidebar state', () => {
  const sidebar = source('components/layout/Sidebar.tsx');
  const header = source('components/layout/Header.tsx');
  const store = source('store.ts');

  assert.match(sidebar, /hidden[\s\S]*md:flex/);
  assert.match(header, /aria-label=\{t\('nav.open_menu'\)\}/);
  assert.match(header, /aria-haspopup="dialog"/);
  assert.match(header, /role="dialog"/);
  assert.doesNotMatch(header, /aria-haspopup="menu"/);
  assert.doesNotMatch(header, /role="menu(item)?"/);
  assert.match(header, /aria-label="Profile menu"/);
  assert.match(header, /viewerNavGroups\(devtoolsEnabled\)\.map/);
  assert.match(header, /aria-label=\{t\('nav.mobile_navigation'\)\}/);
  assert.match(store, /SIDEBAR_STORAGE_KEY = 'tobkiri-launcher-sidebar-open'/);
  assert.match(
    store,
    /readLocalStorage\(SIDEBAR_STORAGE_KEY\) \?\? readLocalStorage\(LEGACY_SIDEBAR_STORAGE_KEY\)/,
  );
  assert.match(store, /writeLocalStorage\(SIDEBAR_STORAGE_KEY, String\(open\)\)/);
  assert.match(store, /SETUP_STORAGE_KEY = 'tobkiri-launcher-setup'/);
  assert.match(
    store,
    /readLocalStorage\(SETUP_STORAGE_KEY\) \?\? readLocalStorage\(LEGACY_SETUP_STORAGE_KEY\)/,
  );
  assert.match(store, /writeLocalStorage\(SETUP_STORAGE_KEY, String\(done\)\)/);
});

test('viewer async dialogs keep the modal open while confirm is pending', () => {
  const dialog = source('components/ui/DialogContainer.tsx');
  const store = source('store.ts');

  assert.match(store, /onConfirm: \(\) => void \| Promise<void>/);
  assert.match(dialog, /await dialog\.onConfirm\(\)/);
  assert.match(dialog, /loading=\{isConfirming\}/);
  assert.match(dialog, /disabled=\{isConfirming\}/);
  assert.match(dialog, /if \(!isConfirming\)[\s\S]*closeDialog\(\)/);
});

test('viewer overlays use shared layer tokens instead of competing z-50 classes', () => {
  const layers = source('lib/layers.ts');
  const toast = source('components/ui/ToastContainer.tsx');
  const dialog = source('components/ui/DialogContainer.tsx');
  const popover = source('components/ui/Popover.tsx');
  const loader = source('components/ui/TobkiriLoader.tsx');

  assert.match(layers, /loading: 'z-\[50\]'/);
  assert.match(layers, /popover: 'z-\[60\]'/);
  assert.match(layers, /dialog: 'z-\[70\]'/);
  assert.match(layers, /toast: 'z-\[80\]'/);
  assert.match(toast, /viewerLayers\.toast/);
  assert.match(dialog, /viewerLayers\.dialog/);
  assert.match(popover, /viewerLayers\.popover/);
  assert.match(loader, /viewerLayers\.loading/);
  assert.doesNotMatch(toast, /z-50/);
  assert.doesNotMatch(dialog, /z-50/);
  assert.doesNotMatch(popover, /z-50/);
});
