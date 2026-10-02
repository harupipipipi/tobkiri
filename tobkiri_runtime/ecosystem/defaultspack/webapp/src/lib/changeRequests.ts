import {
  defaultspackApiFetch,
  defaultspackContractRoute,
  explainDefaultspackApiError,
  type DefaultspackContractRoute,
} from "./api";

export type ChangeRequestStatus = "draft" | "open" | "changes_requested" | "approved" | "committed" | "closed" | "stale" | string;
export type ChangeRequestDecision = "none" | "commented" | "changes_requested" | "approved" | string;

export type ChangeRequestCheckSummary = {
  total?: number;
  passed?: number;
  failed?: number;
  pending?: number;
  skipped?: number;
  label?: string;
};

export type ChangeRequestFile = {
  path: string;
  status?: string;
  additions?: number;
  deletions?: number;
  binary?: boolean;
  untracked?: boolean;
  generated?: boolean;
  docs?: boolean;
  test?: boolean;
  highRisk?: boolean;
  large?: boolean;
};

export type ChangeRequestComment = {
  id: string;
  thread_id?: string;
  kind?: "comment" | "change_request" | "suggestion" | string;
  body?: string;
  path?: string;
  line?: number | null;
  side?: string;
  author?: string;
  suggested_patch?: string;
  resolved?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type ChangeRequestThread = {
  id: string;
  path?: string;
  line?: number | null;
  resolved?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type ChangeRequestViewedFile = {
  path: string;
  viewed: boolean;
  updated_at?: string;
};

export type ChangeRequestCheck = {
  id: string;
  name?: string;
  command?: string;
  status?: string;
  exit_code?: number | null;
  stdout_tail?: string;
  stderr_tail?: string;
  log_tail?: string;
  full_log?: string;
  log_ref?: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number | null;
};

export type ChangeRequestSuggestedCheck = {
  id: string;
  name?: string;
  command: string;
  reason?: string;
};

export type ChangeRequestSeal = {
  valid?: boolean;
  reason?: string;
  checked_at?: string;
  snapshot_working_tree_hash?: string;
  current_working_tree_hash?: string;
  mismatch_paths?: string[];
};

export type ChangeRequestSnapshot = {
  id?: string;
  created_at?: string;
  signature?: string;
  diff?: string;
  stat?: string;
  files?: ChangeRequestFile[];
};

export type ChangeRequestDrift = {
  changed?: boolean;
  stale?: boolean;
  has_drift?: boolean;
  mismatched?: boolean;
  base_changed?: boolean;
  previous_working_tree_hash?: string;
  current_working_tree_hash?: string;
  snapshot_working_tree_hash?: string;
  added_paths?: string[];
  removed_paths?: string[];
  changed_paths?: string[];
};

export type ChangeRequestRecord = {
  id: string;
  revision?: number;
  status: ChangeRequestStatus;
  decision?: ChangeRequestDecision;
  title?: string;
  summary?: string;
  created_at?: string;
  updated_at?: string;
  workspace_id?: string | null;
  check_summary?: ChangeRequestCheckSummary;
  checks?: ChangeRequestCheck[];
  suggested_checks?: ChangeRequestSuggestedCheck[];
  comments?: ChangeRequestComment[];
  review_threads?: ChangeRequestThread[];
  viewed_files?: Record<string, ChangeRequestViewedFile>;
  unresolved_count?: number;
  unresolved_comment_count?: number;
  suggestion_count?: number;
  viewed_file_count?: number;
  commit_seal?: ChangeRequestSeal;
  commit?: Record<string, unknown>;
  snapshot?: ChangeRequestSnapshot;
  drift?: ChangeRequestDrift;
  is_stale?: boolean;
  current_working_tree_hash?: string;
  snapshot_working_tree_hash?: string;
  files?: ChangeRequestFile[];
};

export type ChangeRequestMutationContext = {
  expected_revision?: number;
  expected_updated_at?: string;
  expected_snapshot_working_tree_hash?: string;
  expected_current_working_tree_hash?: string;
  idempotency_key: string;
};

export class ChangeRequestMutationConflictError extends Error {
  readonly code = "CHANGE_REQUEST_CONFLICT";
}

export class ChangeRequestApiUnavailableError extends Error {
  readonly code = "CHANGE_REQUEST_API_UNAVAILABLE";
}

export function changeRequestMutationContext(review: ChangeRequestRecord, action: string): ChangeRequestMutationContext {
  const actionKey = action.slice(0, 120);
  return {
    expected_revision: review.revision,
    expected_updated_at: review.updated_at,
    expected_snapshot_working_tree_hash: review.snapshot_working_tree_hash ?? review.snapshot?.signature,
    expected_current_working_tree_hash: review.current_working_tree_hash,
    // Revision-scoped deterministic keys make a timeout retry a replay rather
    // than a second mutation. A changed payload with the same key fails closed.
    idempotency_key: `cr:${review.id}:${review.revision ?? "unknown"}:${actionKey}`,
  };
}

export type ChangeRequestListResponse = {
  reviews: ChangeRequestRecord[];
  open: ChangeRequestRecord[];
  closed: ChangeRequestRecord[];
  apiAvailable: boolean;
};

type ApiEnvelope<T> = { status?: string; data?: T; error?: { code?: string; message?: string } };

function withQuery(path: DefaultspackContractRoute, params?: Record<string, unknown>): DefaultspackContractRoute {
  if (!params) return path;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? defaultspackContractRoute(`${path.apiPath}?${suffix}`) : path;
}

function readString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

function readNumber(record: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function readBoolean(record: Record<string, unknown>, keys: string[]): boolean | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "boolean") return value;
  }
  return undefined;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function normalizeCheckSummary(value: unknown): ChangeRequestCheckSummary | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  return {
    total: readNumber(record, ["total", "count"]),
    passed: readNumber(record, ["passed", "ok", "success"]),
    failed: readNumber(record, ["failed", "failures", "error"]),
    pending: readNumber(record, ["pending", "running"]),
    skipped: readNumber(record, ["skipped"]),
    label: readString(record, ["label", "summary", "status"]),
  };
}

