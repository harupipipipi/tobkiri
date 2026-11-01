export type FileEditOperation = 'create' | 'write' | 'patch' | 'delete' | 'move';
export type FileEditStatsReason = 'unknown' | 'binary' | 'too_large' | 'sensitive' | 'unsupported_encoding';
export type FileEditReceipt = {
  schema_version: 1; receipt_id: string; file_id: string;
  status: 'committed'; operation: FileEditOperation;
  profile_id: string; workspace_id: string; root_id: string; frame_id: string | null;
  path: string; previous_path: string | null; occurred_at_ms: number;
  sequence: number; version: string;
  stats: { status: 'available'; lines_added: number; lines_deleted: number }
    | { status: 'unavailable'; reason: FileEditStatsReason };
};
export type FileEditTimelineEntry = {
  eventId: string; fileId: string; path: string; timestamp: number; order: number;
  addedLines: number | null; deletedLines: number | null; operation: FileEditOperation;
  status: 'committed'; statsReason?: FileEditStatsReason;
  profileId: string; workspaceId: string; rootId: string; frameId: string | null;
  version: string; previousPath: string | null;
};
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value: Record<string, unknown>, keys: string[]): boolean =>
  Object.keys(value).length === keys.length && keys.every(key => Object.prototype.hasOwnProperty.call(value, key));
const text = (value: unknown): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= 1024 && value === value.trim()
  && !/[\u0000-\u001f\u007f]/.test(value);
const natural = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const relativePath = (value: unknown): value is string =>
  text(value) && !value.includes('\\') && !value.startsWith('/')
  && !/^[A-Za-z]:/.test(value) && value.split('/').every(part =>
    part.length > 0 && part !== '.' && part !== '..');

/** Parse only a complete public committed edit receipt, never tool arguments. */
export function parseFileEditReceipt(value: unknown): FileEditReceipt | null {
  if (!record(value) || !exactKeys(value, [
    'schema_version', 'receipt_id', 'file_id', 'status', 'operation', 'profile_id',
    'workspace_id', 'root_id', 'frame_id', 'path', 'previous_path',
    'occurred_at_ms', 'sequence', 'version', 'stats',
  ])) return null;
  if (value.schema_version !== 1 || value.status !== 'committed'
    || !text(value.receipt_id) || !/^file-edit:[A-Za-z0-9._~-]+$/.test(value.receipt_id)
    || (typeof value.operation !== 'string' || !['create', 'write', 'patch', 'delete', 'move'].includes(value.operation))
    || !['file_id', 'profile_id', 'workspace_id', 'root_id', 'version'].every(key => text(value[key]))
    || !(value.frame_id === null || text(value.frame_id))
    || !relativePath(value.path) || !natural(value.occurred_at_ms)
    || !natural(value.sequence)) return null;
  if (value.operation === 'move'
    ? !relativePath(value.previous_path) || value.previous_path === value.path
    : value.previous_path !== null) return null;
  const stats = value.stats;
  if (!record(stats)) return null;
  let publicStats: FileEditReceipt['stats'];
  if (stats.status === 'available' && exactKeys(stats, ['status', 'lines_added', 'lines_deleted'])
    && natural(stats.lines_added) && natural(stats.lines_deleted)) {
    publicStats = { status: 'available', lines_added: stats.lines_added, lines_deleted: stats.lines_deleted };
  } else if (stats.status === 'unavailable' && exactKeys(stats, ['status', 'reason'])
    && typeof stats.reason === 'string' && ['unknown', 'binary', 'too_large', 'sensitive', 'unsupported_encoding'].includes(stats.reason)) {
    publicStats = { status: 'unavailable', reason: stats.reason as FileEditStatsReason };
  } else return null;
  return {
    schema_version: 1, receipt_id: value.receipt_id, file_id: value.file_id as string,
    status: 'committed', operation: value.operation as FileEditOperation,
    profile_id: value.profile_id as string, workspace_id: value.workspace_id as string,
    root_id: value.root_id as string, frame_id: value.frame_id as string | null,
    path: value.path, previous_path: value.previous_path as string | null,
    occurred_at_ms: value.occurred_at_ms, sequence: value.sequence,
    version: value.version as string, stats: publicStats,
  };
}

