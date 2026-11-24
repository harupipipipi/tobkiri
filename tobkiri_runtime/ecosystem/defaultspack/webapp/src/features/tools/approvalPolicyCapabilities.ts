import type { ActionApprovalMode } from "./ActionApprovalControl";

export type ApprovalPolicyApplicability = Readonly<{
  profile_id: string;
  activation_id: string;
  conversation_id: string | null;
  workspace_id: string;
}>;

/** Presentation facts only; the Host authenticates each operation separately. */
export type ApprovalPolicyCapabilities = Readonly<{
  available_modes: readonly ActionApprovalMode[];
  active_mode: ActionApprovalMode;
  reason: string;
  profile_id: string;
  activation_id: string;
  workspace_id: string;
  conversation_id: string | null;
  capture_digest: string;
  expires_at: number;
}>;

const RESPONSE_KEYS = ["available_modes", "active_mode", "reason", "profile_id",
  "activation_id", "workspace_id", "conversation_id", "capture_digest", "expires_at"];
const identifier = (value: unknown): value is string => typeof value === "string"
  && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
const mode = (value: unknown): value is ActionApprovalMode => value === "ask"
  || value === "agent" || value === "full";

/** Reject malformed, expired or rebound capability projections before display. */
export function parseApprovalPolicyCapabilities(
  value: unknown,
  captured: ApprovalPolicyApplicability,
  nowSeconds = Date.now() / 1000,
): ApprovalPolicyCapabilities {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("承認方式の状態を確認できません。操作は開始していません。");
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  const modes = record.available_modes;
  if (keys.length !== RESPONSE_KEYS.length || keys.some((key) => !RESPONSE_KEYS.includes(key))
    || !Array.isArray(modes) || modes.length < 1 || modes.length > 3
    || !modes.every(mode) || !modes.includes("ask") || new Set(modes).size !== modes.length
    || !mode(record.active_mode) || (captured.conversation_id === null && record.active_mode !== "ask")
    || typeof record.reason !== "string" || record.reason.length > 512
    || !identifier(record.profile_id) || record.profile_id !== captured.profile_id
    || (record.conversation_id !== null && !identifier(record.conversation_id))
    || record.conversation_id !== captured.conversation_id
    || !identifier(record.workspace_id) || record.workspace_id !== captured.workspace_id
    || !identifier(record.activation_id) || record.activation_id !== captured.activation_id
    || typeof record.capture_digest !== "string"
    || !/^sha256:[0-9a-f]{64}$/.test(record.capture_digest)
    || typeof record.expires_at !== "number" || !Number.isFinite(record.expires_at)
    || !Number.isFinite(nowSeconds) || record.expires_at <= nowSeconds
    || record.expires_at > nowSeconds + 900) {
    throw new Error("承認方式の状態が古いか、対象と一致しません。操作は開始していません。");
  }
  return Object.freeze({ ...record, available_modes: Object.freeze([...modes]) }) as ApprovalPolicyCapabilities;
}

/** Capture the target before awaiting and reject late results for a changed view. */
export async function loadApprovalPolicyCapabilities(
  target: ApprovalPolicyApplicability,
  transport: (target: ApprovalPolicyApplicability, signal?: AbortSignal) => Promise<unknown>,
  assertCurrent: () => void,
  signal?: AbortSignal,
): Promise<ApprovalPolicyCapabilities> {
  const captured = Object.freeze({ ...target });
  if (!identifier(captured.profile_id) || !identifier(captured.activation_id)
      || (captured.conversation_id !== null && !identifier(captured.conversation_id))
      || !identifier(captured.workspace_id) || signal?.aborted) {
    throw new Error("承認方式の確認対象を利用できません。");
  }
  assertCurrent();
  const result = await transport(captured, signal);
  if (signal?.aborted) throw new Error("承認方式の確認を取り消しました。");
  assertCurrent();
  return parseApprovalPolicyCapabilities(result, captured);
}
