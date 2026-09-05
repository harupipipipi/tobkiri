import type { ChatUiMessage } from "../renderers/types";

export type BrowserApproval = {
  action: string;
  payload: Record<string, unknown>;
  token?: string;
  requestId?: string;
  riskLevel?: string;
  summary?: string;
  toolCallId?: string;
  toolName: string;
};

export type RuntimeApproval = {
  action: string;
  operation: string;
  payload: Record<string, unknown>;
  requestId: string;
  riskLevel?: string;
  summary?: string;
  toolCallId?: string;
  toolName: string;
};

export type StaleRuntimeApproval = {
  operation: string;
  payload: Record<string, unknown>;
  reason: string;
  riskLevel?: string;
  summary?: string;
  toolCallId?: string;
  toolName: string;
};

const BROWSER_COMPUTER_TOOL_NAMES = new Set([
  "browser_computer",
  "browser_companion",
  "browser_use",
  "computer_use",
  "browser_open_url",
  "open_browser",
]);

const BROWSER_OPEN_TOOL_ALIASES = new Set(["browser_open_url", "open_browser"]);
const BROWSER_OPEN_ACTION_ALIASES = new Set(["browser_open_url", "open_browser", "open_url"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isAuthorityApprovalCandidate(candidate: Record<string, unknown> | undefined): boolean {
  return Boolean(
    candidate
    && (
      candidate.authority
      || candidate.approval_kind === "authority"
      || candidate.permission_id === "model.invoke"
      || candidate.permission_id === "api_key.use"
      || candidate.permission_id === "network.egress"
    ),
  );
}

function numericTimestamp(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 10_000_000_000 ? value : value * 1000;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numericTimestamp(numeric);
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function approvalExpired(candidate: Record<string, unknown>, observedAt: unknown, now: number): boolean {
  const expiresAt = numericTimestamp(candidate.expires_at);
  if (expiresAt !== null) return expiresAt <= now;

  const expiresInSeconds = Number(candidate.approval_expires_in_seconds);
  const timestamp = numericTimestamp(candidate.timestamp ?? observedAt);
  if (Number.isFinite(expiresInSeconds) && expiresInSeconds > 0 && timestamp !== null) {
    return timestamp + expiresInSeconds * 1000 <= now;
  }
  return false;
}

function approvalFromCandidate(
  candidate: Record<string, unknown> | undefined,
  fallbackToolName = "browser_computer",
  fallbackToolCallId: string | undefined,
  fallbackPayload: Record<string, unknown> | undefined,
  observedAt: unknown,
  now: number,
): BrowserApproval | null {
  if (!candidate || isAuthorityApprovalCandidate(candidate)) return null;
  if (!candidate?.requires_approval && !candidate?.approval_required) return null;
  const requestId = requestIdFromCandidate(candidate);
  const rawToken = typeof candidate.approval_token === "string" ? candidate.approval_token.trim() : "";
  const token = rawToken && rawToken !== "[redacted]" ? rawToken : "";
  if (!token && !requestId) return null;
  if (approvalExpired(candidate, observedAt, now)) return null;
  const rawPayload = isRecord(candidate.payload)
    ? candidate.payload
    : isRecord(candidate.arguments)
      ? candidate.arguments
      : fallbackPayload;
  const rawToolName = String(candidate.tool_name ?? fallbackToolName);
  const rawAction = String(candidate.action ?? candidate.operation ?? "browser.session");
  const approval: BrowserApproval = {
    action: BROWSER_OPEN_ACTION_ALIASES.has(rawAction) ? "browser.open_url" : rawAction,
    payload: isRecord(rawPayload) ? rawPayload : {},
    toolName: BROWSER_OPEN_TOOL_ALIASES.has(rawToolName) ? "browser_computer" : rawToolName,
  };
  if (token) approval.token = token;
  if (requestId) approval.requestId = requestId;
  if (typeof candidate.risk_level === "string") approval.riskLevel = candidate.risk_level;
  if (typeof candidate.display_summary === "string") {
    approval.summary = candidate.display_summary;
  } else if (typeof candidate.message === "string") {
    approval.summary = candidate.message;
  }
  if (typeof candidate.tool_call_id === "string") approval.toolCallId = candidate.tool_call_id;
  return approval;
}

function requestIdFromCandidate(candidate: Record<string, unknown> | undefined): string {
  return String(candidate?.approval_request_id ?? candidate?.request_id ?? "").trim();
}

function matchingApprovalToolLog(
  message: ChatUiMessage,
  candidate: Record<string, unknown>,
) {
  const requestId = requestIdFromCandidate(candidate);
  const logs = [...(message.toolLogs ?? [])].reverse();
  const exact = requestId ? logs.find((log) => {
    const result = isRecord(log.result) ? log.result : undefined;
    const data = isRecord(result?.data) ? result.data : result;
    const widget = isRecord(data?.widget) ? data.widget : undefined;
    const logCandidate = (widget?.requires_approval || widget?.approval_required ? widget : data) as Record<string, unknown> | undefined;
    return requestIdFromCandidate(logCandidate) === requestId;
  }) : undefined;
  if (exact) return exact;
  return logs.find((log) => {
    const name = String(log.tool_name ?? "").trim();
    return Boolean(name && name !== "tool");
  });
}

function matchingApprovalToolEvent(
  message: ChatUiMessage,
  candidate: Record<string, unknown>,
) {
  const runId = String(candidate.run_id ?? "").trim();
  return [...(message.events ?? [])].reverse().find((event) => (
    typeof event.tool_name === "string"
    && event.tool_name.trim()
    && (!runId || String(event.run_id ?? "").trim() === runId)
    && (event.type === "tool_call_started" || event.phase === "tool_call_started")
  ));
}

function runtimeApprovalFromCandidate(
  candidate: Record<string, unknown> | undefined,
  fallbackToolName = "tool",
  fallbackToolCallId?: string,
  fallbackPayload?: Record<string, unknown>,
  observedAt?: unknown,
  now = Date.now(),
): RuntimeApproval | null {
  const requestId = requestIdFromCandidate(candidate);
  if (!candidate || !requestId) return null;
  if (isAuthorityApprovalCandidate(candidate)) return null;
  if (!candidate.requires_approval && !candidate.approval_required) return null;
  if (approvalExpired(candidate, observedAt, now)) return null;
  const candidateToolName = String(candidate.tool_name ?? "").trim();
  const toolName = (
    candidateToolName && candidateToolName !== "tool"
      ? candidateToolName
      : fallbackToolName
  ).trim() || "tool";
  const payload = isRecord(candidate.payload)
    ? candidate.payload
    : isRecord(candidate.arguments)
      ? candidate.arguments
      : fallbackPayload
        ? fallbackPayload
      : {};
  return {
    action: String(candidate.action ?? candidate.operation ?? toolName),
    operation: String(candidate.operation ?? candidate.action ?? toolName),
    payload,
    requestId,
    riskLevel: typeof candidate.risk_level === "string" ? candidate.risk_level : undefined,
    summary: typeof candidate.display_summary === "string"
      ? candidate.display_summary
      : typeof candidate.message === "string"
        ? candidate.message
        : undefined,
    toolCallId: typeof candidate.tool_call_id === "string" ? candidate.tool_call_id : fallbackToolCallId,
    toolName,
  };
}

function staleRuntimeApprovalFromCandidate(
  candidate: Record<string, unknown> | undefined,
  fallbackToolName = "tool",
  fallbackToolCallId?: string,
  fallbackPayload?: Record<string, unknown>,
  observedAt?: unknown,
  now = Date.now(),
): StaleRuntimeApproval | null {
  if (!candidate || isAuthorityApprovalCandidate(candidate)) return null;
  if (!candidate.requires_approval && !candidate.approval_required) return null;
  if (requestIdFromCandidate(candidate)) return null;
  if (approvalExpired(candidate, observedAt, now)) return null;
  const toolName = String(candidate.tool_name ?? fallbackToolName).trim() || fallbackToolName;
  const payload = isRecord(candidate.payload)
    ? candidate.payload
    : isRecord(candidate.arguments)
      ? candidate.arguments
      : fallbackPayload
        ? fallbackPayload
        : {};
  return {
    operation: String(candidate.operation ?? candidate.action ?? toolName),
    payload,
    reason: "missing_approval_request_id",
    riskLevel: typeof candidate.risk_level === "string" ? candidate.risk_level : undefined,
    summary: typeof candidate.display_summary === "string"
      ? candidate.display_summary
      : typeof candidate.message === "string"
        ? candidate.message
        : undefined,
    toolCallId: typeof candidate.tool_call_id === "string" ? candidate.tool_call_id : fallbackToolCallId,
    toolName,
  };
}

export function pendingBrowserApproval(messages: ChatUiMessage[], now = Date.now()): BrowserApproval | null {
  for (const message of [...messages].reverse()) {
    if (message.role === "user") return null;
    if (message.role !== "agent") continue;
    for (const event of [...(message.events ?? [])].reverse()) {
      if (event.type !== "approval_requested" && event.phase !== "approval_requested") continue;
      const approval = approvalFromCandidate(
        event as Record<string, unknown>,
        String(event.tool_name ?? "browser_computer"),
        typeof event.tool_call_id === "string" ? event.tool_call_id : undefined,
        undefined,
        event.timestamp,
        now,
      );
      if (approval) return approval;
    }
    for (const log of [...(message.toolLogs ?? [])].reverse()) {
      if (!BROWSER_COMPUTER_TOOL_NAMES.has(String(log.tool_name))) continue;
      const result = isRecord(log.result) ? log.result : undefined;
      const data = isRecord(result?.data) ? result.data : result;
      const widget = isRecord(data?.widget) ? data.widget : undefined;
      const candidate = (widget?.requires_approval || widget?.approval_required ? widget : data) as Record<string, unknown> | undefined;
      const approval = approvalFromCandidate(
        candidate,
        String(log.tool_name),
        typeof log.tool_call_id === "string" ? log.tool_call_id : undefined,
        isRecord(log.arguments) ? log.arguments : undefined,
        log.timestamp,
        now,
      );
      if (approval) return approval;
    }
  }
  return null;
}

export function browserApprovalToolArguments(approval: BrowserApproval, token?: string): Record<string, unknown> {
  const payload = {
    ...approval.payload,
    ...((token || approval.token) ? { approval_token: token || approval.token } : {}),
  };
  if (approval.toolName === "browser_computer") {
    return {
      action: approval.action,
      payload,
    };
  }
  return {
    action: approval.action,
    ...payload,
  };
}

export function browserApprovalRuntimeContent(approval: BrowserApproval, token?: string): string {
  void token;
  return [
    "The user approved the pending browser/computer operation.",
    "Continue with the exact pending tool once. The server attached the approval token out-of-band.",
    "Do not ask the user for the same approval again unless the tool returns a new approval_request_id.",
    `Tool: ${approval.toolName}`,
    `Operation: ${approval.action}`,
    approval.requestId ? `Approval request id: ${approval.requestId}` : "",
    "Approved arguments JSON (no token):",
    JSON.stringify(approval.payload, null, 2),
  ].filter(Boolean).join("\n");
}

export function pendingRuntimeApproval(messages: ChatUiMessage[], now = Date.now()): RuntimeApproval | null {
  for (const message of [...messages].reverse()) {
    if (message.role === "user") return null;
    if (message.role !== "agent") continue;
    const metadataApproval = message.metadata?.pendingApproval;
    const metadataMatchingLog = matchingApprovalToolLog(message, metadataApproval ?? {});
    const metadataMatchingEvent = matchingApprovalToolEvent(message, metadataApproval ?? {});
    const metadataRuntimeApproval = runtimeApprovalFromCandidate(
      metadataApproval,
      String(
        (
          typeof metadataApproval?.tool_name === "string"
          && metadataApproval.tool_name.trim()
          && metadataApproval.tool_name.trim() !== "tool"
        )
          ? metadataApproval.tool_name
          : metadataMatchingLog?.tool_name ?? metadataMatchingEvent?.tool_name ?? "tool",
      ),
      typeof metadataApproval?.tool_call_id === "string"
        ? metadataApproval.tool_call_id
        : typeof metadataMatchingLog?.tool_call_id === "string"
          ? metadataMatchingLog.tool_call_id
          : typeof metadataMatchingEvent?.tool_call_id === "string"
            ? metadataMatchingEvent.tool_call_id
            : undefined,
      metadataMatchingLog && isRecord(metadataMatchingLog.arguments)
        ? metadataMatchingLog.arguments
        : undefined,
      metadataApproval?.timestamp,
      now,
    );
    if (metadataRuntimeApproval) return metadataRuntimeApproval;
    for (const event of [...(message.events ?? [])].reverse()) {
      if (event.type !== "approval_requested" && event.phase !== "approval_requested") continue;
      const matchingLog = matchingApprovalToolLog(message, event as Record<string, unknown>);
      const matchingEvent = matchingApprovalToolEvent(message, event as Record<string, unknown>);
      const approval = runtimeApprovalFromCandidate(
        event as Record<string, unknown>,
        String(
          (
            typeof event.tool_name === "string"
            && event.tool_name.trim()
            && event.tool_name.trim() !== "tool"
          )
            ? event.tool_name
            : matchingLog?.tool_name ?? matchingEvent?.tool_name ?? "tool",
        ),
        typeof event.tool_call_id === "string"
          ? event.tool_call_id
          : typeof matchingLog?.tool_call_id === "string"
            ? matchingLog.tool_call_id
            : typeof matchingEvent?.tool_call_id === "string"
              ? matchingEvent.tool_call_id
            : undefined,
        matchingLog && isRecord(matchingLog.arguments) ? matchingLog.arguments : undefined,
        event.timestamp,
        now,
      );
      if (approval) return approval;
    }
    for (const log of [...(message.toolLogs ?? [])].reverse()) {
      const result = isRecord(log.result) ? log.result : undefined;
      const data = isRecord(result?.data) ? result.data : result;
      const widget = isRecord(data?.widget) ? data.widget : undefined;
      const candidate = (widget?.requires_approval || widget?.approval_required ? widget : data) as Record<string, unknown> | undefined;
      const fallbackPayload = isRecord(log.arguments) ? log.arguments : undefined;
      const approval = runtimeApprovalFromCandidate(
        candidate,
        String(log.tool_name ?? "tool"),
        typeof log.tool_call_id === "string" ? log.tool_call_id : undefined,
        fallbackPayload,
        log.timestamp,
        now,
      );
      if (approval) return approval;
    }
  }
  return null;
}

export function staleRuntimeApproval(messages: ChatUiMessage[], now = Date.now()): StaleRuntimeApproval | null {
  for (const message of [...messages].reverse()) {
    if (message.role === "user") return null;
    if (message.role !== "agent") continue;
    const metadataApproval = message.metadata?.pendingApproval;
    const metadataStale = staleRuntimeApprovalFromCandidate(
      metadataApproval,
      String(metadataApproval?.tool_name ?? "tool"),
      typeof metadataApproval?.tool_call_id === "string" ? metadataApproval.tool_call_id : undefined,
      undefined,
      metadataApproval?.timestamp,
      now,
    );
    if (metadataStale) return metadataStale;
    for (const event of [...(message.events ?? [])].reverse()) {
      if (event.type !== "approval_requested" && event.phase !== "approval_requested") continue;
      const stale = staleRuntimeApprovalFromCandidate(
        event as Record<string, unknown>,
        String(event.tool_name ?? "tool"),
        typeof event.tool_call_id === "string" ? event.tool_call_id : undefined,
        undefined,
        event.timestamp,
        now,
      );
      if (stale) return stale;
    }
    for (const log of [...(message.toolLogs ?? [])].reverse()) {
      const result = isRecord(log.result) ? log.result : undefined;
      const data = isRecord(result?.data) ? result.data : result;
      const widget = isRecord(data?.widget) ? data.widget : undefined;
      const candidate = (widget?.requires_approval || widget?.approval_required ? widget : data) as Record<string, unknown> | undefined;
      const fallbackPayload = isRecord(log.arguments) ? log.arguments : undefined;
      const stale = staleRuntimeApprovalFromCandidate(
        candidate,
        String(log.tool_name ?? "tool"),
        typeof log.tool_call_id === "string" ? log.tool_call_id : undefined,
        fallbackPayload,
        log.timestamp,
        now,
      );
      if (stale) return stale;
    }
  }
  return null;
}
