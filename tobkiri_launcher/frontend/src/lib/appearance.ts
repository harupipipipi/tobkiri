export const VALID_THEMES = ['Rounded', 'Minimal'] as const;
export type Theme = (typeof VALID_THEMES)[number];

export const VALID_COLOR_MODES = ['light', 'dark'] as const;
export type ColorMode = (typeof VALID_COLOR_MODES)[number];

export const THEME_STORAGE_KEY = 'tobkiri-theme';
export const COLOR_MODE_STORAGE_KEY = 'tobkiri-color-mode';
export const LEGACY_THEME_STORAGE_KEY = 'rumi-theme';
export const LEGACY_COLOR_MODE_STORAGE_KEY = 'rumi-color-mode';

export interface Appearance {
  theme: Theme;
  colorMode: ColorMode;
}

// Remove legacy classes so an older Rumi/Standard selection cannot leave stale
// styling on the document after it migrates to Rounded.
export const THEME_CLASS_NAMES = ['theme-rumi', 'theme-minimal', 'theme-standard', 'theme-rounded'];

export function themeClassName(theme: Theme): string {
  return `theme-${theme.toLowerCase()}`;
}

export function normalizeTheme(value: unknown): Theme {
  return typeof value === 'string' && (VALID_THEMES as readonly string[]).includes(value)
    ? (value as Theme)
    : 'Rounded';
}

export function normalizeColorMode(value: unknown): ColorMode {
  return value === 'light' || value === 'dark' ? value : 'dark';
}

type AppearanceStorage = Pick<Storage, 'getItem'> & Partial<Pick<Storage, 'setItem'>>;

function readStorageValue(storage: AppearanceStorage | null | undefined, key: string): string | null {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function getBrowserStorage(): AppearanceStorage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function readValidatedMigratedValue<T extends string>(
  storage: AppearanceStorage | null | undefined,
  canonicalKey: string,
  legacyKey: string,
  isValid: (value: string) => value is T,
): T | null {
  const canonical = readStorageValue(storage, canonicalKey);
  if (canonical !== null) return isValid(canonical) ? canonical : null;
  const legacy = readStorageValue(storage, legacyKey);
  if (legacy === null || !isValid(legacy)) return null;
  try { storage?.setItem?.(canonicalKey, legacy); } catch { /* reads remain usable */ }
  return legacy;
}

export function readStoredAppearance(storage?: AppearanceStorage | null): Appearance {
  const effectiveStorage = storage === undefined ? getBrowserStorage() : storage;
  const canonicalTheme = readStorageValue(effectiveStorage, THEME_STORAGE_KEY);
  const legacyTheme = canonicalTheme === null
    ? readStorageValue(effectiveStorage, LEGACY_THEME_STORAGE_KEY)
    : null;
  const storedTheme = canonicalTheme ?? legacyTheme;
  const theme = normalizeTheme(storedTheme);
  if (storedTheme !== null && storedTheme !== theme) {
    try { effectiveStorage?.setItem?.(THEME_STORAGE_KEY, theme); } catch { /* reads remain usable */ }
  } else if (canonicalTheme === null && legacyTheme !== null) {
    try { effectiveStorage?.setItem?.(THEME_STORAGE_KEY, theme); } catch { /* reads remain usable */ }
  }
  return {
    theme,
    colorMode: normalizeColorMode(readValidatedMigratedValue(
      effectiveStorage,
      COLOR_MODE_STORAGE_KEY,
      LEGACY_COLOR_MODE_STORAGE_KEY,
      (value): value is ColorMode => value === 'light' || value === 'dark',
    )),
  };
}

export function applyAppearanceToRoot(root: Pick<HTMLElement, 'classList' | 'dataset' | 'style'>, appearance: Appearance): void {
  root.classList.remove(...THEME_CLASS_NAMES);
  root.classList.add(themeClassName(appearance.theme));
  root.classList.toggle('dark', appearance.colorMode === 'dark');
  root.dataset.theme = appearance.theme;
  root.dataset.colorMode = appearance.colorMode;
  root.style.colorScheme = appearance.colorMode;
  root.style.backgroundColor = '';
  root.style.color = '';
}

export function bootstrapDocumentAppearance(
  documentRef?: Pick<Document, 'documentElement'>,
  storage?: Pick<Storage, 'getItem'> | null,
): Appearance {
  const appearance = readStoredAppearance(storage);
  const currentDocument = documentRef ?? (typeof document === 'undefined' ? null : document);
  if (currentDocument) {
    applyAppearanceToRoot(currentDocument.documentElement, appearance);
  }
  return appearance;
}
