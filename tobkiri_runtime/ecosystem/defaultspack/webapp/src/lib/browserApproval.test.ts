import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

import type { ChatUiMessage } from "../renderers/types";
import { browserApprovalRuntimeContent, pendingBrowserApproval, pendingRuntimeApproval, staleRuntimeApproval } from "./browserApproval";

function agentMessage(patch: Partial<ChatUiMessage>): ChatUiMessage {
  return {
    id: "m1",
    role: "agent",
    content: [],
    rawText: "",
    events: [],
    toolLogs: [],
    ...patch,
  };
}

function appSource(): string {
  return readFileSync(resolve(import.meta.dirname, "..", "App.tsx"), "utf8").replace(/\r\n/g, "\n");
}

test("chat browser approval card exposes deny and settles stale request-backed cards", () => {
  const source = appSource();
  const cardStart = source.indexOf("{visibleBrowserApproval && (");
  const cardEnd = source.indexOf("{!visibleBrowserApproval && authorityApproval", cardStart);
  const browserCardSource = source.slice(cardStart, cardEnd);

  assert.match(source, /const denyBrowserAction = async \(\) => \{/);
  assert.match(source, /await api\.denyCodingApproval\(currentApproval\.requestId, "User denied the request from the shared approval surface"\)/);
  assert.match(source, /function browserApprovalSettlementKey\(approval: BrowserApproval\): string/);
  assert.match(source, /const staleMessage = currentApproval\.requestId \? approvalStaleUiMessage\(approvalError\) : null/);
  assert.match(source, /settleBrowserApproval\(currentApproval\)/);
  assert.match(browserCardSource, /<ApprovalDecisionSurface/);
  assert.match(browserCardSource, /approval=\{browserApprovalViewModel\(visibleBrowserApproval\)\}/);
  assert.match(browserCardSource, /onDeny=\{\(\) => void denyBrowserAction\(\)\}/);
  assert.match(browserCardSource, /onApprove=\{\(\) => void approveBrowserAction\(\)\}/);
  assert.match(browserCardSource, /keyboardShortcuts=\{\{ deny: "2", approve: "3" \}\}/);
});

test("browser approval follow-up resumes only the approved pending tool once", () => {
  const source = appSource();
  const approveStart = source.indexOf("const approveBrowserAction = async () => {");
  const approveEnd = source.indexOf("const denyBrowserAction = async () => {", approveStart);
  const approveSource = source.slice(approveStart, approveEnd);

  assert.match(approveSource, /const actionKey = browserApprovalSettlementKey\(currentApproval\)/);
  assert.match(approveSource, /if \(activeBrowserApprovalActionRef\.current === actionKey\) return/);
  assert.match(approveSource, /activeBrowserApprovalActionRef\.current = actionKey/);
  assert.match(approveSource, /browserApprovalTokenRef\.current\.get\(actionKey\)/);
  assert.match(approveSource, /browserApprovalTokenRef\.current\.set\(actionKey, approvalToken\)/);
  assert.match(approveSource, /const approvalToolIds = \[currentApproval\.toolName\]\.filter\(Boolean\)/);
  assert.doesNotMatch(approveSource, /selectedToolIds\.length\s*\?\s*selectedToolIds/);
  assert.match(approveSource, /tools: approvalToolIds\.length \? approvalToolIds : undefined/);
  assert.match(approveSource, /selected_tools: approvalToolIds/);
  assert.ok(
    approveSource.indexOf("await api.streamMessage")
      < approveSource.indexOf("settleBrowserApproval(currentApproval)"),
    "the approval card must remain recoverable until the replay request completes",
  );
  assert.match(approveSource, /activeBrowserApprovalActionRef\.current = null/);
});

test("returns a fresh browser computer approval request", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "computer_use",
        action: "computer.screenshot",
        payload: { app: "Google Chrome" },
        requires_approval: true,
        approval_token: "tok",
        approval_expires_in_seconds: 300,
        timestamp: "2026-05-20T08:05:40Z",
      }],
    }),
  ], Date.parse("2026-05-20T08:06:00Z"));

  assert.deepEqual(approval, {
    action: "computer.screenshot",
    payload: { app: "Google Chrome" },
    token: "tok",
    toolName: "computer_use",
  });
});

