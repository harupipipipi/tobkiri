import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(import.meta.dirname, "SubagentTeamWorkspace.tsx"), "utf8");

test("Subagent Team decision previews cannot mutate local approval state", () => {
  assert.doesNotMatch(source, /setDecisionStatus/);
  assert.doesNotMatch(source, /onStatusChange=\{setDecisionStatus\}/);
  assert.doesNotMatch(source, /onStatusChange\("approved"\)/);
  assert.doesNotMatch(source, />Approve<|>Revise<|>OK</);
});

test("Subagent Team labels API and fallback decision data as read-only previews", () => {
  assert.match(source, /Read-only preview\. No approval or revision is recorded from this card\./);
  assert.match(source, /Fallback preview data is read-only and cannot be approved\./);
  assert.match(source, /Open the authoritative pending request to approve, reject, or request revision\./);
  assert.match(source, /subagent-approval-preview-readonly/);
});

test("Subagent Team preview failures expose separate severity and copy controls", () => {
  assert.match(source, /preview\.error \? \([\s\S]*?<ErrorNotice/);
  assert.match(source, /errorIcon="channel-preview"/);
  assert.match(source, /copyLabel="Copy channel preview error"/);
  assert.match(source, /message=\{preview\.error\}/);
  assert.doesNotMatch(source, /\{preview\.error \|\| "No preview content"\}/);
});
