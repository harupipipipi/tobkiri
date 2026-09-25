import {
  getBrowserStorage,
  readSafeStorageValue,
  removeSafeStorageValue,
  writeSafeStorageValue,
  type SafeStorage,
} from './safeStorage';

export const RECOVERABLE_DRAFT_STORAGE_KEY = 'tobkiri-launcher-recoverable-drafts-v1';
export const CRASH_HISTORY_STORAGE_KEY = 'tobkiri-launcher-crash-history-v1';
export const CRASH_DIAGNOSTIC_STORAGE_KEY = 'tobkiri-launcher-crash-diagnostic-v1';

const CRASH_WINDOW_MS = 60_000;
const MAX_DRAFTS = 8;
const MAX_FIELDS = 40;
const MAX_FIELD_LENGTH = 20_000;
const MAX_SERIALIZED_LENGTH = 100_000;
const SENSITIVE_FIELD = /(?:api.?key|auth|cookie|credential|password|secret|session|token)/i;

export interface RecoverableDraft {
  id: string;
  label: string;
  route: string;
  updatedAt: string;
  fields: Record<string, unknown>;
}

export interface CrashDraftSnapshot {
  schema: 'tobkiri.launcher.crash_drafts.v1';
  capturedAt: string;
  drafts: RecoverableDraft[];
}

export interface SafeCrashDiagnostic {
  schema: 'tobkiri.launcher.crash_diagnostic.v1';
  reference: string;
  code: 'viewer.render_crash';
  errorType: string;
  route: string;
  componentNames: string[];
  createdAt: string;
}

function safeIdentifier(value: unknown, fallback: string, maxLength = 160): string {
  const text = typeof value === 'string' ? value : '';
  const normalized = text.trim().replace(/[^A-Za-z0-9._:/-]+/g, '_');
  return (normalized || fallback).slice(0, maxLength);
}

function safeRoute(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) {
    return '/panel/';
  }
  const path = value.split(/[?#]/, 1)[0];
  const knownRoutes = new Set([
    '/panel/',
    '/panel/setup',
    '/panel/packs',
    '/panel/profile',
    '/panel/settings',
    '/panel/profile-graph',
    '/panel/profile-workspace',
    '/panel/flows',
    '/panel/graphs',
    '/panel/ai-input',
    '/panel/api-map',
    '/panel/nodes',
  ]);
  if (knownRoutes.has(path)) return path;
  if (path.startsWith('/panel/packs/')) return '/panel/packs/:pack';
  return '/panel/:surface';
}

function safeDraftValue(value: unknown): unknown {
  if (typeof value === 'string') return value.slice(0, MAX_FIELD_LENGTH);
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) return value;
  if (Array.isArray(value)) return value.slice(0, 100).map(safeDraftValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !SENSITIVE_FIELD.test(key))
        .slice(0, MAX_FIELDS)
        .map(([key, item]) => [safeIdentifier(key, 'field', 80), safeDraftValue(item)]),
    );
  }
  return undefined;
}

function hasDraftContent(value: unknown): boolean {
  if (typeof value === 'string') return value.trim().length > 0;
  if (typeof value === 'number' || typeof value === 'boolean') return true;
  if (Array.isArray(value)) return value.some(hasDraftContent);
  if (value && typeof value === 'object') return Object.values(value).some(hasDraftContent);
  return false;
}

function parseDrafts(storage: SafeStorage | null): RecoverableDraft[] {
  const raw = readSafeStorageValue(storage, RECOVERABLE_DRAFT_STORAGE_KEY);
  if (!raw || raw.length > MAX_SERIALIZED_LENGTH) return [];
  try {
    const parsed = JSON.parse(raw) as {drafts?: unknown};
    if (!Array.isArray(parsed.drafts)) return [];
    return parsed.drafts.flatMap((item): RecoverableDraft[] => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
      const record = item as Record<string, unknown>;
      if (!record.fields || typeof record.fields !== 'object' || Array.isArray(record.fields)) return [];
      const fields = safeDraftValue(record.fields) as Record<string, unknown>;
      if (!hasDraftContent(fields)) return [];
      return [{
        id: safeIdentifier(record.id, 'draft'),
        label: typeof record.label === 'string' ? record.label.slice(0, 160) : 'Recoverable input',
        route: safeRoute(record.route),
        updatedAt: typeof record.updatedAt === 'string' ? record.updatedAt.slice(0, 40) : '',
        fields,
      }];
    }).slice(0, MAX_DRAFTS);
  } catch {
    return [];
  }
}

function draftStorage(): SafeStorage | null {
  return getBrowserStorage('session');
}

