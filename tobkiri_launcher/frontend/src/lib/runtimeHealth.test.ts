import assert from "node:assert/strict";
import test from "node:test";

import { describeRuntimeBadge, describeRuntimeBanner, runtimeMonitorDelay } from "./runtimeHealth";
import {getRuntimeDispatchStatus, setRuntimeDispatchStatus} from "./runtimeDispatchGate";
import {useAppStore} from "@/src/store";

test("runtimeMonitorDelay polls slowly when the runtime is stable", () => {
  assert.equal(runtimeMonitorDelay({
    runtimeReady: true,
    runtimeStatus: "runtime_ready",
    runtimeError: null,
    runtimeDisconnected: false,
    lastRuntimeHealthyAt: 1,
  }), 15_000);
});

test("runtimeMonitorDelay polls quickly while recovering from a disconnect", () => {
  assert.equal(runtimeMonitorDelay({
    runtimeReady: false,
    runtimeStatus: "error",
    runtimeError: "connection lost",
    runtimeDisconnected: true,
    lastRuntimeHealthyAt: 1,
  }), 2_500);
});

test("describeRuntimeBadge highlights reconnecting state with an offline badge", () => {
  const badge = describeRuntimeBadge({
    runtimeReady: false,
    runtimeStatus: "error",
    runtimeError: "connection lost",
    runtimeDisconnected: true,
    lastRuntimeHealthyAt: 30_000,
  }, 90_000);

  assert.equal(badge.tone, "danger");
  assert.equal(badge.label, "Reconnecting");
  assert.equal(badge.showOfflineBadge, true);
  assert.match(badge.detail, /最後に安定していた/);
});

test("describeRuntimeBanner returns crafted warmup copy", () => {
  const banner = describeRuntimeBanner({
    runtimeReady: false,
    runtimeStatus: "starting",
    runtimeError: null,
    runtimeDisconnected: false,
    lastRuntimeHealthyAt: null,
  });

  assert.equal(banner.tone, "warning");
  assert.match(banner.title, /静かに起動中/);
});

test("reconfirmation is a distinct actionable state, not warmup or runtime error", () => {
  const badge = describeRuntimeBadge({
    runtimeReady: false,
    runtimeStatus: "profile_reconfirmation_required",
    runtimeError: "private Host diagnostic",
    runtimeDisconnected: false,
    lastRuntimeHealthyAt: null,
  });
  assert.equal(badge.label, "Profile reconfirmation required");
  assert.equal(badge.tone, "warning");
  assert.doesNotMatch(badge.detail, /private Host diagnostic/);

  const banner = describeRuntimeBanner({
    runtimeReady: false,
    runtimeStatus: "profile_reconfirmation_required",
    runtimeError: "private Host diagnostic",
    runtimeDisconnected: false,
    lastRuntimeHealthyAt: null,
  });
  assert.match(banner.title, /Profile reconfirmation/);
  assert.doesNotMatch(banner.detail, /private Host diagnostic/);
  assert.equal(runtimeMonitorDelay({
    runtimeReady: false,
    runtimeStatus: "profile_reconfirmation_required",
    runtimeError: null,
    runtimeDisconnected: false,
    lastRuntimeHealthyAt: null,
  }), 2_500);
});

test("the store cannot publish a contradictory health state to the dispatch gate", () => {
  const previousState = useAppStore.getState();
  const previousDispatchStatus = getRuntimeDispatchStatus();
  setRuntimeDispatchStatus("runtime_ready");

  assert.throws(() => useAppStore.getState().setRuntimeHealth({
    status: "error",
    needs_setup: false,
    panel_ready: true,
    runtime_ready: false,
    runtime_status: "runtime_ready",
    runtime_error: "denied",
  }), /contradictory/);
  assert.equal(getRuntimeDispatchStatus(), "error");
  assert.equal(useAppStore.getState().runtimeReady, false);
  assert.equal(useAppStore.getState().runtimeStatus, "error");

  useAppStore.setState(previousState, true);
  setRuntimeDispatchStatus(previousDispatchStatus);
});
