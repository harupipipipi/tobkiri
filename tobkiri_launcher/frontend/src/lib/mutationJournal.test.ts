import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
  beginMutation,
  completeMutation,
  isLegacyAdoptedMutation,
  listMutationJournal,
  markMutationUnknown,
  MutationBlockedError,
  mutationRequestId,
} from './mutationJournal.ts';

test('a logical mutation retains its identity and blocks a second submit while unknown', () => {
  const key = `test:mutation:${Date.now()}:${Math.random()}`;
  const record = beginMutation(key, {kind: 'test'}, {
    primary: '22222222-2222-4222-8222-222222222222',
    retry: '33333333-3333-4333-8333-333333333333',
  });
  assert.equal(mutationRequestId(record), '22222222-2222-4222-8222-222222222222');
  assert.equal(mutationRequestId(record, 'retry'), '33333333-3333-4333-8333-333333333333');

  const unknown = markMutationUnknown(key, record.requestId);
  assert.equal(unknown.state, 'unknown');
  assert.throws(() => beginMutation(key), (error: unknown) => (
    error instanceof MutationBlockedError && error.journalState === 'unknown'
  ));
  assert.equal(listMutationJournal().some((item) => item.key === key), true);
  completeMutation(key, record.requestId);
  assert.equal(listMutationJournal().some((item) => item.key === key), false);
});

test('a pending request becomes unknown in a fresh storage context', () => {
  const previousStorage = (globalThis as typeof globalThis & {localStorage?: unknown}).localStorage;
  const firstValues = new Map<string, string>();
  const makeStorage = (values: Map<string, string>) => ({
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  });
  const key = `test:restart:${Date.now()}:${Math.random()}`;
  try {
    Object.defineProperty(globalThis, 'localStorage', {
      value: makeStorage(firstValues),
      configurable: true,
    });
    const record = beginMutation(key, {kind: 'restart-test'});
    assert.equal(listMutationJournal().find((item) => item.key === key)?.state, 'pending');

    const restartedValues = new Map(firstValues);
    Object.defineProperty(globalThis, 'localStorage', {
      value: makeStorage(restartedValues),
      configurable: true,
    });
    const hydrated = listMutationJournal().find((item) => item.key === key);
    assert.equal(hydrated?.state, 'unknown');
    assert.throws(() => beginMutation(key), /result is unknown/);
    completeMutation(key, record.requestId);
  } finally {
    Object.defineProperty(globalThis, 'localStorage', {
      value: previousStorage,
      configurable: true,
    });
  }
});

test('authenticated app-data scopes isolate mutation journals on one browser origin', () => {
  const globals = globalThis as typeof globalThis & {
    localStorage?: unknown;
    sessionStorage?: unknown;
  };
  const previousLocalStorage = globals.localStorage;
  const previousSessionStorage = globals.sessionStorage;
  const localValues = new Map<string, string>();
  const sessionValues = new Map<string, string>();
  const makeStorage = (values: Map<string, string>) => ({
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  });
  const scopeA = `sha256:${'a'.repeat(64)}`;
  const scopeB = `sha256:${'b'.repeat(64)}`;
  const key = `test:scope:${Date.now()}:${Math.random()}`;
  try {
    Object.defineProperty(globalThis, 'localStorage', {
      value: makeStorage(localValues), configurable: true,
    });
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: makeStorage(sessionValues), configurable: true,
    });
    sessionValues.set('tobkiri-panel-journal-scope-v1', scopeA);
    beginMutation(key, {}, {primary: '44444444-4444-4444-8444-444444444444'});
    assert.equal(listMutationJournal().some((item) => item.key === key), true);

    sessionValues.set('tobkiri-panel-journal-scope-v1', scopeB);
    assert.equal(listMutationJournal().some((item) => item.key === key), false);

    sessionValues.set('tobkiri-panel-journal-scope-v1', scopeA);
    assert.equal(listMutationJournal().find((item) => item.key === key)?.state, 'unknown');
  } finally {
    Object.defineProperty(globalThis, 'localStorage', {
      value: previousLocalStorage, configurable: true,
    });
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: previousSessionStorage, configurable: true,
    });
  }
});

test('a legacy unknown is adopted once without being discarded or leaked', () => {
  const globals = globalThis as typeof globalThis & {
    localStorage?: unknown;
    sessionStorage?: unknown;
  };
  const previousLocalStorage = globals.localStorage;
  const previousSessionStorage = globals.sessionStorage;
  const localValues = new Map<string, string>();
  const sessionValues = new Map<string, string>();
  const makeStorage = (values: Map<string, string>) => ({
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  });
  const key = `test:legacy:${Date.now()}:${Math.random()}`;
  const requestId = '55555555-5555-4555-8555-555555555555';
  try {
    localValues.set('tobkiri-launcher-mutation-journal-v1', JSON.stringify([{
      key, requestId, state: 'unknown', createdAt: Date.now(), metadata: {kind: 'test'},
    }]));
    Object.defineProperty(globalThis, 'localStorage', {
      value: makeStorage(localValues), configurable: true,
    });
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: makeStorage(sessionValues), configurable: true,
    });
    sessionValues.set('tobkiri-panel-journal-scope-v1', `sha256:${'c'.repeat(64)}`);
    assert.throws(() => beginMutation(key), MutationBlockedError);
    const adopted = listMutationJournal().find((item) => item.key === key);
    assert.ok(adopted);
    assert.equal(isLegacyAdoptedMutation(adopted), true);
    assert.equal(adopted.metadata.journal_migration, 'legacy-unscoped-v1');
    assert.equal(localValues.get('tobkiri-launcher-mutation-journal-v1'), '[]');

    sessionValues.set('tobkiri-panel-journal-scope-v1', `sha256:${'d'.repeat(64)}`);
    assert.equal(listMutationJournal().some((item) => item.key === key), false);
  } finally {
    Object.defineProperty(globalThis, 'localStorage', {
      value: previousLocalStorage, configurable: true,
    });
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: previousSessionStorage, configurable: true,
    });
  }
});