function normalizeFile(value: unknown): ChangeRequestFile | null {
  if (typeof value === "string") return { path: value };
  const record = asRecord(value);
  if (!record) return null;
  const path = readString(record, ["path", "file", "name"]);
  if (!path) return null;
  return {
    path,
    status: readString(record, ["status", "change_type", "kind"]),
    additions: readNumber(record, ["additions", "added"]),
    deletions: readNumber(record, ["deletions", "deleted"]),
    binary: record.binary === true,
    untracked: record.untracked === true,
    generated: record.generated === true,
    docs: record.docs === true,
    test: record.test === true,
    highRisk: record.high_risk === true || record.highRisk === true,
    large: record.large === true,
  };
}

function normalizeComment(value: unknown): ChangeRequestComment | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = readString(record, ["id", "comment_id"]);
  if (!id) return null;
  return {
    id,
    thread_id: readString(record, ["thread_id", "threadId"]),
    kind: readString(record, ["kind", "type"]),
    body: readString(record, ["body", "text"]),
    path: readString(record, ["path", "file_path"]),
    line: readNumber(record, ["line"]) ?? null,
    side: readString(record, ["side"]),
    author: readString(record, ["author"]),
    suggested_patch: readString(record, ["suggested_patch", "suggestedPatch"]),
    resolved: readBoolean(record, ["resolved"]),
    created_at: readString(record, ["created_at", "createdAt"]),
    updated_at: readString(record, ["updated_at", "updatedAt"]),
  };
}

function normalizeThread(value: unknown): ChangeRequestThread | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = readString(record, ["id", "thread_id"]);
  if (!id) return null;
  return {
    id,
    path: readString(record, ["path", "file_path"]),
    line: readNumber(record, ["line"]) ?? null,
    resolved: readBoolean(record, ["resolved"]),
    created_at: readString(record, ["created_at", "createdAt"]),
    updated_at: readString(record, ["updated_at", "updatedAt"]),
  };
}

