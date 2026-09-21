import type { CodingApprovalRequest } from "./api";
import type { AuthorityApproval } from "./authorityApproval";
import type { BrowserApproval, RuntimeApproval } from "./browserApproval";

export type ApprovalSource = "browser" | "runtime" | "authority" | "coding";
export type ApprovalStatus = "pending" | "approving" | "denying" | "approved" | "denied" | "expired" | "stale" | "error";

export type ApprovalViewModel = {
  id: string;
  source: ApprovalSource;
  title: string;
  consequence: string;
  reason: string;
  target: string;
  riskExplanation: string;
  scope: string;
  persistence: string;
  auditText: string;
  technicalDetails: Record<string, unknown>;
  status: ApprovalStatus;
  trustedWindowRequired?: boolean;
};

function text(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function first(payload: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = text(payload[key]);
    if (value) return value;
  }
  return "このリクエストで示された対象";
}

function humanAction(value: string): string {
  const normalized = value.replace(/[._-]+/g, " ").trim();
  return normalized || "操作を実行";
}

function riskExplanation(level?: string): string {
  const normalized = text(level).toLowerCase();
  if (normalized === "critical" || normalized === "blocked") return "端末外や重要なデータに影響する可能性があるため、内容を十分に確認してください。";
  if (normalized === "high") return "データの変更・送信を伴う可能性があります。対象と影響を確認してください。";
  if (normalized === "medium") return "外部サービスへのアクセスまたはローカルデータの変更を伴う可能性があります。";
  return "この操作は実行前にあなたの明示的な判断が必要です。";
}

export function browserApprovalViewModel(approval: BrowserApproval): ApprovalViewModel {
  const target = first(approval.payload, ["url", "target", "path", "selector", "tab_id", "query"]);
  return {
    id: approval.requestId || `${approval.toolName}:${approval.action}`,
    source: "browser",
    title: `ブラウザで「${humanAction(approval.action)}」を実行`,
    consequence: approval.summary || `ブラウザまたは画面上で ${humanAction(approval.action)} を実行します。`,
    reason: "会話で依頼された作業を続けるために必要です。",
    target,
    riskExplanation: riskExplanation(approval.riskLevel),
    scope: "この1回の操作のみ",
    persistence: "次の操作には再度確認が必要です。",
    auditText: "判断と対象はローカルの監査記録に残ります。",
    technicalDetails: { request_id: approval.requestId, tool: approval.toolName, operation: approval.action, payload: approval.payload },
    status: "pending",
  };
}

export function runtimeApprovalViewModel(approval: RuntimeApproval): ApprovalViewModel {
  return {
    id: approval.requestId,
    source: "runtime",
    title: `「${humanAction(approval.operation)}」を実行`,
    consequence: approval.summary || `${humanAction(approval.operation)} によりローカル環境が変更される可能性があります。`,
    reason: "会話で依頼された作業を続けるために必要です。",
    target: first(approval.payload, ["path", "cwd", "command", "repository", "url", "target"]),
    riskExplanation: riskExplanation(approval.riskLevel),
    scope: "この1回の操作のみ",
    persistence: "承認はこのリクエストにだけ使用されます。",
    auditText: "判断と操作内容はローカルの監査記録に残ります。",
    technicalDetails: { request_id: approval.requestId, tool: approval.toolName, operation: approval.operation, payload: approval.payload },
    status: "pending",
  };
}

export function authorityApprovalViewModel(approval: AuthorityApproval, title: string): ApprovalViewModel {
  return {
    id: approval.requestId,
    source: "authority",
    title: `${title} を利用`,
    consequence: approval.summary || approval.reason || "モデル、API、または端末の保護された機能を利用します。",
    reason: approval.reason || "会話で依頼された処理を続けるために必要です。",
    target: first(approval.resource, ["model_id", "provider_id", "domain", "endpoint_url", "path", "host_action", "operation"]),
    riskExplanation: riskExplanation(approval.riskLevel),
    scope: "専用画面で許可範囲を選択",
    persistence: "選んだ範囲と期限に従います。",
    auditText: "判断、許可範囲、対象はローカルの監査記録に残ります。",
    technicalDetails: { request_id: approval.requestId, principal_id: approval.principalId, permission_id: approval.permissionId, risk_level: approval.riskLevel, resource: approval.resource },
    status: "pending",
    trustedWindowRequired: true,
  };
}

export function codingApprovalViewModel(request: CodingApprovalRequest, now = Date.now()): ApprovalViewModel {
  const expiresAt = request.expires_at ? (request.expires_at > 1e12 ? request.expires_at : request.expires_at * 1000) : null;
  const status = request.status === "pending" && expiresAt && expiresAt <= now ? "expired" : request.status;
  const details = request.details ?? {};
  const presentedDetails = request.operation === "tool.mcp_connect"
    ? { mcp_review: details.mcp_review ?? details.review ?? details.connection_review ?? {} }
    : details;
  return {
    id: request.request_id,
    source: "coding",
    title: `「${humanAction(request.operation)}」を実行`,
    consequence: request.display_summary || `${humanAction(request.operation)} によりワークスペースが変更される可能性があります。`,
    reason: "Coding workspace で依頼された作業を続けるために必要です。",
    target: first(details, ["path", "cwd", "command", "repository", "url", "target"]),
    riskExplanation: riskExplanation(request.risk_level),
    scope: "この1回の操作のみ",
    persistence: "承認はこのリクエストにだけ使用されます。",
    auditText: "判断と操作内容はローカルの監査記録に残ります。",
    technicalDetails: { request_id: request.request_id, operation: request.operation, risk_level: request.risk_level, args_hash: request.args_hash, details: presentedDetails },
    status: (status === "approved" || status === "denied" || status === "expired" || status === "pending") ? status : "stale",
  };
}
