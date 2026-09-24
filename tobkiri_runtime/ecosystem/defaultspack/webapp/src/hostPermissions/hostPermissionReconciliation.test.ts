import test from "node:test";
import assert from "node:assert/strict";

import type { HostPermissionRow } from "./hostPermissions";
import {
  beginHostPermissionReconciliation,
  classifyHostPermissionRecheck,
  hostPermissionSettingsInstruction,
  hostPermissionSnapshotFailure,
  hostPermissionReturnAction,
  isHostPermissionReconciliationBusy,
  markHostPermissionReconciliationFailure,
  markHostPermissionSettingsOpened,
} from "./hostPermissionReconciliation";

function row(osStatus: HostPermissionRow["osStatus"]): HostPermissionRow {
  return {
    id: "host.microphone.capture",
    label: "Microphone",
    description: "Capture microphone input.",
    rumiStatus: "approved",
    osStatus,
    riskLevel: "high",
    streamAllowed: false,
    requiredByFunctions: ["ambient_monitor_start"],
    source: "desktop",
  };
}

test("reconciliation reports a grant and a revoke as changed", () => {
  const grant = markHostPermissionSettingsOpened(beginHostPermissionReconciliation(row("missing"), 1), 2);
  const granted = classifyHostPermissionRecheck(grant, row("approved"), 1, false, 3);
  assert.equal(granted.phase, "changed");
  assert.equal(granted.detail, "Missing → Approved");

  const revoke = markHostPermissionSettingsOpened(beginHostPermissionReconciliation(row("approved"), 4), 5);
  const revoked = classifyHostPermissionRecheck(revoke, row("missing"), 1, false, 6);
  assert.equal(revoked.phase, "changed");
  assert.equal(revoked.detail, "Approved → Missing");
});

test("reconciliation distinguishes denial from an unchanged or delayed status", () => {
  const opened = markHostPermissionSettingsOpened(beginHostPermissionReconciliation(row("missing"), 1), 2);
  const checking = classifyHostPermissionRecheck(opened, row("missing"), 1, false, 3);
  assert.equal(checking.phase, "checking");
  assert.equal(isHostPermissionReconciliationBusy(checking), true);

  const changedAfterDelay = classifyHostPermissionRecheck(checking, row("approved"), 2, false, 4);
  assert.equal(changedAfterDelay.phase, "changed");

  const unchanged = classifyHostPermissionRecheck(checking, row("missing"), 3, true, 5);
  assert.equal(unchanged.phase, "unchanged");
  assert.equal(isHostPermissionReconciliationBusy(unchanged), false);

  const denied = classifyHostPermissionRecheck(opened, row("denied"), 1, false, 6);
  assert.equal(denied.phase, "denied");
});

test("bridge and refresh failures become terminal unavailable and error states", () => {
  const opening = beginHostPermissionReconciliation(row("unknown"), 1);
  const unavailable = markHostPermissionReconciliationFailure(opening, "unavailable", "No bridge", 2);
  assert.equal(unavailable.phase, "unavailable");
  assert.equal(isHostPermissionReconciliationBusy(unavailable), false);

  const error = markHostPermissionReconciliationFailure(opening, "error", "Refresh failed", 3);
  assert.equal(error.phase, "error");
  assert.equal(error.detail, "Refresh failed");

  const missing = classifyHostPermissionRecheck(opening, undefined, 1, true, 4);
  assert.equal(missing.phase, "unavailable");
});

test("opening is synchronously busy so rapid opens can be rejected", () => {
  const opening = beginHostPermissionReconciliation(row("missing"), 1);
  assert.equal(isHostPermissionReconciliationBusy(opening), true);
  const waiting = markHostPermissionSettingsOpened(opening, 2);
  assert.equal(isHostPermissionReconciliationBusy(waiting), true);
});

test("focus and visibility return reconcile active opens and refresh external changes", () => {
  const opening = beginHostPermissionReconciliation(row("missing"), 1);
  assert.equal(hostPermissionReturnAction("hidden", opening), "none");
  assert.equal(hostPermissionReturnAction("visible", opening), "reconcile");
  assert.equal(hostPermissionReturnAction("visible", null), "refresh");
});

test("settings guidance uses the native hint or names the exact fallback control", () => {
  assert.equal(
    hostPermissionSettingsInstruction({ ...row("missing"), settingsHint: "Privacy > Microphone" }, "Tobkiri Launcher"),
    "Privacy > Microphone",
  );
  assert.equal(
    hostPermissionSettingsInstruction(row("missing"), "Tobkiri Launcher"),
    "In OS Settings, change the Microphone control for Tobkiri Launcher, then return to Tobkiri.",
  );
});

test("a fully degraded refresh is stale instead of replacing the last verified snapshot", () => {
  assert.equal(
    hostPermissionSnapshotFailure({ info: null, authorityError: "Failed to fetch" }),
    "Host permission refresh failed: Failed to fetch",
  );
  assert.equal(hostPermissionSnapshotFailure({ info: { reliable: true }, authorityError: "Partial failure" }), null);
  assert.equal(hostPermissionSnapshotFailure({ info: null }), null);
});
