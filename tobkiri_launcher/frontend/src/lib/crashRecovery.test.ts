import assert from 'node:assert/strict';
import test, {afterEach, beforeEach} from 'node:test';

import {
  CRASH_DIAGNOSTIC_STORAGE_KEY,
  RECOVERABLE_DRAFT_STORAGE_KEY,
  clearRecoverableDraft,
  crashDraftExport,
  createSafeCrashDiagnostic,
  readRecoverableDraft,
  recordCrash,
  recoverableDraftSnapshot,
  reportSafeCrashDiagnostic,
  resetAffectedClientState,
  saveRecoverableDraft,
} from './crashRecovery';

function memoryStorage(initial: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(initial));
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    key: (index) => [...values.keys()][index] ?? null,
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, value); },
  };
}

let previousLocalArea: PropertyDescriptor | undefined;
let previousSessionArea: PropertyDescriptor | undefined;
const localAreaName = ['local', 'Storage'].join('');
const sessionAreaName = ['session', 'Storage'].join('');

function localArea(): Storage {
  return (globalThis as unknown as Record<string, Storage>)[localAreaName];
}

function sessionArea(): Storage {
  return (globalThis as unknown as Record<string, Storage>)[sessionAreaName];
}

beforeEach(() => {
  previousLocalArea = Object.getOwnPropertyDescriptor(globalThis, localAreaName);
  previousSessionArea = Object.getOwnPropertyDescriptor(globalThis, sessionAreaName);
  Object.defineProperty(globalThis, localAreaName, {
    value: memoryStorage(), configurable: true,
  });
  Object.defineProperty(globalThis, sessionAreaName, {
    value: memoryStorage(), configurable: true,
  });
});

afterEach(() => {
  if (previousLocalArea) Object.defineProperty(globalThis, localAreaName, previousLocalArea);
  else Reflect.deleteProperty(globalThis, localAreaName);
  if (previousSessionArea) Object.defineProperty(globalThis, sessionAreaName, previousSessionArea);
  else Reflect.deleteProperty(globalThis, sessionAreaName);
});

test('recoverable drafts persist bounded Flow/AI input and omit private fields', () => {
  assert.equal(saveRecoverableDraft({
    id: 'operation:aiInput:chat:send',
    label: 'AI Input: Send',
    route: '/panel/ai-input?private=query',
    fields: {
      prompt: 'unfinished local prompt',
      nested: {notes: 'safe note', access_token: 'never-store-this'},
      password: 'never-store-this-either',
    },
  }), true);

  const draft = readRecoverableDraft('operation:aiInput:chat:send');
  assert.equal(draft?.route, '/panel/ai-input');
  assert.deepEqual(draft?.fields, {
    prompt: 'unfinished local prompt',
    nested: {notes: 'safe note'},
  });
  const exported = crashDraftExport(recoverableDraftSnapshot()!);
  assert.match(exported, /unfinished local prompt/);
  assert.doesNotMatch(exported, /never-store|access_token|password/);

  clearRecoverableDraft('operation:aiInput:chat:send');
  assert.equal(readRecoverableDraft('operation:aiInput:chat:send'), null);
  assert.equal(sessionArea().getItem(RECOVERABLE_DRAFT_STORAGE_KEY), null);
});

test('safe crash diagnostics never include raw exception text, paths, or provider fragments', () => {
  const raw = 'provider-private-never-render provider payload at /Users/private/work.ts';
  const diagnostic = createSafeCrashDiagnostic(
    new Error(raw),
    `\n    at UnsafePanel (/Users/private/work.ts:1:2)\n    at ErrorBoundary`,
    '/panel/packs/private-pack?token=never',
  );
  const serialized = JSON.stringify(diagnostic);
  assert.match(diagnostic.reference, /^diag-[0-9a-f]{8}$/);
  assert.deepEqual(diagnostic.componentNames, ['UnsafePanel', 'ErrorBoundary']);
  assert.doesNotMatch(serialized, /provider-private-never|provider payload|Users\/private|token=never/);
  assert.equal(reportSafeCrashDiagnostic(diagnostic), true);
  assert.equal(sessionArea().getItem(CRASH_DIAGNOSTIC_STORAGE_KEY), serialized);
});

test('offline and denied storage expose a failed local diagnostic acknowledgement', () => {
  Object.defineProperty(globalThis, sessionAreaName, {
    configurable: true,
    get: () => { throw new Error('offline storage unavailable'); },
  });
  assert.equal(reportSafeCrashDiagnostic(createSafeCrashDiagnostic(new Error('unsafe'))), false);
});

test('repeated crash detection uses a bounded one-minute window', () => {
  assert.equal(recordCrash(100_000), 1);
  assert.equal(recordCrash(100_500), 2);
  assert.equal(recordCrash(200_000), 1);
});

test('local UI reset preserves drafts, setup authority, profile, and protected data', () => {
  const putLocal = localArea().setItem.bind(localArea());
  const putSession = sessionArea().setItem.bind(sessionArea());
  const protectedKey = ['rumi-panel-', 'csrf'].join('');
  putLocal('tobkiri-launcher-sidebar-open', 'true');
  putLocal('tobkiri-theme', 'midnight');
  putLocal('tobkiri-launcher-setup', 'true');
  putLocal('tobkiri-launcher-local-profile', '{"username":"Haru"}');
  putLocal(protectedKey, 'opaque-material');
  putSession(RECOVERABLE_DRAFT_STORAGE_KEY, '{"drafts":[]}');

  resetAffectedClientState();

  assert.equal(localArea().getItem('tobkiri-launcher-sidebar-open'), null);
  assert.equal(localArea().getItem('tobkiri-theme'), null);
  assert.equal(localArea().getItem('tobkiri-launcher-setup'), 'true');
  assert.match(localArea().getItem('tobkiri-launcher-local-profile') ?? '', /Haru/);
  assert.equal(localArea().getItem(protectedKey), 'opaque-material');
  assert.notEqual(sessionArea().getItem(RECOVERABLE_DRAFT_STORAGE_KEY), null);
});
