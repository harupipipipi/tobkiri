import {
  getBrowserStorage,
  readSafeStorageValue,
  removeSafeStorageValue,
  writeSafeStorageValue,
} from './safeStorage';

const PANEL_JOURNAL_SCOPE_STORAGE_KEY = 'tobkiri-panel-journal-scope-v1';
const JOURNAL_SCOPE_PATTERN = /^sha256:[0-9a-f]{64}$/;

/** Return the Host-derived app-data scope for durable browser journals. */
export function getPanelJournalScope(): string {
  const value = readSafeStorageValue(
    getBrowserStorage('session'),
    PANEL_JOURNAL_SCOPE_STORAGE_KEY,
  );
  return value && JOURNAL_SCOPE_PATTERN.test(value) ? value : '';
}

/** Replace the current journal scope after an authenticated panel exchange. */
export function setPanelJournalScope(scope: string): void {
  const storage = getBrowserStorage('session');
  if (!JOURNAL_SCOPE_PATTERN.test(scope)) {
    removeSafeStorageValue(storage, PANEL_JOURNAL_SCOPE_STORAGE_KEY);
    return;
  }
  writeSafeStorageValue(storage, PANEL_JOURNAL_SCOPE_STORAGE_KEY, scope);
}
