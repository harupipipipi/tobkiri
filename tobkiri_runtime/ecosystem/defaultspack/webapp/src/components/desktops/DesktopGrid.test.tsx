import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { DesktopInstance } from "../../features/sandboxes/types";
import { filterVisibleDesktops, resolveVisibleSelectedDesktop, resolveVisibleSelectedSeatId, shouldShowDesktopList } from "./DesktopMonitorWorkspace";
import { DesktopGrid } from "./DesktopGrid";
import { keyboardCaptureDecision } from "./DesktopTile";

const noop = () => undefined;
const inputNoop = async () => true;
const key = (value: string, overrides = {}) => ({ key: value, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...overrides });

test("desktop keyboard capture reserves escape and preserves remote shortcuts", () => {
  assert.deepEqual(keyboardCaptureDecision(key("Escape")), { kind: "release" });
  assert.deepEqual(keyboardCaptureDecision(key("Escape", { ctrlKey: true, altKey: true, shiftKey: true })), { kind: "release" });
  assert.deepEqual(keyboardCaptureDecision(key("Escape", { isComposing: true })), { kind: "release" });
  assert.deepEqual(keyboardCaptureDecision(key("Tab")), { kind: "key", key: "Tab" });
  assert.deepEqual(keyboardCaptureDecision(key("Tab", { shiftKey: true })), { kind: "key", key: "shift+Tab" });
  assert.deepEqual(keyboardCaptureDecision(key("l", { ctrlKey: true })), { kind: "key", key: "ctrl+l" });
  assert.deepEqual(keyboardCaptureDecision(key("S", { ctrlKey: true, shiftKey: true })), { kind: "key", key: "ctrl+shift+s" });
  assert.deepEqual(keyboardCaptureDecision(key("Control", { ctrlKey: true })), { kind: "modifier" });
  assert.deepEqual(keyboardCaptureDecision(key("AltGraph", { ctrlKey: true, altKey: true, altGraphKey: true })), { kind: "ignore" });
  assert.deepEqual(keyboardCaptureDecision(key("€", { ctrlKey: true, altKey: true, altGraphKey: true })), { kind: "type", text: "€" });
  assert.deepEqual(keyboardCaptureDecision(key("F1")), { kind: "unsupported", key: "F1" });
  assert.deepEqual(keyboardCaptureDecision(key("AudioVolumeUp")), { kind: "unsupported", key: "AudioVolumeUp" });
});

test("desktop keyboard capture avoids duplicate IME events", () => {
  assert.deepEqual(keyboardCaptureDecision(key("é")), { kind: "type", text: "é" });
  assert.deepEqual(keyboardCaptureDecision(key("😀")), { kind: "type", text: "😀" });
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
      onInput: inputNoop,
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
      onInput: inputNoop,
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

test("running filter retains the selected stopped seat for capture focus restoration", () => {
  const stoppedDesktop = { ...desktop("seat-stopped"), status: "stopped" } satisfies DesktopInstance;
  const runningDesktop = desktop("seat-running");

  assert.deepEqual(
    filterVisibleDesktops([stoppedDesktop, runningDesktop], "running", stoppedDesktop.seat_id)
      .map((instance) => instance.seat_id),
    [stoppedDesktop.seat_id, runningDesktop.seat_id],
  );
  assert.deepEqual(
    filterVisibleDesktops([stoppedDesktop, runningDesktop], "running", runningDesktop.seat_id)
      .map((instance) => instance.seat_id),
    [runningDesktop.seat_id],
  );
});