function normalizeCheck(value: unknown): ChangeRequestCheck | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = readString(record, ["id", "check_id"]);
  if (!id) return null;
  return {
    id,
    name: readString(record, ["name", "label"]),
    command: readString(record, ["command"]),
    status: readString(record, ["status"]),
    exit_code: readNumber(record, ["exit_code", "exitCode"]) ?? null,
    stdout_tail: readString(record, ["stdout_tail", "stdoutTail"]),
    stderr_tail: readString(record, ["stderr_tail", "stderrTail"]),
    log_tail: readString(record, ["log_tail", "logTail"]),
    full_log: readString(record, ["full_log", "fullLog"]),
    log_ref: readString(record, ["log_ref", "logRef"]),
    started_at: readString(record, ["started_at", "startedAt"]),
    completed_at: readString(record, ["completed_at", "completedAt"]),
    duration_ms: readNumber(record, ["duration_ms", "durationMs"]) ?? null,
  };
}

function normalizeSuggestedCheck(value: unknown): ChangeRequestSuggestedCheck | null {
  const record = asRecord(value);
  if (!record) return null;
  const command = readString(record, ["command"]);
  if (!command) return null;
  return {
    id: readString(record, ["id", "check_id"]) ?? command,
    name: readString(record, ["name", "label"]),
    command,
    reason: readString(record, ["reason", "description"]),
  };
}

function normalizeViewedFiles(value: unknown): Record<string, ChangeRequestViewedFile> | undefined {
  if (Array.isArray(value)) {
    const result: Record<string, ChangeRequestViewedFile> = {};
    for (const raw of value) {
      const item = asRecord(raw);
      if (!item) continue;
      const path = readString(item, ["path", "file_path"]);
      if (!path) continue;
      result[path] = { path, viewed: readBoolean(item, ["viewed"]) === true, updated_at: readString(item, ["updated_at", "updatedAt"]) };
    }
    return result;
  }
  const record = asRecord(value);
  if (!record) return undefined;
  const result: Record<string, ChangeRequestViewedFile> = {};
  for (const [key, raw] of Object.entries(record)) {
    const item = asRecord(raw);
    if (item) {
      const path = readString(item, ["path", "file_path"]) ?? key;
      result[path] = { path, viewed: readBoolean(item, ["viewed"]) === true, updated_at: readString(item, ["updated_at", "updatedAt"]) };
    } else {
      result[key] = { path: key, viewed: raw === true };
    }
  }
  return result;
}

function normalizeSeal(value: unknown): ChangeRequestSeal | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  return {
    valid: readBoolean(record, ["valid"]),
    reason: readString(record, ["reason"]),
    checked_at: readString(record, ["checked_at", "checkedAt"]),
    snapshot_working_tree_hash: readString(record, ["snapshot_working_tree_hash", "snapshot_worktree_hash"]),
    current_working_tree_hash: readString(record, ["current_working_tree_hash", "current_worktree_hash"]),
    mismatch_paths: normalizeStringList(record.mismatch_paths ?? record.changed_paths),
  };
}

function normalizeFiles(value: unknown): ChangeRequestFile[] {
  return Array.isArray(value) ? value.map(normalizeFile).filter((file): file is ChangeRequestFile => file !== null) : [];
}

function mergeFiles(...groups: Array<ChangeRequestFile[] | undefined>): ChangeRequestFile[] {
  const byPath = new Map<string, ChangeRequestFile>();
  for (const group of groups) {
    for (const file of group ?? []) {
      byPath.set(file.path, { ...byPath.get(file.path), ...file });
    }
  }
  return [...byPath.values()];
}

function normalizeStringList(value: unknown): string[] | undefined {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : undefined;
}

function normalizeSnapshot(value: unknown): ChangeRequestSnapshot | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  const signature = readString(record, ["signature", "tree_signature", "working_tree_signature", "working_tree_hash"]);
  const diff = readString(record, ["diff", "patch", "normalized_patch"]);
  const stat = readString(record, ["stat", "diff_stat"]);
  const files = normalizeFiles(record.files ?? record.file_stats);
  if (!signature && !diff && !stat && files.length === 0) return undefined;
  return {
    id: readString(record, ["id", "snapshot_id"]),
    created_at: readString(record, ["created_at", "createdAt"]),
    signature,
    diff,
    stat,
    files,
  };
}

