import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";


test("chat approval handlers use the one-shot server continuation instead of legacy streaming", () => {
  const source = readFileSync(resolve(import.meta.dirname, "..", "App.tsx"), "utf8");
  const browserStart = source.indexOf("const approveBrowserAction = async () => {");
  const browserEnd = source.indexOf("const denyBrowserAction = async () => {", browserStart);
  const codingStart = source.indexOf("const approveCodingAction = async () => {");
  const codingEnd = source.indexOf("const approveCommandAction = async () => {", codingStart);
  const handlers = source.slice(browserStart, browserEnd) + source.slice(codingStart, codingEnd);

  assert.match(handlers, /api\.approveCodingApprovalForContinuation/);
  assert.match(handlers, /api\.resumeCodingApproval/);
  // Continuation packets must be bound to the exact pending saved turn so
  // they can never be projected onto a newer or unrelated pending request.
  assert.match(handlers, /pendingRequests\[activeConversationId\]\?\.savedTurn/);
  assert.match(handlers, /pendingTurnId/);
  assert.doesNotMatch(handlers, /api\.streamMessage/);
  assert.doesNotMatch(handlers, /approval_token|runtime_content|tool_policy/);
});