/** Save a bounded, non-secret local input draft for crash recovery. */
export function saveRecoverableDraft(input: {
  id: string;
  label: string;
  route: string;
  fields: Record<string, unknown>;
}): boolean {
  const storage = draftStorage();
  const id = safeIdentifier(input.id, 'draft');
  const fields = safeDraftValue(input.fields) as Record<string, unknown>;
  const current = parseDrafts(storage).filter((draft) => draft.id !== id);
  if (hasDraftContent(fields)) {
    current.unshift({
      id,
      label: input.label.slice(0, 160),
      route: safeRoute(input.route),
      updatedAt: new Date().toISOString(),
      fields,
    });
  }
  const serialized = JSON.stringify({schema: 'tobkiri.launcher.drafts.v1', drafts: current.slice(0, MAX_DRAFTS)});
  if (serialized.length > MAX_SERIALIZED_LENGTH) return false;
  return writeSafeStorageValue(storage, RECOVERABLE_DRAFT_STORAGE_KEY, serialized);
}

/** Read one previously saved local input draft. */
export function readRecoverableDraft(id: string): RecoverableDraft | null {
  const safeId = safeIdentifier(id, 'draft');
  return parseDrafts(draftStorage()).find((draft) => draft.id === safeId) ?? null;
}

/** Remove a draft after its owner explicitly saves or submits the input. */
export function clearRecoverableDraft(id: string): void {
  const storage = draftStorage();
  const safeId = safeIdentifier(id, 'draft');
  const drafts = parseDrafts(storage).filter((draft) => draft.id !== safeId);
  if (drafts.length === 0) {
    removeSafeStorageValue(storage, RECOVERABLE_DRAFT_STORAGE_KEY);
    return;
  }
  writeSafeStorageValue(
    storage,
    RECOVERABLE_DRAFT_STORAGE_KEY,
    JSON.stringify({schema: 'tobkiri.launcher.drafts.v1', drafts}),
  );
}

/** Capture the recoverable draft index without exposing its content in the UI. */
export function recoverableDraftSnapshot(): CrashDraftSnapshot | null {
  const drafts = parseDrafts(draftStorage());
  return drafts.length > 0 ? {
    schema: 'tobkiri.launcher.crash_drafts.v1',
    capturedAt: new Date().toISOString(),
    drafts,
  } : null;
}

/** Serialize an explicit user-requested export of the bounded drafts. */
export function crashDraftExport(snapshot: CrashDraftSnapshot): string {
  return JSON.stringify(snapshot, null, 2);
}

/** Count recent recovery failures in this browsing session. */
export function recordCrash(now = Date.now()): number {
  const storage = getBrowserStorage('session');
  const raw = readSafeStorageValue(storage, CRASH_HISTORY_STORAGE_KEY);
  let prior: number[] = [];
  try {
    const parsed = JSON.parse(raw ?? '[]') as unknown;
    if (Array.isArray(parsed)) {
      prior = parsed.filter((value): value is number => typeof value === 'number');
    }
  } catch {
    prior = [];
  }
  const recent = [...prior.filter((value) => now - value < CRASH_WINDOW_MS), now].slice(-5);
  writeSafeStorageValue(storage, CRASH_HISTORY_STORAGE_KEY, JSON.stringify(recent));
  return recent.length;
}

function componentNames(componentStack: string | null | undefined): string[] {
  if (!componentStack) return [];
  return componentStack.split(/\r?\n/)
    .map((line) => line.match(/^\s*at\s+([A-Za-z0-9_$.[\]-]{1,100})/)?.[1])
    .filter((name): name is string => Boolean(name))
    .slice(0, 12);
}

/** Build a secret-free diagnostic that contains types and component names only. */
export function createSafeCrashDiagnostic(
  error: unknown,
  componentStack?: string | null,
  route = typeof window === 'undefined' ? '/panel/' : window.location.pathname,
): SafeCrashDiagnostic {
  const errorType = error instanceof Error ? error.name : typeof error;
  const seed = `${errorType}:${safeRoute(route)}:${componentNames(componentStack).join(':')}`;
  let hash = 0x811c9dc5;
  for (let index = 0; index < seed.length; index += 1) {
    hash ^= seed.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return {
    schema: 'tobkiri.launcher.crash_diagnostic.v1',
    reference: `diag-${(hash >>> 0).toString(16).padStart(8, '0')}`,
    code: 'viewer.render_crash',
    errorType: safeIdentifier(errorType, 'Error', 80),
    route: safeRoute(route),
    componentNames: componentNames(componentStack),
    createdAt: new Date().toISOString(),
  };
}

/** Persist the safe diagnostic locally and return an explicit acknowledgement. */
export function reportSafeCrashDiagnostic(diagnostic: SafeCrashDiagnostic): boolean {
  return writeSafeStorageValue(
    getBrowserStorage('session'),
    CRASH_DIAGNOSTIC_STORAGE_KEY,
    JSON.stringify(diagnostic),
  );
}

/** Reset only presentation state that can cause a repeated render failure. */
export function resetAffectedClientState(): void {
  const storage = getBrowserStorage('local');
  for (const key of [
    'tobkiri-launcher-sidebar-open',
    'rumi-viewer-sidebar-open',
    'tobkiri-theme',
    'rumi-theme',
    'tobkiri-color-mode',
    'rumi-color-mode',
  ]) {
    removeSafeStorageValue(storage, key);
  }
}
