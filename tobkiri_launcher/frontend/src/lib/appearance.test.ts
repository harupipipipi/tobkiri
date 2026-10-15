import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  applyAppearanceToRoot,
  normalizeColorMode,
  normalizeTheme,
  readStoredAppearance,
} from './appearance';

function fakeStorage(values: Record<string, string> = {}, options: { throwGet?: boolean; throwSet?: boolean } = {}) {
  const data = new Map(Object.entries(values));
  return {
    data,
    storage: {
      getItem: (key: string) => {
        if (options.throwGet) throw new Error('get blocked');
        return data.get(key) ?? null;
      },
      setItem: (key: string, value: string) => {
        if (options.throwSet) throw new Error('set blocked');
        data.set(key, value);
      },
    },
  };
}

function fakeRoot() {
  const classes = new Set<string>();
  const classList = {
    add: (...names: string[]) => {
      names.forEach((name) => classes.add(name));
    },
    remove: (...names: string[]) => {
      names.forEach((name) => classes.delete(name));
    },
    toggle: (name: string, force?: boolean) => {
      const shouldAdd = force ?? !classes.has(name);
      if (shouldAdd) {
        classes.add(name);
      } else {
        classes.delete(name);
      }
      return shouldAdd;
    },
  };

  return {
    classes,
    element: {
      classList,
      dataset: {},
      style: {},
    } as unknown as HTMLElement,
  };
}

test('appearance normalization falls back to the startup-safe defaults', () => {
  assert.equal(normalizeTheme('Rounded'), 'Rounded');
  assert.equal(normalizeTheme('Rumi'), 'Rounded');
  assert.equal(normalizeTheme('Standard'), 'Rounded');
  assert.equal(normalizeTheme('Unknown'), 'Rounded');
  assert.equal(normalizeColorMode('light'), 'light');
  assert.equal(normalizeColorMode('sepia'), 'dark');
});

test('canonical appearance keys win when canonical and legacy values differ', () => {
  const { storage } = fakeStorage({
    'tobkiri-theme': 'Rounded', 'rumi-theme': 'Minimal',
    'tobkiri-color-mode': 'dark', 'rumi-color-mode': 'light',
  });
  assert.deepEqual(readStoredAppearance(storage), { theme: 'Rounded', colorMode: 'dark' });
});

test('legacy appearance keys migrate without deletion and remain idempotent', () => {
  const { storage, data } = fakeStorage({ 'rumi-theme': 'Minimal', 'rumi-color-mode': 'light' });
  assert.deepEqual(readStoredAppearance(storage), { theme: 'Minimal', colorMode: 'light' });
  assert.equal(data.get('tobkiri-theme'), 'Minimal');
  assert.equal(data.get('tobkiri-color-mode'), 'light');
  assert.equal(data.get('rumi-theme'), 'Minimal');
  assert.deepEqual(readStoredAppearance(storage), { theme: 'Minimal', colorMode: 'light' });
});

test('malformed legacy appearance values are not copied', () => {
  const { storage, data } = fakeStorage({ 'rumi-theme': 'Bogus', 'rumi-color-mode': 'sepia' });
  assert.deepEqual(readStoredAppearance(storage), { theme: 'Rounded', colorMode: 'dark' });
  assert.equal(data.get('tobkiri-theme'), 'Rounded');
  assert.equal(data.has('tobkiri-color-mode'), false);
});

test('appearance migration falls back safely when storage access throws', () => {
  assert.deepEqual(readStoredAppearance(fakeStorage({}, { throwGet: true }).storage), { theme: 'Rounded', colorMode: 'dark' });
  const throwingSet = fakeStorage({ 'rumi-theme': 'Minimal', 'rumi-color-mode': 'light' }, { throwSet: true });
  assert.deepEqual(readStoredAppearance(throwingSet.storage), { theme: 'Minimal', colorMode: 'light' });
  assert.equal(throwingSet.data.has('tobkiri-theme'), false);
});

test('preboot appearance migration uses the same canonical-first contract', () => {
  const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
  assert.ok(html.indexOf("readStoredValue('tobkiri-theme')") < html.indexOf("readStoredValue('rumi-theme')"));
  assert.ok(html.indexOf("readStoredValue('tobkiri-color-mode')") < html.indexOf("readStoredValue('rumi-color-mode')"));
  assert.match(html, /normalizeTheme\(storedTheme\)[\s\S]*setItem\('tobkiri-theme'/);
  assert.match(html, /legacyMode === 'light' \|\| legacyMode === 'dark'[\s\S]*setItem\('tobkiri-color-mode'/);
});

test('preboot appearance reads each storage key with its own fallback', () => {
  const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
  // One blocked getItem must not reset the other appearance value: every
  // read goes through the per-key helper, the same per-key isolation
  // readStoredAppearance gets from readStorageValue.
  assert.equal(html.match(/localStorage\.getItem\(/g)?.length, 1);
  assert.match(
    html,
    /function readStoredValue\(key\) \{\s*try \{\s*return localStorage\.getItem\(key\);\s*\} catch \(error\) \{\s*return null;\s*\}\s*\}/,
  );
});

test('preboot appearance script stays identical in the packaged panel copy', () => {
  const preboot = /\(function \(\) \{[\s\S]*?\}\)\(\);/;
  const launcher = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
  const panel = readFileSync(
    new URL(
      '../../../../tobkiri_runtime/core_runtime/core_pack/core_control_panel/web/index.html',
      import.meta.url,
    ),
    'utf8',
  );
  const launcherScript = launcher.match(preboot)?.[0];
  assert.ok(launcherScript);
  assert.equal(panel.match(preboot)?.[0], launcherScript);
});

test('appearance application keeps one theme class and toggles dark before paint', () => {
  const root = fakeRoot();
  root.classes.add('theme-rumi');
  root.classes.add('dark');

  applyAppearanceToRoot(root.element, { theme: 'Rounded', colorMode: 'light' });

  assert.deepEqual([...root.classes].sort(), ['theme-rounded']);
  assert.equal(root.element.dataset.theme, 'Rounded');
  assert.equal(root.element.dataset.colorMode, 'light');
  assert.equal(root.element.style.colorScheme, 'light');
});

test('appearance application clears preboot inline colors after React takes over', () => {
  const root = fakeRoot();
  root.element.style.backgroundColor = '#ffffff';
  root.element.style.color = '#111827';

  applyAppearanceToRoot(root.element, { theme: 'Rounded', colorMode: 'dark' });

  assert.equal(root.element.style.backgroundColor, '');
  assert.equal(root.element.style.color, '');
  assert.equal(root.element.style.colorScheme, 'dark');
});
