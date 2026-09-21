import type {
  CodingApprovalDecision,
  CodingApprovalRequest,
  McpApprovalReview,
} from "../../lib/api";

export type McpConnectionDraft = {
  serverId: string;
  command: string;
  args: string[];
  workspaceId: string | null;
};

export type PendingMcpConnection = {
  requestId: string;
  draft: McpConnectionDraft;
};

export type McpReviewRow = {
  label: string;
  value: string;
};

const cleanString = (value: unknown): string => typeof value === "string" ? value.trim() : "";

const recordValue = (value: unknown): Record<string, unknown> | null => (
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
);

const displayValue = (value: unknown): string => {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(" ");
  if (value && typeof value === "object") return JSON.stringify(value);
  if (value === null || value === undefined || value === "") return "none";
  return String(value);
};

export function isMcpApprovalRequest(request: CodingApprovalRequest): boolean {
  return request.operation === "tool.mcp_connect";
}

export function sameMcpDraft(left: McpConnectionDraft, right: McpConnectionDraft): boolean {
  return left.serverId === right.serverId
    && left.command === right.command
    && left.workspaceId === right.workspaceId
    && JSON.stringify(left.args) === JSON.stringify(right.args);
}

export function approvedMcpRetryReason(
  pending: PendingMcpConnection | null,
  current: McpConnectionDraft,
  decision: CodingApprovalDecision,
): string | null {
  if (!pending || pending.requestId !== decision.request_id) {
    return "This MCP approval is stale or already settled. Review the configuration and connect again.";
  }
  if (!decision.approved || !decision.token) {
    return decision.reason || "The MCP connection was not approved. You can review it and connect again.";
  }
  if (!sameMcpDraft(pending.draft, current)) {
    return "The MCP configuration or workspace changed after review. Connect again to create a new approval request.";
  }
  return null;
}

/** Return only server-produced, explicitly redacted MCP review fields. */
export function mcpApprovalReview(request: CodingApprovalRequest): McpApprovalReview | null {
  if (!isMcpApprovalRequest(request)) return null;
  const details = recordValue(request.details);
  if (!details) return null;
  const review = (
    recordValue(details.mcp_review)
    ?? recordValue(details.review)
    ?? recordValue(details.connection_review)
  );
  return review as McpApprovalReview | null;
}

/** Build the fixed review table without ever falling back to raw config or env. */
export function mcpApprovalReviewRows(request: CodingApprovalRequest): McpReviewRow[] {
  const review = mcpApprovalReview(request);
  if (!review) return [];
  const executable = cleanString(review.executable ?? review.normalized_executable);
  const transport = cleanString(review.transport);
  const cwd = cleanString(review.cwd ?? review.normalized_cwd);
  const serverSource = cleanString(review.server_source ?? review.source);
  // `env` is accepted only inside the authority-owned review object. Raw
  // `details.config.env` is deliberately unreachable here.
  const redactedEnv = review.redacted_env ?? review.environment_redacted ?? review.env;
  const fields: Array<[string, unknown]> = [
    ["Executable", executable],
    ["Transport", transport],
    ["Arguments", review.args ?? review.normalized_args],
    ["Working directory", cwd],
    ["Environment (redacted)", redactedEnv],
    ["Server source", serverSource],
    ["Capabilities", review.capabilities],
    ["Tools", review.tools],
    ["Network", review.network],
    ["Filesystem", review.filesystem],
    ["Persistence", review.persistence],
    ["Consequences", review.consequences],
  ];
  return fields
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([label, value]) => ({ label, value: displayValue(value) }));
}
