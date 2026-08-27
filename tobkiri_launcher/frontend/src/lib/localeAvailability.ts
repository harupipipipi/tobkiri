export type UiLocale = 'en' | 'ja';

export type LocaleAvailability = 'general' | 'preview';

export type UiLocaleOption = {
  id: UiLocale;
  nativeLabel: string;
  availability: LocaleAvailability;
  direction: 'ltr' | 'rtl';
  surfaceCatalogComplete: boolean;
};

export const GENERAL_LOCALE_COVERAGE_THRESHOLD = 1;

export const UI_LOCALE_OPTIONS: readonly UiLocaleOption[] = [
  {
    id: 'en',
    nativeLabel: 'English',
    availability: 'general',
    direction: 'ltr',
    surfaceCatalogComplete: true,
  },
  {
    id: 'ja',
    nativeLabel: '日本語',
    availability: 'preview',
    direction: 'ltr',
    surfaceCatalogComplete: false,
  },
];

const UI_LOCALE_IDS = new Set<string>(UI_LOCALE_OPTIONS.map((option) => option.id));

export function isUiLocale(value: unknown): value is UiLocale {
  return typeof value === 'string' && UI_LOCALE_IDS.has(value);
}

export function resolveUiLocale(value: unknown): UiLocale {
  return isUiLocale(value) ? value : 'en';
}

export function uiLocaleOption(value: unknown): UiLocaleOption {
  const locale = resolveUiLocale(value);
  return UI_LOCALE_OPTIONS.find((option) => option.id === locale) ?? UI_LOCALE_OPTIONS[0];
}

export function applyLocaleToRoot(root: HTMLElement, value: unknown): void {
  const option = uiLocaleOption(value);
  root.lang = option.id;
  root.dir = option.direction;
}
