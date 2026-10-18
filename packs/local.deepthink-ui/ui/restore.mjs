// Public diagnostic checks only. A browser reference never grants dispatch authority.
export const REQUEST_OPTIONS = Object.freeze({ timeoutMs: 300000 });
const VERSION = "deepthink.workflow.v5";
const HASH = /^[a-f0-9]{64}$/;
const DECIMAL = /^(0|[1-9][0-9]*)(\.[0-9]+)?$/;
const U64 = 18446744073709551615n;
const TOKEN_FIELDS = ["input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"];
const STATES = new Set(["paused", "awaiting_retry", "needs_input", "running", "completed", "quarantined", "needs_reconciliation"]);
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 4096;
const bounded = (value, max) => typeof value === "string" && value.length <= max && !!value.trim() && !/[\u0000-\u001f\u007f]/.test(value);
const check = (condition) => { if (!condition) throw new Error("保存状態の形式または参照が一致しません。再送せず参照を保持してください。"); };
const decimal = (value) => typeof value === "string" && value.length <= 256 && DECIMAL.test(value);

export const validRunId = (value) => typeof value === "string" && HASH.test(value);
export const validConversation = (value) => bounded(value, 256) && new TextEncoder().encode(value).length <= 256;

function counter(value) {
  if (Number.isSafeInteger(value) && value >= 0) return BigInt(value);
  if (typeof value === "string" && /^(0|[1-9][0-9]{0,19})$/.test(value)) {
    const number = BigInt(value);
    if (number <= U64) return number;
  }
  return null;
}

function usageState(value) {
  check(object(value) && object(value.tokens));
  for (const key of ["attempts", "pending_attempts", "tokens_unconfirmed_attempts", "content_incomplete_attempts", "invalid_resolution_attempts"]) {
    check(count(value[key]) && value[key] <= value.attempts);
  }
  check(typeof value.tokens_complete === "boolean" && typeof value.cost_known === "boolean");
  check(Object.keys(value.tokens).every((key) => TOKEN_FIELDS.includes(key)));
  check(Object.values(value.tokens).every((number) => counter(number) !== null));
  check(Array.isArray(value.tokens_overflow_fields) && value.tokens_overflow_fields.length <= 5
    && new Set(value.tokens_overflow_fields).size === value.tokens_overflow_fields.length
    && value.tokens_overflow_fields.every((key) => TOKEN_FIELDS.includes(key)));
  check(Array.isArray(value.cost_receipts_usd) && value.cost_receipts_usd.length <= value.attempts
    && value.cost_receipts_usd.every(decimal));
  check(value.cost_known ? decimal(value.cost_usd) && value.cost_receipts_usd.length === value.attempts : value.cost_usd === null);
  // Accounting completeness is diagnostic, not an extra browser admission rule.
  // Core status/reconciliation and the saved boundary decide continuation.
  return value.pending_attempts === 0;
}

export function validRetryBoundary(receipt, runId, conversation) {
  const keys = ["version", "retry_boundary_version", "run_id", "conversation", "status", "approved", "phase", "next_phase", "completed_phases", "completed_phases_this_segment", "reason", "rejected_attempt", "rejected_generation", "prepared_generation", "retry", "maximum_retries", "usage"];
  if (!object(receipt) || Object.keys(receipt).length !== keys.length || Object.keys(receipt).some((key) => !keys.includes(key))) return false;
  const rejected = bounded(receipt.rejected_generation, 512) ? /^(.+)#(0|[1-9][0-9]*)$/.exec(receipt.rejected_generation) : null;
  const epoch = rejected ? Number(rejected[2]) : NaN;
  const ordinal = bounded(receipt.rejected_attempt, 544) && receipt.rejected_attempt.startsWith(`${receipt.rejected_generation}:`)
    ? receipt.rejected_attempt.slice(receipt.rejected_generation.length + 1) : "";
  return receipt.version === VERSION && receipt.retry_boundary_version === "deepthink.contract-retry-boundary.v1"
    && receipt.status === "awaiting_retry" && receipt.approved === false && receipt.reason === "invalid_phase_contract"
    && receipt.conversation === conversation && receipt.run_id === runId
    && bounded(receipt.phase, 256) && receipt.next_phase === receipt.phase
    && count(receipt.completed_phases) && receipt.completed_phases_this_segment === 0
    && Number.isSafeInteger(receipt.retry) && receipt.retry >= 1
    && Number.isSafeInteger(receipt.maximum_retries) && receipt.retry <= receipt.maximum_retries && receipt.maximum_retries <= 3
    && !!rejected && rejected[1].startsWith(`${receipt.phase}@`)
    && Number.isSafeInteger(epoch) && Number.isSafeInteger(epoch + 1)
    && receipt.prepared_generation === `${rejected[1]}#${epoch + 1}`
    && /^(0|[1-9][0-9]*)$/.test(ordinal) && Number.isSafeInteger(Number(ordinal))
    && object(receipt.usage) && receipt.usage.pending_attempts === 0;
}