/** Deduplicate receipts by identity while discarding contradictory replays. */
export function buildFileEditTimelineEntries(receipts: unknown[]): FileEditTimelineEntry[] {
  const accepted = new Map<string, FileEditReceipt>();
  const conflicts = new Set<string>();
  for (const value of receipts) {
    const receipt = parseFileEditReceipt(value);
    if (!receipt || conflicts.has(receipt.receipt_id)) continue;
    const previous = accepted.get(receipt.receipt_id);
    if (previous && JSON.stringify(previous) !== JSON.stringify(receipt)) {
      accepted.delete(receipt.receipt_id);
      conflicts.add(receipt.receipt_id);
    } else accepted.set(receipt.receipt_id, receipt);
  }
  return [...accepted.values()].sort((a, b) => a.sequence - b.sequence
    || a.occurred_at_ms - b.occurred_at_ms
    || (a.receipt_id < b.receipt_id ? -1 : a.receipt_id > b.receipt_id ? 1 : 0))
    .map(receipt => ({
      eventId: receipt.receipt_id, fileId: receipt.file_id, path: receipt.path,
      timestamp: receipt.occurred_at_ms, order: receipt.sequence,
      addedLines: receipt.stats.status === 'available' ? receipt.stats.lines_added : null,
      deletedLines: receipt.stats.status === 'available' ? receipt.stats.lines_deleted : null,
      operation: receipt.operation, status: receipt.status,
      ...(receipt.stats.status === 'unavailable' ? { statsReason: receipt.stats.reason } : {}),
      profileId: receipt.profile_id, workspaceId: receipt.workspace_id,
      rootId: receipt.root_id, frameId: receipt.frame_id, version: receipt.version,
      previousPath: receipt.previous_path,
    }));
}

const CREATE_TOOL_IDS = new Set([
  'coding_file_create', 'rumi_default_tools_pack:coding_file_create',
]);
const BLOCKED_RESULT_STATES = new Set([
  'failed', 'error', 'failure', 'denied', 'rejected', 'cancelled', 'canceled',
  'pending', 'running', 'queued', 'started', 'approval_required',
  'pending_approval', 'requires_approval', 'ambiguous', 'stale',
]);
const own = (value: Record<string, unknown>, key: string): boolean =>
  Object.prototype.hasOwnProperty.call(value, key);

/** Extract only a verified create receipt from bounded known owner wrappers. */
export function parseFileEditReceiptFromToolResult(
  toolId: string, value: unknown,
): FileEditReceipt | null {
  if (!CREATE_TOOL_IDS.has(toolId)) return null;
  const visit = (candidate: unknown, depth: number): FileEditReceipt | null => {
    if (depth > 5) return null;
    if (typeof candidate === 'string') {
      if (candidate.length > 16384) return null;
      try { return visit(JSON.parse(candidate), depth); } catch { return null; }
    }
    if (!record(candidate)
      || (Object.getPrototypeOf(candidate) !== Object.prototype
        && Object.getPrototypeOf(candidate) !== null)) return null;
    for (const key of ['is_error', 'approval_required', 'requires_approval', 'cancelled']) {
      if (own(candidate, key) && candidate[key] === true) return null;
    }
    for (const key of ['success', 'ok']) {
      if (own(candidate, key) && candidate[key] === false) return null;
    }
    if (own(candidate, 'error') && candidate.error !== null && candidate.error !== undefined) return null;
    for (const key of ['status', 'state', 'phase', 'outcome']) {
      const state = own(candidate, key) ? candidate[key] : undefined;
      if (typeof state === 'string' && BLOCKED_RESULT_STATES.has(state.trim().toLowerCase())) return null;
    }
    if (own(candidate, 'file_edit_receipt')) {
      const receipt = parseFileEditReceipt(candidate.file_edit_receipt);
      if (!receipt || receipt.operation !== 'create'
        || !own(candidate, 'created') || candidate.created !== true
        || !own(candidate, 'path') || candidate.path !== receipt.path
        || !own(candidate, 'workspace_id') || candidate.workspace_id !== receipt.workspace_id) return null;
      return receipt;
    }
    const results: FileEditReceipt[] = [];
    for (const key of ['data', 'result']) {
      if (!own(candidate, key)) continue;
      const receipt = visit(candidate[key], depth + 1);
      if (receipt) results.push(receipt);
      else return null;
    }
    if (results.length === 0) return null;
    if (results.some(receipt => JSON.stringify(receipt) !== JSON.stringify(results[0]))) return null;
    return results[0];
  };
  return visit(value, 0);
}
