import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  beginHighRiskAttempt,
  highRiskCommandRef,
  highRiskPrepareArguments,
  releaseHighRiskAttempt,
} from "./highRiskCommand";
import type { ComposerCommandItem } from "./api";

function command(id: string, canonical_id?: string): ComposerCommandItem {
  return {
    id,
    name: id,
    label: id,
    category: "coding",
    visibility: "hidden",
    risk: "high",
    execution: { type: "frontend", action: `request_${id}_approval` },
    ...(canonical_id ? { canonical_id } : {}),
  };
}

test("only the fixed five command names use the interactive adapter", () => {
  assert.equal(highRiskCommandRef(command("terminal", "defaultspack:terminal")), "terminal");
  assert.equal(highRiskCommandRef(command("commit")), "commit");
  assert.equal(highRiskCommandRef(command("push")), "push");
  assert.equal(highRiskCommandRef(command("patch")), "patch");
  assert.equal(highRiskCommandRef(command("restore")), "restore");
  assert.equal(highRiskCommandRef(command("delete_everything")), null);
});

test("prepare normalizes provider arguments while resume data stays opaque", () => {
  assert.deepEqual(
    highRiskPrepareArguments("terminal", { cmd: "git status" }, {
      workspaceId: "workspace-1",
    }),
    { command: "git status", cwd: ".", env: {}, timeout: 30 },
  );
  assert.deepEqual(
    highRiskPrepareArguments("restore", { paths: "src/a.ts src/b.ts" }, {
      workspaceId: "workspace-1",
    }),
    {
      workspace_id: "workspace-1",
      source: "HEAD",
      paths: ["src/a.ts", "src/b.ts"],
    },
  );
});

test("none of the five browser prepare payloads selects a profile", () => {
  const common = { workspaceId: "workspace-1", currentBranch: "main" };
  const payloads = [
    highRiskPrepareArguments("terminal", { cmd: "git status" }, common),
    highRiskPrepareArguments("commit", { message: "test" }, common),
    highRiskPrepareArguments("push", { remote: "origin" }, common),
    highRiskPrepareArguments("patch", { patch: "diff --git a/a b/a" }, common),
    highRiskPrepareArguments("restore", { paths: "src/a.ts" }, common),
  ];

  for (const payload of payloads) {
    assert.equal(
      Object.prototype.hasOwnProperty.call(payload, "profile_id"),
      false,
    );
  }
});

test("git prepare refuses a missing workspace before a request is sent", () => {
  assert.throws(
    () => highRiskPrepareArguments("commit", { message: "test" }, {
      workspaceId: null,
    }),
    /作業空間/,
  );
});

test("a lost response releases the invocation guard for an idempotent retry", () => {
  const inFlight = new Set<string>();
  assert.equal(beginHighRiskAttempt(inFlight, "high-risk-1"), true);
  assert.equal(beginHighRiskAttempt(inFlight, "high-risk-1"), false);

  // A response may be lost either side of the Host's CAS claim. The next
  // call carries the same opaque id and is therefore the safe retry shape.
  releaseHighRiskAttempt(inFlight, "high-risk-1");
  assert.equal(beginHighRiskAttempt(inFlight, "high-risk-1"), true);
});

test("the five adapter commands branch before legacy frontend command execution", () => {
  const appSource = readFileSync(resolve(import.meta.dirname, "..", "App.tsx"), "utf8");
  const executeStart = appSource.indexOf("const executeComposerCommand");
  const adapterBranch = appSource.indexOf("const highRiskRef = highRiskCommandRef(parsed.command);", executeStart);
  const legacyBranch = appSource.indexOf("if (isRegisteredSlashCommand(parsed.command)", executeStart);
  assert.ok(adapterBranch > executeStart);
  assert.ok(legacyBranch > adapterBranch);
  assert.match(appSource.slice(adapterBranch, legacyBranch), /api\.prepareHighRiskCommand\(/);
  assert.doesNotMatch(appSource.slice(adapterBranch, legacyBranch), /api\.executeResolvedUiCommand\(/);
});

test("reload restoration opens each native high-risk approval window once", () => {
  const appSource = readFileSync(resolve(import.meta.dirname, "..", "App.tsx"), "utf8");
  const restoreStart = appSource.indexOf("void api.listHighRiskCommands()");
  const restoreEnd = appSource.indexOf("  useEffect(", restoreStart);
  const restoreSource = appSource.slice(restoreStart, restoreEnd);
  const claimIndex = restoreSource.indexOf(
    "highRiskApprovalWindowOpenedRequestRef.current = approvalRequestId;",
  );
  const openIndex = restoreSource.indexOf("openAuthorityApprovalWindow(approvalRequestId)");

  assert.ok(claimIndex >= 0);
  assert.ok(openIndex > claimIndex);
  assert.match(
    restoreSource,
    /highRiskApprovalWindowOpenedRequestRef\.current === approvalRequestId/,
  );
});
