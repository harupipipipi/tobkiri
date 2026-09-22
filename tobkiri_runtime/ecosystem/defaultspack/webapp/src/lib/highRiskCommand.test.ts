import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";

import {
  beginHighRiskAttempt,
  highRiskCommandRef,
  highRiskPrepareArguments,
  highRiskResumeDisposition,
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

// Execute the production polling effect with deterministic API/timer boundaries.
// This catches clearing pending state or resubmitting resume in the component.
for (const liveState of ["claimed", "dispatched"]) {
  for (const terminalState of ["succeeded", "ambiguous"]) {
    test(`approval poll keeps ${liveState} pending until ${terminalState}`, async () => {
      const appSource = readFileSync(resolve(import.meta.dirname, "..", "App.tsx"), "utf8");
      const marker = "  useEffect(() => {\n    if (!pendingHighRiskCommand) return;";
      const start = appSource.indexOf(marker);
      const end = appSource.indexOf("  }, [pendingHighRiskCommand]);", start);
      assert.ok(start >= 0 && end > start, "production approval poll effect is present");
      const body = appSource.slice(start + "  useEffect(() => {".length, end);
      const compiled = ts.transpileModule(`const run = () => {${body}};`, {
        compilerOptions: { target: ts.ScriptTarget.ES2022 },
      }).outputText;
      const pending = { invocationId: "invocation-1", requestId: "request-1", commandLabel: "terminal" };
      let pendingState: typeof pending | null = pending;
      let resumeCalls = 0;
      let statusCalls = 0;
      const notifications: Array<{ tone: string }> = [];
      const errors: string[] = [];
      const timers: Array<() => void> = [];
      const states = ["approved", "approved", "dispatched", terminalState];
      const dependencies = {
        pendingHighRiskCommand: pending,
        api: {
          getInteractiveApproval: async () => ({ request_id: pending.requestId, state: "approved" }),
          highRiskCommandStatus: async () => ({
            invocation_id: pending.invocationId,
            approval_request_id: pending.requestId,
            state: states[statusCalls++],
          }),
          resumeHighRiskCommand: async () => {
            resumeCalls += 1;
            return { invocation_id: pending.invocationId, state: liveState };
          },
        },
        window: {
          setTimeout: (callback: () => void) => { timers.push(callback); return timers.length; },
          clearTimeout: () => {},
        },
        setPendingHighRiskCommand: (update: (value: typeof pending | null) => typeof pending | null) => {
          pendingState = update(pendingState);
        },
        setTransientAlert: (value: { tone: string }) => notifications.push(value),
        setError: (value: string) => errors.push(value),
        transientAlertSequenceRef: { current: 0 },
        highRiskResumeStartedRef: { current: new Set<string>() },
        highRiskCancelStartedRef: { current: new Set<string>() },
        HIGH_RISK_TERMINAL_STATES: new Set(["succeeded", "ambiguous", "stale", "cancelled", "failed"]),
        beginHighRiskAttempt,
        releaseHighRiskAttempt,
        highRiskResumeDisposition,
      };
      const cleanup = new Function(...Object.keys(dependencies), `${compiled}; return run();`)(
        ...Object.values(dependencies),
      ) as () => void;
      try {
        for (let poll = 0; poll < states.length; poll += 1) {
          await new Promise<void>((resolve) => setImmediate(resolve));
          assert.equal(resumeCalls, 1, "resume must never be used to wait for completion");
          if (poll < states.length - 1) {
            assert.equal(pendingState, pending);
            assert.deepEqual(notifications, []);
            assert.deepEqual(errors, []);
            assert.equal(timers.length, 1, "one status poll stays scheduled");
            timers.shift()!();
          }
        }
        assert.equal(pendingState, null);
        assert.equal(timers.length, 0);
        assert.equal(statusCalls, states.length);
        if (terminalState === "succeeded") {
          assert.deepEqual(notifications.map((value) => value.tone), ["success"]);
          assert.deepEqual(errors, []);
        } else {
          assert.deepEqual(notifications, []);
          assert.equal(errors.length, 1);
          assert.match(errors[0], /ambiguous/);
        }
      } finally {
        cleanup();
      }
    });
  }
}
