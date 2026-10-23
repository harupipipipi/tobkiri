import assert from "node:assert/strict";
import test from "node:test";
import { consumeSidebarCanvasRequest, isAvailableSidebarCanvas } from "./sidebarPanelRequest";

test("catalogue rerenders do not replay a consumed Canvas token; new revisions do", () => {
  const consumed = { current: null as string | null };
  assert.equal(consumeSidebarCanvasRequest(consumed, "__canvas_widget__:1", "__canvas_widget__", true), true);
  assert.equal(consumeSidebarCanvasRequest(consumed, "__canvas_widget__:1", "__canvas_widget__", true), false);
  assert.equal(consumeSidebarCanvasRequest(consumed, "__canvas_widget__:2", "__canvas_widget__", true), true);
});

test("Canvas open remains pending until the actual surface is ready", () => {
  const consumed = { current: null as string | null };
  assert.equal(consumeSidebarCanvasRequest(consumed, "__canvas_widget__:1", "__canvas_widget__", false), false);
  assert.equal(consumed.current, null);
  assert.equal(consumeSidebarCanvasRequest(consumed, "__canvas_widget__:1", "__canvas_widget__", true), true);
  assert.equal(consumeSidebarCanvasRequest(consumed, "__close_canvas_widget__:2", "__close_canvas_widget__", false), true);
});

test("non-Canvas requests keep existing repeated catalogue and initial-category reconciliation", () => {
  const consumed = { current: "__canvas_widget__:1" as string | null };
  for (const id of ["tool", "__coding_widget__", "__company_workspace__", "__workspace_tabs__", "__prompt_usage__", "__timeline_widget__", null]) {
    for (let render = 0; render < 3; render++) {
      assert.equal(consumeSidebarCanvasRequest(consumed, id && `${id}:2`, id, false), true);
      assert.equal(consumed.current, null);
    }
  }
});

// Model the actual child-before-parent passive effect order. App mirrors showPreview
// into a revisioned request; Sidebar emits visibility changes and reruns request
// reconciliation when its catalogue array identity changes on a parent render.
function settleCanvasEffects(consumeOnce: boolean, initiallyOpen: boolean, localPanel: string | null) {
  let show = initiallyOpen;
  let request = `${show ? "__canvas_widget__" : "__close_canvas_widget__"}:1`;
  let panel = localPanel;
  let lastMirrored = show;
  let lastNotified = show;
  let revision = 1;
  const consumed = { current: request as string | null };
  const notifications: boolean[] = [];
  for (let commit = 0; commit < 20; commit++) {
    let nextPanel = panel;
    let nextShow = show;
    let nextRequest = request;
    if (!consumeOnce || consumeSidebarCanvasRequest(consumed, request, request.split(":")[0], true)) {
      if (request.startsWith("__canvas_widget__:")) nextPanel = "canvas";
      else if (panel === "canvas") nextPanel = null;
    }
    const canvas = panel === "canvas";
    if (canvas !== lastNotified) {
      notifications.push(canvas);
      nextShow = canvas;
      lastNotified = canvas;
    }
    if (show !== lastMirrored) {
      nextRequest = `${show ? "__canvas_widget__" : "__close_canvas_widget__"}:${++revision}`;
      lastMirrored = show;
    }
    if (nextPanel === panel && nextShow === show && nextRequest === request) return { panel, show, notifications };
    panel = nextPanel;
    show = nextShow;
    request = nextRequest;
  }
  throw new Error("Canvas visibility feedback did not settle");
}

test("Canvas to Tools settles rather than reopening the stale Canvas request", () => {
  assert.throws(() => settleCanvasEffects(false, true, "tools"), /did not settle/);
  assert.deepEqual(settleCanvasEffects(true, true, "tools"), { panel: "tools", show: false, notifications: [false] });
});

test("reopening Canvas ignores the old close token and settles once", () => {
  assert.throws(() => settleCanvasEffects(false, false, "canvas"), /did not settle/);
  assert.deepEqual(settleCanvasEffects(true, false, "canvas"), { panel: "canvas", show: true, notifications: [true] });
});



test("initial native Canvas survives the old category snapshot when its reserved alias is catalogued", () => {
  const token = "__canvas_widget__:1";
  const consumed = { current: null as string | null };
  const initialPanel = "__canvas_widget__";
  const initialCategory = "activity";
  const cataloguedAlias = { id: initialPanel, category: "widget" };
  assert.equal(consumeSidebarCanvasRequest(consumed, token, initialPanel, true), true);
  // The request effect schedules category=widget, but the same commit's category
  // validity effect still sees activity. The native panel must not be cleared.
  const categoryMismatch = cataloguedAlias.category !== initialCategory;
  assert.equal(categoryMismatch, true);
  const nextPanel = categoryMismatch && !isAvailableSidebarCanvas(initialPanel, true) ? null : initialPanel;
  assert.equal(nextPanel, initialPanel);
  assert.equal(consumeSidebarCanvasRequest(consumed, token, nextPanel, true), false);
  assert.equal(isAvailableSidebarCanvas("catalog-tool", true), false);
  assert.equal(isAvailableSidebarCanvas(initialPanel, false), false);
});
