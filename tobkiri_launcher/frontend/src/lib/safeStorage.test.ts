import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clearClientDiagnostics,
  listClientDiagnostics,
} from './clientDiagnostics';
import {
  readSafeStorageValue,
  removeSafeStorageValue,
  writeSafeStorageValue,
  type SafeStorage,
} from './safeStorage';

function storage(initial: Record<string, string> = {}): SafeStorage {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value); },
    removeItem: (key) => { values.delete(key); },
  };
}

test('safe storage wrapper preserves values and returns explicit write status', () => {
  const target = storage();
  assert.equal(writeSafeStorageValue(target, 'launcher.theme', 'Rounded'), true);
  assert.equal(readSafeStorageValue(target, 'launcher.theme'), 'Rounded');
  assert.equal(removeSafeStorageValue(target, 'launcher.theme'), true);
  assert.equal(readSafeStorageValue(target, 'launcher.theme'), null);
});

test('safe storage wrapper records typed diagnostics when a browser area rejects access', () => {
  clearClientDiagnostics();
  const target: SafeStorage = {
    getItem: () => { throw new Error('storage denied'); },
    setItem: () => { throw new Error('storage denied'); },
    removeItem: () => { throw new Error('storage denied'); },
  };

  assert.equal(readSafeStorageValue(target, 'launcher.theme'), null);
  assert.equal(writeSafeStorageValue(target, 'launcher.theme', 'Rounded'), false);
  assert.equal(removeSafeStorageValue(target, 'launcher.theme'), false);
  assert.deepEqual(
    listClientDiagnostics().map((diagnostic) => diagnostic.code),
    ['storage.remove_failed', 'storage.write_failed', 'storage.read_failed'],
  );
  clearClientDiagnostics();
});
