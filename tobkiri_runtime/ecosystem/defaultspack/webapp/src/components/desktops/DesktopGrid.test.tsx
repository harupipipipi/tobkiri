import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { DesktopInstance } from "../../features/sandboxes/types";
import { resolveVisibleSelectedDesktop, resolveVisibleSelectedSeatId, shouldShowDesktopList } from "./DesktopMonitorWorkspace";
import { DesktopGrid } from "./DesktopGrid";
import { keyboardCaptureDecision } from "./DesktopTile";

const noop = () => undefined;
const key = (value: string, overrides = {}) => ({ key: value, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...overrides });

test("desktop keyboard capture reserves escape and preserves remote shortcuts", () => {
  assert.deepEqual(keyboardCaptureDecision(key("Escape")), { kind: "release" });
  assert.deepEqual(keyboardCaptureDecision(key("Escape", { ctrlKey: true, altKey: true, shiftKey: true })), { kind: "release" });
  assert.deepEqual(keyboardCaptureDecision(key("Tab")), { kind: "key", key: "Tab" });
  assert.deepEqual(keyboardCaptureDecision(key("Tab", { shiftKey: true })), { kind: "key", key: "shift+Tab" });
  assert.deepEqual(keyboardCaptureDecision(key("l", { ctrlKey: true })), { kind: "key", key: "ctrl+l" });
});

test("desktop keyboard capture avoids duplicate IME events", () => {
  assert.deepEqual(keyboardCaptureDecision(key("é")), { kind: "type", text: "é" });
  assert.deepEqual(keyboardCaptureDecision(key("Process", { isComposing: true })), { kind: "ignore" });
  assert.deepEqual(keyboardCaptureDecision(key("Dead")), { kind: "ignore" });
});

function desktop(seatId: string): DesktopInstance {
  return {
    seat_id: seatId,
    name: `Desktop ${seatId}`,
    status: "running",
    provider_id: "linux_native",
    resolution: { width: 1024, height: 768 },
  };
}

function renderGrid(desktops: DesktopInstance[]) {
  return renderToStaticMarkup(
    createElement(DesktopGrid, {
      desktops,
      selectedSeatId: desktops[0]?.seat_id ?? null,
      density: "comfortable",
      leaseSeatId: null,
      onSelect: noop,
      onTakeOver: noop,
      onReturnToAI: noop,
      onInput: noop,
      onStart: noop,
      onRestart: noop,
      onStop: noop,
      onDelete: noop,
    }),
  );
}

test("single desktop tile uses prominent monitor sizing", () => {
  const html = renderGrid([desktop("seat-1")]);

  assert.match(html, /grid w-full grid-cols-1/);
  assert.match(html, /min-h-\[calc\(100vh-150px\)\]/);
  assert.match(html, /min-h-\[520px\]/);
  assert.doesNotMatch(html, /min-\[900px\]:grid-cols-2/);
});

test("multiple desktop grid keeps compact multi-column sizing", () => {
  const html = renderGrid([desktop("seat-1"), desktop("seat-2")]);

  assert.match(html, /min-\[900px\]:grid-cols-2/);
  assert.doesNotMatch(html, /min-h-\[calc\(100vh-180px\)\]/);
});

test("desktop grid distinguishes filter-empty from backend-empty", () => {
  const html = renderToStaticMarkup(
    createElement(DesktopGrid, {
      desktops: [],
      selectedSeatId: null,
      density: "comfortable",
      leaseSeatId: null,
      emptyReason: "filter",
      onSelect: noop,
      onTakeOver: noop,
      onReturnToAI: noop,
      onInput: noop,
      onStart: noop,
      onRestart: noop,
      onStop: noop,
      onDelete: noop,
    }),
  );

  assert.match(html, /No matching desktop seats/);
  assert.doesNotMatch(html, /backend returned an empty desktop list/);
});

test("desktop workspace keeps existing seats visible while runtime setup is degraded", () => {
  assert.equal(shouldShowDesktopList({
    runtimeReady: false,
    desktopCount: 1,
    loading: false,
  }), true);
  assert.equal(shouldShowDesktopList({
    runtimeReady: false,
    desktopCount: 0,
    loading: false,
  }), false);
});

