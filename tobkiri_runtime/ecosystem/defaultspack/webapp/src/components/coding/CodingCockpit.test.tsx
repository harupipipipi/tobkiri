import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ApprovalQueue } from "./ApprovalQueue";
import { CheckpointPanel } from "./CheckpointPanel";
import { ChangeReviewChecksTab } from "./ChangeReviewChecksTab";
import { CodingCockpit } from "./CodingCockpit";
import { DiffPanel } from "./DiffPanel";
import { TERMINAL_HISTORY_POLICY, TerminalPanel } from "./TerminalPanel";
import { codingApprovalRequestId } from "./CheckpointPanel";
import {
  codingActionRequiresApproval,
  nextApprovalQueueRefreshSignal,
} from "./approvalQueueSync";

test("approval-required coding results advance the queue refresh signal", () => {
  assert.equal(codingActionRequiresApproval({ approval_required: true }), true);
  assert.equal(codingActionRequiresApproval({ approval_request: { request_id: "apr_restore" } }), true);
  assert.equal(codingActionRequiresApproval({ approval_required: false }), false);
  assert.equal(nextApprovalQueueRefreshSignal(3, { approval_required: true }), 4);
  assert.equal(nextApprovalQueueRefreshSignal(3, { ok: true }), 3);
});

test("checkpoint restore resolves approval request ids from both response shapes", () => {
  assert.equal(codingApprovalRequestId({ approval_request_id: "apr_direct" }), "apr_direct");
  assert.equal(
    codingApprovalRequestId({ approval_request: { request_id: "apr_nested" } }),
    "apr_nested",
  );
  assert.equal(codingApprovalRequestId({ restored: true }), "");
});

test("approval queue renders cockpit approval decisions", () => {
  const html = renderToStaticMarkup(
    createElement(ApprovalQueue, {
      initialApprovals: [
        {
          request_id: "apr_1",
          operation: "terminal.exec",
          risk_level: "high",
          status: "pending",
          display_summary: "terminal.exec: git push origin main",
        },
      ],
    }),
  );

  assert.match(html, /terminal\.exec/);
  assert.match(html, /許可/);
  assert.match(html, /拒否/);
});

test("approval queue separates expired pending approvals from active approvals", () => {
  const html = renderToStaticMarkup(
    createElement(ApprovalQueue, {
      initialApprovals: [
        {
          request_id: "apr_expired",
          operation: "terminal.exec",
          risk_level: "medium",
          status: "pending",
          display_summary: "terminal.exec: old command",
          expires_at: 1,
        },
      ],
    }),
  );

  assert.match(html, /Active pending approvals/);
  assert.match(html, />0<\/span>/);
  assert.match(html, /No active approvals/);
  assert.match(html, /Recent approval history/);
  assert.match(html, /expired/);
  assert.doesNotMatch(html, />許可</);
  assert.doesNotMatch(html, />拒否</);
});

test("diff panel renders status, content, and an operable refresh control", () => {
  const html = renderToStaticMarkup(
    createElement(DiffPanel, {
      initialStatus: { branch: "main", clean: false, modified: ["src/App.tsx"] },
      initialDiff: { diff: "-old\n+new", files_changed: 1, files: ["src/App.tsx"] },
    }),
  );

  assert.match(html, /main/);
  assert.match(html, /src\/App\.tsx/);
  assert.match(html, /-old/);
  assert.match(html, /\+new/);
  assert.match(html, /aria-label="Refresh diff"/);
});

test("checkpoint panel renders refresh and restore-review controls for supplied snapshots", () => {
  const html = renderToStaticMarkup(
    createElement(CheckpointPanel, {
      workspaceId: "ws-main",
      initialCheckpoints: [
        {
          snapshot_id: "snapshot-1",
          path: "/repo/.rumi/checkpoints/snapshot-1",
        },
      ],
      initialDiff: { diff: "-before\n+after", files_changed: 1, files: ["src/App.tsx"] },
    }),
  );

  assert.match(html, /snapshot-1/);
  assert.match(html, /Refresh checkpoints/);
  assert.match(html, /Review restore snapshot-1/);
  assert.match(html, /Restore diff/);
  assert.match(html, /-before/);
});

test("failed review checks keep their failure icon and expose a separate copy action", () => {
  const html = renderToStaticMarkup(
    createElement(ChangeReviewChecksTab, {
      review: {
        id: "review-1",
        status: "open",
        checks: [{ id: "check-1", command: "python -m pytest", status: "failed", stderr_tail: "AssertionError" }],
      },
      actionBusy: null,
      checkCommand: "",
      onCheckCommandChange: () => undefined,
      onReloadChecks: () => undefined,
      onRunCheck: () => undefined,
    }),
  );

  assert.match(html, /AssertionError/);
  assert.match(html, /aria-label="失敗したチェックをコピー"/);
  assert.match(html, /data-copy-icon=""/);
});

test("terminal panel starts empty and accepts no initial history", () => {
  const html = renderToStaticMarkup(
    createElement(TerminalPanel, {}),
  );

  assert.match(html, /No terminal runs/);
  assert.match(html, /Memory only/);
  assert.match(html, /not saved to browser storage/);
  assert.match(html, /aria-label="Clear terminal history from this private session"/);
  assert.equal(TERMINAL_HISTORY_POLICY.durable, false);
});

test("terminal history never reads or writes raw browser storage", () => {
  const source = readFileSync(resolve(import.meta.dirname, "TerminalPanel.tsx"), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB/);
  assert.doesNotMatch(source, /approval_request_id\s*===/);
  assert.match(source, /sessionPendingApprovals\.current\.get/);
});

test("coding cockpit renders workspace and sidecar sections", () => {
  const html = renderToStaticMarkup(
    createElement(CodingCockpit, {
      workspaces: [{ workspace_id: "ws-main", label: "Main Repo", root_path: "/repo", trusted: true }],
      selectedWorkspaceId: "ws-main",
    }),
  );

  assert.match(html, /Coding Cockpit/);
  assert.match(html, /Main Repo/);
  assert.match(html, /Approvals/);
  assert.match(html, /Checkpoints/);
  assert.match(html, /Terminal/);
  assert.match(html, /Browser/);
  assert.match(html, /MCP/);
  assert.match(html, /Agents/);
});

test("MCP requester never approves its own request", () => {
  const source = readFileSync(resolve(import.meta.dirname, "CodingCockpit.tsx"), "utf8");
  assert.doesNotMatch(source, /codingResources\.approveCodingApproval/);
  assert.match(source, /Review the shared approval request below/);
  assert.match(source, /onApproved=\{handleApprovalApproved\}/);
  assert.match(source, /approval_token: decision\.token/);
});