test("canonicalizes browser open approval aliases", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "browser_open_url",
        action: "open_url",
        payload: { url: "https://gemini.google.com" },
        requires_approval: true,
        approval_token: "tok",
        approval_expires_in_seconds: 300,
        timestamp: "2026-05-20T08:05:40Z",
      }],
    }),
  ], Date.parse("2026-05-20T08:06:00Z"));

  assert.deepEqual(approval, {
    action: "browser.open_url",
    payload: { url: "https://gemini.google.com" },
    token: "tok",
    toolName: "browser_computer",
  });
});

test("ignores expired browser computer approvals", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "computer_use",
        action: "computer.screenshot",
        payload: { app: "Google Chrome" },
        requires_approval: true,
        approval_token: "tok",
        approval_expires_in_seconds: 300,
        timestamp: "2026-05-20T08:05:40Z",
      }],
    }),
  ], Date.parse("2026-05-20T08:11:00Z"));

  assert.equal(approval, null);
});

test("ignores redacted approval tokens from stored tool logs", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      toolLogs: [{
        tool_name: "computer_use",
        timestamp: "2026-05-20T08:05:40Z",
        result: {
          status: "ok",
          data: {
            widget: {
              type: "browser_computer",
              action: "computer.screenshot",
              requires_approval: true,
              approval_token: "[redacted]",
              approval_expires_in_seconds: 300,
              payload: { app: "Google Chrome" },
            },
          },
        },
      }],
    }),
  ], Date.parse("2026-05-20T08:06:00Z"));

  assert.equal(approval, null);
});

test("accepts browser computer approvals backed by approval request ids", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "computer_use",
        action: "computer.screenshot",
        payload: { app: "Google Chrome" },
        requires_approval: true,
        approval_token: "tok",
        approval_request_id: "apr_browser",
        approval_expires_in_seconds: 300,
        timestamp: "2026-05-20T08:05:40Z",
      }],
    }),
  ], Date.parse("2026-05-20T08:06:00Z"));

  assert.deepEqual(approval, {
    action: "computer.screenshot",
    payload: { app: "Google Chrome" },
    token: "tok",
    requestId: "apr_browser",
    toolName: "computer_use",
  });
});

test("accepts request id browser approvals without legacy tokens", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "computer_use",
        action: "computer.apps",
        payload: { action: "apps" },
        requires_approval: true,
        approval_request_id: "apr_1",
        risk_level: "high",
        display_summary: "computer.apps",
      }],
    }),
  ]);

  assert.deepEqual(approval, {
    action: "computer.apps",
    payload: { action: "apps" },
    requestId: "apr_1",
    riskLevel: "high",
    summary: "computer.apps",
    toolName: "computer_use",
  });
});

test("does not treat model authority approvals as browser approvals", () => {
  const approval = pendingBrowserApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        approval_kind: "authority",
        authority: true,
        requires_approval: true,
        approval_request_id: "auth_model_1",
        principal_id: "conversation:abc",
        permission_id: "model.invoke",
        resource: {
          kind: "model",
          provider_id: "opencode-go",
          api_id: "legacy",
          model_id: "qwen3.5-plus",
          model_ref: "opencode-go/qwen3.5-plus",
        },
        risk_level: "medium",
        display_summary: "Model invocation: opencode-go/qwen3.5-plus",
      }],
    }),
  ]);

  assert.equal(approval, null);
});

test("browserApprovalRuntimeContent includes request id without exposing the token", () => {
  const text = browserApprovalRuntimeContent(
    {
      action: "computer.apps",
      payload: { action: "apps" },
      requestId: "apr_1",
      toolName: "computer_use",
    },
    "token-1",
  );

  assert.match(text, /computer_use/);
  assert.match(text, /computer\.apps/);
  assert.match(text, /Approval request id: apr_1/);
  assert.doesNotMatch(text, /approval_token/);
  assert.doesNotMatch(text, /token-1/);
});