test("desktop workspace resolves selection from visible desktops", () => {
  const destroyedDesktop = {
    ...desktop("1c1dd944-destroyed-seat"),
    name: "QA-Swarm-18766-Worker1",
    status: "destroyed",
  } satisfies DesktopInstance;
  const runningDesktop = {
    ...desktop("822f90f9-running-seat"),
    name: "QA-Swarm-18766-Worker1",
  };
  const visibleDesktops = [destroyedDesktop, runningDesktop].filter((instance) => instance.status === "running");

  assert.equal(
    resolveVisibleSelectedDesktop(visibleDesktops, destroyedDesktop.seat_id)?.seat_id,
    runningDesktop.seat_id,
  );
  assert.equal(
    resolveVisibleSelectedSeatId(visibleDesktops, destroyedDesktop.seat_id),
    runningDesktop.seat_id,
  );
});

test("desktop workspace defaults focus to a running desktop when stale seats appear first", () => {
  const destroyedDesktop = {
    ...desktop("1c1dd944-destroyed-seat"),
    name: "QA-Swarm-18766-Worker1",
    status: "destroyed",
  } satisfies DesktopInstance;
  const runningDesktop = {
    ...desktop("822f90f9-running-seat"),
    name: "QA-Swarm-18766-Worker1",
  };
  const visibleDesktops = [destroyedDesktop, runningDesktop];

  assert.equal(
    resolveVisibleSelectedDesktop(visibleDesktops, null)?.seat_id,
    runningDesktop.seat_id,
  );
  assert.equal(
    resolveVisibleSelectedSeatId(visibleDesktops, destroyedDesktop.seat_id),
    runningDesktop.seat_id,
  );
});

test("desktop workspace preserves an explicit non-running selection", () => {
  const destroyedDesktop = {
    ...desktop("1c1dd944-destroyed-seat"),
    name: "QA-Swarm-18766-Worker1",
    status: "destroyed",
  } satisfies DesktopInstance;
  const runningDesktop = {
    ...desktop("822f90f9-running-seat"),
    name: "QA-Swarm-18766-Worker1",
  };
  const visibleDesktops = [destroyedDesktop, runningDesktop];

  assert.equal(
    resolveVisibleSelectedDesktop(visibleDesktops, destroyedDesktop.seat_id, { preserveSelected: true })?.seat_id,
    destroyedDesktop.seat_id,
  );
  assert.equal(
    resolveVisibleSelectedSeatId(visibleDesktops, destroyedDesktop.seat_id, { preserveSelected: true }),
    destroyedDesktop.seat_id,
  );
});

test("registered desktop stays visible while unsupported actions are disabled", () => {
  const html = renderToStaticMarkup(createElement(DesktopGrid, {
    desktops: [desktop("saved-seat")], selectedSeatId: "saved-seat", density: "comfortable",
    leaseSeatId: "saved-seat", operationSupport: { control: false, frame: false, lifecycle: false, delete: false },
    onSelect: noop, onTakeOver: noop, onReturnToAI: noop, onInput: noop,
    onStart: noop, onRestart: noop, onStop: noop, onDelete: noop,
  }));
  assert.match(html, /Desktop saved-seat/);
  assert.match(html, /操作未接続/);
  for (const action of ["Take over", "Snapshot", "Restart", "Stop", "Delete"]) {
    assert.match(html, new RegExp(`<button[^>]*disabled=""[^>]*aria-label="${action} Desktop saved-seat"`));
  }
  assert.doesNotMatch(html, /Control available|Human control|Waiting for first snapshot|live snapshot/);
  assert.match(html, /画面取得は未接続です/);
});

test("unconnected desktop frames label saved records without live readiness", async () => {
  const { DesktopToolbar } = await import("./DesktopToolbar");
  const html = renderToStaticMarkup(createElement(DesktopToolbar, {
    totalCount: 1, runningCount: 1, framesAvailable: false, canCreate: false,
    filter: "all", density: "comfortable", onFilterChange: noop,
    onDensityChange: noop, onCreate: noop, onDoctor: noop,
  }));
  assert.match(html, /保存された一覧（稼働未確認）/);
  assert.doesNotMatch(html, /Live snapshots|1 running/);
});

test("unprobed provider notice explains Mac Lima path and independent PackVM", async () => {
  const { DesktopProviderNotice } = await import("./DesktopProviderNotice");
  const html = renderToStaticMarkup(createElement(DesktopProviderNotice, {
    availability: { status: "registered", selectedProvider: { provider_id: "mac_lima", status: "available" }, providers: [], missing: [], message: "登録済み・未診断" },
    operation: null, operationSupport: { setup: false, doctor: false },
    onSetup: noop, onDoctor: noop, onCopyDiagnostics: noop, onRefresh: noop,
  }));
  assert.match(html, /MacはLimaに対応/);
  assert.match(html, /Tobkiri本体の実行環境とは別/);
  assert.match(html, /登録情報を更新/);
  assert.match(html, /<button[^>]*disabled=""[^>]*>[^]*?ゲストを準備/);
});
