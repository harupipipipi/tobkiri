import assert from "node:assert/strict";
import test from "node:test";
import { createElement, createRef } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { DesktopInstance } from "../../features/sandboxes/types";
import { DesktopLifecycleConfirmation } from "./DesktopLifecycleConfirmation";

const target: DesktopInstance = {
  seat_id: "seat-1",
  name: "Accounting desktop",
  status: "running",
};

function renderConfirmation(
  phase: "pending" | "failed",
  action: "stop" | "delete" = "delete",
): string {
  return renderToStaticMarkup(createElement(DesktopLifecycleConfirmation, {
    action,
    target,
    feedback: {
      action,
      operationId: `${action}-operation-1`,
      phase,
      error: phase === "failed" ? "The desktop state changed before the action ran." : undefined,
    },
    confirmButtonRef: createRef<HTMLButtonElement>(),
    onClose: () => undefined,
    onConfirm: () => undefined,
  }));
}

test("pending lifecycle confirmation is outcome-aware and fully non-dismissible", () => {
  const html = renderConfirmation("pending");

  assert.match(html, /role="alertdialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /aria-busy="true"/);
  assert.match(html, /data-desktop-lifecycle-action="delete"/);
  assert.match(html, /data-desktop-seat-id="seat-1"/);
  assert.match(html, /Accounting desktop/);
  assert.match(html, /Deleting this desktop and checking the latest server state/);
  assert.match(html, /Operation delete-operation-1/);
  assert.equal((html.match(/ disabled=""/g) ?? []).length, 3);
});

test("failed lifecycle confirmation keeps safe failure, operation identity, and retry in the dialog", () => {
  const html = renderConfirmation("failed", "stop");

  assert.match(html, /Stop Desktop/);
  assert.match(html, /Accounting desktop/);
  assert.match(html, /role="alert"/);
  assert.match(html, /The desktop state changed before the action ran/);
  assert.match(html, /Operation stop-operation-1/);
  assert.match(html, />Retry</);
  assert.doesNotMatch(html, / disabled=""/);
});