test("returns pending runtime approval requests without browser tokens", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "coding_file_create",
        tool_call_id: "call_file",
        action: "coding_file_create",
        operation: "file.create",
        payload: { path: "index.html", content: "<html></html>" },
        requires_approval: true,
        approval_request_id: "apr_1",
        risk_level: "medium",
        display_summary: "Create index.html",
      }],
    }),
  ]);

  assert.deepEqual(approval, {
    action: "coding_file_create",
    operation: "file.create",
    payload: { path: "index.html", content: "<html></html>" },
    requestId: "apr_1",
    riskLevel: "medium",
    summary: "Create index.html",
    toolCallId: "call_file",
    toolName: "coding_file_create",
  });
});

test("uses the matching tool log when an approval event omits its tool name", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      events: [
        {
          type: "tool_call_started",
          phase: "tool_call_started",
          run_id: "run_repo",
          tool_name: "repository_context_prepare",
          tool_call_id: "call_repo",
        },
        {
          type: "approval_requested",
          phase: "approval_requested",
          run_id: "run_repo",
          action: "tool.repository_context_prepare",
          operation: "tool.repository_context_prepare",
          payload: { query: "ToolExecutor._execute_global_contract" },
          requires_approval: true,
          approval_request_id: "apr_repo",
        },
      ],
      toolLogs: [{
        tool_name: "repository_context_prepare",
        tool_call_id: "call_repo",
        arguments: { query: "ToolExecutor._execute_global_contract" },
        result: {
          status: "ok",
          data: {
            widget: {
              approval_required: true,
              approval_request_id: "[truncated depth]",
              operation: "tool.repository_context_prepare",
            },
          },
        },
      }],
    }),
  ]);

  assert.equal(approval?.toolName, "repository_context_prepare");
  assert.equal(approval?.toolCallId, "call_repo");
  assert.deepEqual(approval?.payload, {
    query: "ToolExecutor._execute_global_contract",
  });
});

test("returns runtime approval requests from assistant metadata", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      metadata: {
        pendingApproval: {
          tool_name: "coding_file_write",
          tool_call_id: "call_write",
          action: "coding_file_write",
          operation: "file.write",
          payload: { path: "probe.txt", content: "ok" },
          approval_required: true,
          approval_request_id: "apr_write",
          risk_level: "high",
          display_summary: "Write probe.txt",
        },
      },
    }),
  ]);

  assert.deepEqual(approval, {
    action: "coding_file_write",
    operation: "file.write",
    payload: { path: "probe.txt", content: "ok" },
    requestId: "apr_write",
    riskLevel: "high",
    summary: "Write probe.txt",
    toolCallId: "call_write",
    toolName: "coding_file_write",
  });
});

test("uses the matching tool event when metadata approval omits its tool name", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      metadata: {
        pendingApproval: {
          tool_name: "tool",
          action: "tool.repository_context_prepare",
          operation: "tool.repository_context_prepare",
          payload: { query: "ToolExecutor._execute_global_contract" },
          approval_required: true,
          approval_request_id: "apr_repo_metadata",
        },
      },
      events: [{
        type: "tool_call_started",
        phase: "tool_call_started",
        tool_name: "repository_context_prepare",
        tool_call_id: "call_repo_metadata",
      }],
    }),
  ]);

  assert.equal(approval?.toolName, "repository_context_prepare");
  assert.equal(approval?.toolCallId, "call_repo_metadata");
});

test("uses the only specific tool log when approval metadata is depth-truncated", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      metadata: {
        pendingApproval: {
          tool_name: "tool",
          action: "tool.repository_context_prepare",
          operation: "tool.repository_context_prepare",
          payload: { query: "ToolExecutor._execute_global_contract" },
          approval_required: true,
          approval_request_id: "apr_repo_truncated",
        },
      },
      toolLogs: [{
        tool_name: "repository_context_prepare",
        tool_call_id: "call_repo_truncated",
        arguments: { query: "ToolExecutor._execute_global_contract" },
        result: {
          data: {
            widget: {
              approval_required: "[truncated depth]",
              approval_request_id: "[truncated depth]",
            },
          },
        },
      }],
    }),
  ]);

  assert.equal(approval?.toolName, "repository_context_prepare");
  assert.equal(approval?.toolCallId, "call_repo_truncated");
});

