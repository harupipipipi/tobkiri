import type {
  ApiUpdateInfo,
  ApiUpdateSettings,
  ApiUpdateTarget,
  RuntimeUpdatesResponseData,
  UpdateApplyResponseData,
} from './apiTypes';
import {hostApiFetch} from './hostClient';

const UPDATE_TARGETS: ReadonlySet<string> = new Set(['tobkiri', 'defaultspack']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredString(value: Record<string, unknown>, field: string): string {
  if (typeof value[field] !== 'string') {
    throw new Error(`Updates response field ${field} is invalid.`);
  }
  return value[field] as string;
}

function optionalString(value: Record<string, unknown>, field: string): string | null {
  const candidate = value[field];
  if (candidate === null || candidate === undefined) return null;
  if (typeof candidate !== 'string') {
    throw new Error(`Updates response field ${field} is invalid.`);
  }
  return candidate;
}

function requiredBoolean(value: Record<string, unknown>, field: string): boolean {
  if (typeof value[field] !== 'boolean') {
    throw new Error(`Updates response field ${field} is invalid.`);
  }
  return value[field] as boolean;
}

function requiredStringArray(value: Record<string, unknown>, field: string): string[] {
  const candidate = value[field];
  if (!Array.isArray(candidate) || candidate.some((entry) => typeof entry !== 'string')) {
    throw new Error(`Updates response field ${field} is invalid.`);
  }
  return candidate as string[];
}

function requiredNumber(value: Record<string, unknown>, field: string): number {
  if (typeof value[field] !== 'number' || !Number.isFinite(value[field])) {
    throw new Error(`Updates response field ${field} is invalid.`);
  }
  return value[field] as number;
}

function parseUpdateTarget(value: unknown, field: string): ApiUpdateTarget {
  if (typeof value !== 'string' || !UPDATE_TARGETS.has(value)) {
    throw new Error(`Updates response field ${field} is invalid.`);
  }
  return value as ApiUpdateTarget;
}

export function parseUpdateInfo(value: unknown): ApiUpdateInfo {
  if (!isRecord(value)) throw new Error('Updates response entry is not an object.');
  return {
    target: parseUpdateTarget(value.target, 'target'),
    current_version: requiredString(value, 'current_version'),
    latest_version: requiredString(value, 'latest_version'),
    update_available: requiredBoolean(value, 'update_available'),
    release_url: requiredString(value, 'release_url'),
    repo: requiredString(value, 'repo'),
  };
}

export function parseRuntimeUpdatesResponse(value: unknown): RuntimeUpdatesResponseData {
  if (!isRecord(value)) throw new Error('Updates response is not an object.');
  if (!Array.isArray(value.updates)) {
    throw new Error('Updates response field updates is invalid.');
  }
  const result: RuntimeUpdatesResponseData = {
    updates: value.updates.map(parseUpdateInfo),
  };
  const checkError = optionalString(value, 'check_error');
  if (checkError !== null) result.check_error = checkError;
  return result;
}

export function parseUpdateSettings(value: unknown): ApiUpdateSettings {
  if (!isRecord(value)) throw new Error('Update settings response is not an object.');
  const autoUpdate = value.auto_update;
  if (!isRecord(autoUpdate)) {
    throw new Error('Update settings response field auto_update is invalid.');
  }
  const parsedAutoUpdate: Partial<Record<ApiUpdateTarget, boolean>> = {};
  for (const [key, entry] of Object.entries(autoUpdate)) {
    parsedAutoUpdate[parseUpdateTarget(key, 'auto_update')] =
      typeof entry === 'boolean' ? entry : Boolean(entry);
  }
  return {
    auto_update: parsedAutoUpdate as Record<ApiUpdateTarget, boolean>,
    check_interval_hours:
      typeof value.check_interval_hours === 'number' ? value.check_interval_hours : 0,
    last_checked_at: optionalString(value, 'last_checked_at'),
    last_results: Array.isArray(value.last_results)
      ? (value.last_results.filter(isRecord) as Array<Record<string, unknown>>)
      : [],
    updated_at: optionalString(value, 'updated_at'),
  };
}

export function parseUpdateApplyResponse(value: unknown): UpdateApplyResponseData {
  if (!isRecord(value)) throw new Error('Update apply response is not an object.');
  const result: UpdateApplyResponseData = {
    target: parseUpdateTarget(value.target, 'target'),
    current_version: requiredString(value, 'current_version'),
    latest_version: requiredString(value, 'latest_version'),
    release_url: requiredString(value, 'release_url'),
    backup_dir: requiredString(value, 'backup_dir'),
    applied_files: requiredStringArray(value, 'applied_files'),
    skipped_files: requiredStringArray(value, 'skipped_files'),
    applied_count: requiredNumber(value, 'applied_count'),
    skipped_count: requiredNumber(value, 'skipped_count'),
  };
  if (value.restart_required === true) result.restart_required = true;
  if (value.routes_reload_recommended === true) result.routes_reload_recommended = true;
  return result;
}

/** List per-target runtime update status from the Host. */
export function fetchRuntimeUpdates(): Promise<RuntimeUpdatesResponseData> {
  return hostApiFetch<unknown>('/api/v4/updates', {cache: 'no-store'})
    .then(parseRuntimeUpdatesResponse);
}

/** Read the persisted per-target auto-update preferences. */
export function fetchRuntimeUpdateSettings(): Promise<ApiUpdateSettings> {
  return hostApiFetch<unknown>('/api/v4/updates/settings', {cache: 'no-store'})
    .then(parseUpdateSettings);
}

/** Toggle auto-update for one runtime target. */
export function setRuntimeAutoUpdate(
  target: ApiUpdateTarget,
  enabled: boolean,
): Promise<ApiUpdateSettings> {
  return hostApiFetch<unknown>('/api/v4/updates/settings', {
    method: 'POST',
    body: JSON.stringify({auto_update: {[target]: enabled}}),
  }).then(parseUpdateSettings);
}

/** Apply the latest verified release overlay to one runtime target. */
export function applyRuntimeUpdate(
  target: ApiUpdateTarget,
): Promise<UpdateApplyResponseData> {
  return hostApiFetch<unknown>('/api/v4/updates/apply', {
    method: 'POST',
    body: JSON.stringify({target}),
  }).then(parseUpdateApplyResponse);
}