function normalizeDrift(value: unknown): ChangeRequestDrift | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  const drift: ChangeRequestDrift = {
    changed: readBoolean(record, ["changed"]),
    stale: readBoolean(record, ["stale"]),
    has_drift: readBoolean(record, ["has_drift"]),
    mismatched: readBoolean(record, ["mismatched"]),
    base_changed: readBoolean(record, ["base_changed"]),
    previous_working_tree_hash: readString(record, ["previous_working_tree_hash", "previous_worktree_hash"]),
    current_working_tree_hash: readString(record, ["current_working_tree_hash", "current_worktree_hash"]),
    snapshot_working_tree_hash: readString(record, ["snapshot_working_tree_hash", "snapshot_worktree_hash"]),
    added_paths: normalizeStringList(record.added_paths),
    removed_paths: normalizeStringList(record.removed_paths),
    changed_paths: normalizeStringList(record.changed_paths ?? record.mismatch_paths),
  };
  return Object.values(drift).some((item) => item !== undefined) ? drift : undefined;
}

function normalizeReview(value: unknown): ChangeRequestRecord | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = readString(record, ["id", "change_request_id", "cr_id", "review_id"]);
  if (!id) return null;
  const status = readString(record, ["status", "state"]) ?? "open";
  const snapshot = normalizeSnapshot(record.snapshot ?? record.latest_snapshot ?? record.base_snapshot ?? record);
  const topLevelFiles = normalizeFiles(record.files ?? record.file_stats);
  const drift = normalizeDrift(record.drift ?? record.last_drift);
  return {
    id,
    revision: readNumber(record, ["revision", "version", "etag_version"]),
    status,
    decision: readString(record, ["decision"]),
    title: readString(record, ["title", "name"]),
    summary: readString(record, ["summary", "description"]),
    created_at: readString(record, ["created_at", "createdAt"]),
    updated_at: readString(record, ["updated_at", "updatedAt"]),
    workspace_id: readString(record, ["workspace_id"]) ?? null,
    check_summary: normalizeCheckSummary(record.check_summary ?? record.checks),
    checks: Array.isArray(record.checks) ? record.checks.map(normalizeCheck).filter((check): check is ChangeRequestCheck => check !== null) : undefined,
    suggested_checks: Array.isArray(record.suggested_checks) ? record.suggested_checks.map(normalizeSuggestedCheck).filter((check): check is ChangeRequestSuggestedCheck => check !== null) : undefined,
    comments: Array.isArray(record.comments) ? record.comments.map(normalizeComment).filter((comment): comment is ChangeRequestComment => comment !== null) : undefined,
    review_threads: Array.isArray(record.review_threads) ? record.review_threads.map(normalizeThread).filter((thread): thread is ChangeRequestThread => thread !== null) : undefined,
    viewed_files: normalizeViewedFiles(record.viewed_files),
    unresolved_count: readNumber(record, ["unresolved_count"]),
    unresolved_comment_count: readNumber(record, ["unresolved_comment_count"]),
    suggestion_count: readNumber(record, ["suggestion_count"]),
    viewed_file_count: readNumber(record, ["viewed_file_count"]),
    commit_seal: normalizeSeal(record.commit_seal),
    commit: asRecord(record.commit) ?? undefined,
    snapshot,
    drift,
    is_stale: readBoolean(record, ["is_stale", "stale"]),
    current_working_tree_hash: readString(record, ["current_working_tree_hash", "current_worktree_hash", "working_tree_hash"]) ?? snapshot?.signature,
    snapshot_working_tree_hash: readString(record, ["snapshot_working_tree_hash", "snapshot_worktree_hash"]) ?? snapshot?.signature,
    files: mergeFiles(topLevelFiles, snapshot?.files),
  };
}

async function decodeResponse<T>(response: Response): Promise<T> {
  let envelope: ApiEnvelope<T>;
  try {
    envelope = await response.json() as ApiEnvelope<T>;
  } catch {
    if (!response.ok) throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
    throw new Error("Change request API returned invalid JSON");
  }
  if (!response.ok || envelope.status === "error") {
    throw new Error(explainDefaultspackApiError(
      response.status,
      envelope.status === "error" ? envelope.error : undefined,
      response.statusText,
    ));
  }
  return (envelope.data ?? envelope) as T;
}

