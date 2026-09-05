import {recordClientDiagnostic} from './clientDiagnostics';

export type BrowserStorageKind = 'local' | 'session';
export type SafeStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/** Return a browser storage area when it is available and callable. */
export function getBrowserStorage(kind: BrowserStorageKind): Storage | null {
  try {
    const candidate = kind === 'local' ? globalThis.localStorage : globalThis.sessionStorage;
    if (
      !candidate
      || typeof candidate.getItem !== 'function'
      || typeof candidate.setItem !== 'function'
      || typeof candidate.removeItem !== 'function'
    ) return null;
    return candidate;
  } catch (error) {
    recordClientDiagnostic({
      code: 'storage.access_failed',
      operation: `browser.${kind}`,
      error,
    });
    return null;
  }
}

/** Read a value without allowing unavailable or denied storage to break the UI. */
export function readSafeStorageValue(storage: SafeStorage | null, key: string): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(key);
  } catch (error) {
    recordClientDiagnostic({code: 'storage.read_failed', operation: key, error});
    return null;
  }
}

/** Write a value and report whether the browser accepted the write. */
export function writeSafeStorageValue(
  storage: SafeStorage | null,
  key: string,
  value: string,
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch (error) {
    recordClientDiagnostic({code: 'storage.write_failed', operation: key, error});
    return false;
  }
}

/** Remove a value without allowing storage failures to escape into the UI. */
export function removeSafeStorageValue(storage: SafeStorage | null, key: string): boolean {
  if (!storage) return false;
  try {
    storage.removeItem(key);
    return true;
  } catch (error) {
    recordClientDiagnostic({code: 'storage.remove_failed', operation: key, error});
    return false;
  }
}