const canonical = (value) => JSON.stringify(value, (_key, item) => object(item)
  ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);

/** Read only public boundary metadata, never reconstruct a permit or private ledger. */
export function inspectStatus(value, runId, conversation) {
  check(validRunId(runId) && validConversation(conversation) && object(value));
  check(value.version === VERSION && value.run_id === runId && STATES.has(value.status));
  const head = value.repository;
  check(object(head) && head.format === "deepthink.run-ledger.v1" && head.run_id === runId
    && head.run_request_digest === runId && validRunId(head.snapshot_digest));
  check(typeof head.revision === "string" && /^[1-9][0-9]{0,19}$/.test(head.revision) && BigInt(head.revision) <= U64);
  check(Array.isArray(value.phases) && value.phases.length <= 4096
    && value.phases.every((item) => object(item) && bounded(item.phase, 256) && item.status === "completed"));
  for (const key of ["error", "reconciliation"]) {
    check(value[key] === null || (object(value[key]) && bounded(value[key].code, 128) && bounded(value[key].message, 4096)));
  }
  const settled = usageState(value.usage) && value.error === null && value.reconciliation === null;
  let resumable = false;
  let canAnswer = false;
  let explanation = "状態は参照用です。再開・再送はしていません。";
  if (value.status === "paused") {
    check(value.phases.length > 0);
    resumable = settled;
  } else if (value.status === "awaiting_retry") {
    check(validRetryBoundary(value.retry_boundary, runId, conversation));
    check(value.retry_boundary.completed_phases === value.phases.length);
    usageState(value.retry_boundary.usage);
    check(canonical(value.retry_boundary.usage) === canonical(value.usage));
    resumable = settled;
  } else if (value.status === "needs_input" && settled) {
    const questions = value.goal?.clarifications;
    canAnswer = Array.isArray(questions) && questions.length > 0 && questions.length <= 64
      && questions.every((question) => bounded(question, 4096));
  }
  if (resumable) explanation = "保存状態を読み取りました。続ける場合だけ「再開」を押してください。";
  if (!settled) explanation = "未確定の効果またはエラーがあります。再開せず参照を保持してください。";
  if (canAnswer) explanation = "確認事項への回答を入力できます。自動送信はしていません。";
  return { status: value.status, usage: value.usage, phaseCount: value.phases.length,
    revision: head.revision, snapshot: head.snapshot_digest, resumable, canAnswer, explanation };
}

/** Preserve exact integer/decimal strings; never fill missing counters with zero. */
export function statusUsage(value) {
  const display = (key) => counter(value.tokens[key]) === null ? "不明"
    : String(value.tokens[key]).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const cost = value.cost_known ? `$${value.cost_usd}` : "不明";
  return `入力 ${display("input_tokens")} / 出力 ${display("output_tokens")} / 合計 ${display("total_tokens")} / キャッシュ ${display("cached_input_tokens")} / 推論 ${display("reasoning_tokens")} tokens・費用 ${cost}${value.tokens_complete ? "" : "（確認済み分・未確定の使用量あり）"}`;
}
