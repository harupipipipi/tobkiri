import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
  beginMutation,
  completeMutation,
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
