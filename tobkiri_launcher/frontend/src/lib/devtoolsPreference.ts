export const DEVTOOLS_PREFERENCE_STORAGE_KEY = 'tobkiri-launcher-devtools-enabled';

/**
 * Devtools visibility is a Launcher-local presentation preference.
 * It must never be inferred from runtime Profile or authority state.
 */
export function normalizeDevtoolsEnabled(value: string | null): boolean {
  return value === 'true';
}