test("returns generic browser tool approval requests when they use approval request ids", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      toolLogs: [{
        tool_name: "browser_computer",
        tool_call_id: "call_browser",
        arguments: { action: "computer.click", payload: { x: 10, y: 20 } },
        result: {
          widget: {
            type: "approval_request",
            tool_name: "browser_computer",
            approval_required: true,
            approval_request_id: "apr_browser",
            operation: "tool.browser_computer",
          },
        },
      }],
    }),
  ]);

  assert.equal(approval?.toolName, "browser_computer");
  assert.equal(approval?.requestId, "apr_browser");
  assert.deepEqual(approval?.payload, { action: "computer.click", payload: { x: 10, y: 20 } });
});

test("returns stale browser tool approvals when no token or request id exists", () => {
  const approval = staleRuntimeApproval([
    agentMessage({
      toolLogs: [{
        tool_name: "browser_computer",
        arguments: { action: "computer.click" },
        result: {
          widget: {
            type: "approval_request",
            tool_name: "browser_computer",
            approval_required: true,
            arguments: { action: "computer.click" },
          },
        },
      }],
    }),
  ]);

  assert.equal(approval?.toolName, "browser_computer");
  assert.equal(approval?.reason, "missing_approval_request_id");
});

test("uses tool log arguments as runtime approval payload fallback", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      toolLogs: [{
        tool_name: "coding_file_create",
        tool_call_id: "call_file",
        arguments: { path: "index.html", content: "<html></html>" },
        result: {
          status: "ok",
          data: {
            approval_required: true,
            approval_request_id: "apr_log",
            operation: "file.create",
          },
        },
      }],
    }),
  ]);

  assert.equal(approval?.requestId, "apr_log");
  assert.deepEqual(approval?.payload, { path: "index.html", content: "<html></html>" });
});

test("ignores expired runtime approval requests", () => {
  const approval = pendingRuntimeApproval([
    agentMessage({
      events: [{
        type: "approval_requested",
        tool_name: "coding_file_create",
        operation: "file.create",
        payload: { path: "index.html" },
        requires_approval: true,
        approval_request_id: "apr_expired",
        approval_expires_in_seconds: 10,
        timestamp: "2026-05-20T08:05:40Z",
      }],
    }),
  ], Date.parse("2026-05-20T08:06:00Z"));

  assert.equal(approval, null);
});

test("returns stale runtime approvals without actionable request ids", () => {
  const approval = staleRuntimeApproval([
    agentMessage({
      metadata: {
        pendingApproval: {
          tool_name: "coding_file_create",
          tool_call_id: "call_file",
          operation: "file.create",
          payload: { path: "index.html" },
          approval_required: true,
          risk_level: "medium",
        },
      },
      toolLogs: [{
        tool_name: "coding_file_create",
        tool_call_id: "call_file",
        arguments: { path: "index.html" },
        result: {
          widget: {
            type: "approval_request",
            approval_required: true,
            risk_level: "medium",
            arguments: { path: "index.html" },
          },
        },
      }],
    }),
  ]);

  assert.deepEqual(approval, {
    operation: "file.create",
    payload: { path: "index.html" },
    reason: "missing_approval_request_id",
    riskLevel: "medium",
    summary: undefined,
    toolCallId: "call_file",
    toolName: "coding_file_create",
  });
});

test("does not treat actionable runtime approvals as stale", () => {
  const approval = staleRuntimeApproval([
    agentMessage({
      toolLogs: [{
        tool_name: "coding_file_create",
        arguments: { path: "index.html" },
        result: {
          status: "ok",
          data: {
            approval_required: true,
            approval_request_id: "apr_log",
            operation: "file.create",
          },
        },
      }],
    }),
  ]);

  assert.equal(approval, null);
});
