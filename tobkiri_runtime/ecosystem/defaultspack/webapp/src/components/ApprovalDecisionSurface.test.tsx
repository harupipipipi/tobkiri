import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { authorityApprovalViewModel, browserApprovalViewModel, codingApprovalViewModel, runtimeApprovalViewModel } from "../lib/approvalPresentation";
import { ApprovalDecisionSurface } from "./ApprovalDecisionSurface";

test("all approval sources render the same user-first contract", () => {
  const models = [
    browserApprovalViewModel({ requestId: "b1", action: "click", payload: { url: "https://example.test" }, toolName: "browser_computer" }),
    runtimeApprovalViewModel({ requestId: "r1", action: "write", operation: "file.write", payload: { path: "/tmp/note" }, toolName: "coding" }),
    authorityApprovalViewModel({ requestId: "a1", principalId: "local", permissionId: "network.egress", resource: { domain: "example.test" } }, "外部接続"),
    codingApprovalViewModel({ request_id: "c1", operation: "terminal.exec", risk_level: "high", status: "pending", details: { command: "git push" } }),
  ];
  for (const model of models) {
    const html = renderToStaticMarkup(createElement(ApprovalDecisionSurface, { approval: model }));
    for (const label of ["Tobkiri が許可を求めています", "対象", "必要な理由", "影響とリスク", "許可範囲", "有効期間", "記録", "技術的な詳細"]) assert.match(html, new RegExp(label));
    assert.match(html, new RegExp(`data-approval-source="${model.source}"`));
  }
});

test("raw identifiers stay inside the closed technical disclosure", () => {
  const model = authorityApprovalViewModel({ requestId: "secret-request-id", principalId: "principal-internal", permissionId: "network.egress", resource: { domain: "example.test" } }, "外部接続");
  const html = renderToStaticMarkup(createElement(ApprovalDecisionSurface, { approval: model, onOpenTrustedWindow: () => undefined }));
  const disclosure = html.indexOf("技術的な詳細");
  assert.ok(disclosure > 0);
  assert.equal(html.slice(0, disclosure).includes("secret-request-id"), false);
  assert.equal(html.slice(0, disclosure).includes("principal-internal"), false);
  assert.match(html, /専用ウィンドウで確認/);
});

test("settled and expired approvals expose status without decision actions", () => {
  for (const status of ["approved", "denied", "expired", "stale"] as const) {
    const model = { ...codingApprovalViewModel({ request_id: status, operation: "file.write", risk_level: "medium", status }), status };
    const html = renderToStaticMarkup(createElement(ApprovalDecisionSurface, { approval: model, onApprove: () => undefined, onDeny: () => undefined }));
    assert.doesNotMatch(html, />許可</);
    assert.doesNotMatch(html, />拒否</);
    assert.match(html, /role="status"/);
  }
});

test("numeric shortcuts are explained when explicitly configured", () => {
  const model = codingApprovalViewModel({ request_id: "keys", operation: "terminal.exec", risk_level: "medium", status: "pending" });
  const html = renderToStaticMarkup(createElement(ApprovalDecisionSurface, { approval: model, onApprove: () => undefined, onDeny: () => undefined, keyboardShortcuts: { deny: "2", approve: "3" } }));
  assert.match(html, /入力欄にフォーカスがない場合のみ/);
  assert.match(html, /拒否（2）/);
  assert.match(html, /許可（3）/);
});