async function requestChangeRequest<T>(path: DefaultspackContractRoute, init?: RequestInit, options?: { unavailableReturnsNull?: boolean }): Promise<T | null> {
  const response = await defaultspackApiFetch(path, { cache: "no-store", ...init });
  if (response.status === 409 || response.status === 412) {
    throw new ChangeRequestMutationConflictError("Review changed on another client. Reload before retrying.");
  }
  if (response.status === 404 || response.status === 405) {
    if (options?.unavailableReturnsNull !== false) return null;
    throw new ChangeRequestApiUnavailableError("Change request API is unavailable. Enable or configure it before retrying.");
  }
  return decodeResponse<T>(response);
}

export async function listChangeRequests(options?: { workspace_id?: string | null }): Promise<ChangeRequestListResponse> {
  const payload = await requestChangeRequest<unknown>(
    withQuery(defaultspackContractRoute("api/change-requests"), { workspace_id: options?.workspace_id }),
  );
  if (payload === null) return { reviews: [], open: [], closed: [], apiAvailable: false };
  const record = asRecord(payload);
  const rawReviews = Array.isArray(payload)
    ? payload
    : Array.isArray(record?.reviews)
      ? record.reviews
      : Array.isArray(record?.change_requests)
        ? record.change_requests
        : [];
  const reviews = rawReviews.map(normalizeReview).filter((review): review is ChangeRequestRecord => review !== null);
  return {
    reviews,
    open: reviews.filter((review) => !String(review.status).toLowerCase().includes("closed")),
    closed: reviews.filter((review) => String(review.status).toLowerCase().includes("closed")),
    apiAvailable: true,
  };
}

export async function createChangeRequest(payload: { workspace_id?: string | null }): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute("api/change-requests"), {
    method: "POST",
    body: JSON.stringify({
      domain: "change_request",
      source: "working_tree",
      workspace_id: payload.workspace_id,
    }),
  });
  const record = asRecord(result);
  return normalizeReview(record?.review ?? record?.change_request ?? result);
}

export async function getChangeRequest(reviewId: string): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}`));
  const record = asRecord(result);
  return normalizeReview(record?.review ?? record?.change_request ?? result);
}

export async function refreshChangeRequest(reviewId: string, payload: { workspace_id?: string | null } & Partial<ChangeRequestMutationContext>): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/refresh`), {
    method: "POST",
    body: JSON.stringify(payload),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  const review = normalizeReview(record?.review ?? record?.change_request ?? result);
  if (!review) return null;
  return {
    ...review,
    drift: normalizeDrift(record?.drift) ?? review.drift,
    is_stale: false,
  };
}

export async function addChangeRequestComment(reviewId: string, payload: {
  kind?: string;
  body?: string;
  path?: string;
  line?: number | null;
  suggested_patch?: string;
} & Partial<ChangeRequestMutationContext>): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/comments`), {
    method: "POST",
    body: JSON.stringify(payload),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  return normalizeReview(record?.change_request ?? record?.review ?? result);
}

export async function updateChangeRequestComment(reviewId: string, commentId: string, payload: {
  body?: string;
  resolved?: boolean;
  suggested_patch?: string;
} & Partial<ChangeRequestMutationContext>): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/comments/${encodeURIComponent(commentId)}`), {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  return normalizeReview(record?.change_request ?? record?.review ?? result);
}

export async function submitChangeRequestDecision(reviewId: string, payload: { decision: "approve" | "request_changes" | "comment" | "approved" | "changes_requested" | "commented"; body?: string } & Partial<ChangeRequestMutationContext>): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/decision`), {
    method: "POST",
    body: JSON.stringify(payload),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  return normalizeReview(record?.change_request ?? record?.review ?? result);
}

export async function setChangeRequestViewedFile(reviewId: string, path: string, viewed: boolean, context?: ChangeRequestMutationContext): Promise<ChangeRequestRecord | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/viewed-files`), {
    method: "PATCH",
    body: JSON.stringify({ path, viewed, ...(context ?? {}) }),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  return normalizeReview(record?.change_request ?? record?.review ?? result);
}

export async function listChangeRequestChecks(reviewId: string): Promise<{ review: ChangeRequestRecord | null; checks: ChangeRequestCheck[]; suggested_checks: ChangeRequestSuggestedCheck[] }> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/checks`));
  const record = asRecord(result);
  const review = normalizeReview(record?.change_request ?? record?.review ?? result);
  return {
    review,
    checks: Array.isArray(record?.checks) ? record.checks.map(normalizeCheck).filter((check): check is ChangeRequestCheck => check !== null) : review?.checks ?? [],
    suggested_checks: Array.isArray(record?.suggested_checks) ? record.suggested_checks.map(normalizeSuggestedCheck).filter((check): check is ChangeRequestSuggestedCheck => check !== null) : review?.suggested_checks ?? [],
  };
}

export async function runChangeRequestCheck(reviewId: string, command: string, context?: ChangeRequestMutationContext): Promise<{ review: ChangeRequestRecord | null; check: ChangeRequestCheck | null }> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/checks/run`), {
    method: "POST",
    body: JSON.stringify({ command, ...(context ?? {}) }),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  return {
    review: normalizeReview(record?.change_request ?? record?.review ?? result),
    check: normalizeCheck(record?.check),
  };
}

export async function getChangeRequestSeal(reviewId: string): Promise<ChangeRequestSeal | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/seal`));
  const record = asRecord(result);
  return normalizeSeal(record?.seal ?? result) ?? null;
}

export type ChangeRequestCommitResult = {
  committed?: boolean;
  blocked?: boolean;
  reason?: string;
  approval_required?: boolean;
  approval_request_id?: string;
  display_summary?: string;
  commit?: Record<string, unknown>;
  seal?: ChangeRequestSeal;
  review?: ChangeRequestRecord | null;
};

const CHANGE_REQUEST_COMMIT_ENABLED_VALUES = new Set(["1", "true", "yes", "on", "enabled"]);

export const changeRequestCommitEnabled = CHANGE_REQUEST_COMMIT_ENABLED_VALUES.has(
  String((import.meta as unknown as { env?: Record<string, string | undefined> }).env?.VITE_RUMI_REVIEW_ENABLE_COMMIT ?? "").trim().toLowerCase(),
);

export async function commitChangeRequest(reviewId: string, message: string, context?: ChangeRequestMutationContext): Promise<ChangeRequestCommitResult | null> {
  if (!changeRequestCommitEnabled) {
    return {
      committed: false,
      blocked: true,
      reason: "phase1_review_only",
      display_summary: "Rumi Review Phase 1 is review-only; commit is disabled by default.",
    };
  }
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/commit`), {
    method: "POST",
    body: JSON.stringify({ message, ...(context ?? {}) }),
  }, { unavailableReturnsNull: false });
  const record = asRecord(result);
  if (!record) return null;
  return {
    committed: readBoolean(record, ["committed"]),
    blocked: readBoolean(record, ["blocked"]),
    reason: readString(record, ["reason"]),
    approval_required: readBoolean(record, ["approval_required"]),
    approval_request_id: readString(record, ["approval_request_id"]),
    display_summary: readString(record, ["display_summary"]),
    commit: asRecord(record.commit) ?? undefined,
    seal: normalizeSeal(record.seal),
    review: normalizeReview(record.change_request ?? record.review),
  };
}

export async function exportChangeRequestPatch(reviewId: string): Promise<{ filename?: string; patch?: string; patch_bytes?: number } | null> {
  const result = await requestChangeRequest<unknown>(defaultspackContractRoute(`api/change-requests/${encodeURIComponent(reviewId)}/export-patch`), {
    method: "POST",
  });
  const record = asRecord(result);
  if (!record) return null;
  return {
    filename: readString(record, ["filename"]),
    patch: readString(record, ["patch"]),
    patch_bytes: readNumber(record, ["patch_bytes", "patchBytes"]),
  };
}
