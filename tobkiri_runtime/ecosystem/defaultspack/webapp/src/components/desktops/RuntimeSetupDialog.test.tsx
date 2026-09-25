import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { RuntimeOperation } from "../../features/sandboxes/types";
import { RuntimeSetupDialog } from "./RuntimeSetupDialog";

function operation(status: RuntimeOperation["status"]): RuntimeOperation {
  return {
    operation_id: `runtime-${status}`,
    status,
    step: status === "running" ? "packages" : status,
    message: "Runtime operation status",
    progress: status === "completed" ? 100 : 40,
  };
}

test("runtime setup dialog exposes cancel only while operation is running", () => {
  const running = renderToStaticMarkup(
    createElement(RuntimeSetupDialog, {
      operation: operation("running"),
      onCancel: () => undefined,
    }),
  );
  const completed = renderToStaticMarkup(
    createElement(RuntimeSetupDialog, {
      operation: operation("completed"),
      onCancel: () => undefined,
    }),
  );
  const cancelRequested = renderToStaticMarkup(
    createElement(RuntimeSetupDialog, {
      operation: operation("cancel_requested"),
      onCancel: () => undefined,
    }),
  );
  const cancelled = renderToStaticMarkup(
    createElement(RuntimeSetupDialog, {
      operation: operation("cancelled"),
      onCancel: () => undefined,
    }),
  );

  assert.match(running, />Cancel</);
  assert.match(running, /type="button"/);
  assert.match(running, /role="status"/);
  assert.match(running, /aria-live="polite"/);
  assert.match(running, /role="progressbar"/);
  assert.match(running, /aria-valuenow="40"/);
  assert.match(cancelRequested, /disabled=""/);
  assert.match(cancelRequested, />Cancelling</);
  assert.doesNotMatch(completed, />Cancel</);
  assert.doesNotMatch(cancelled, />Cancel</);
});

test("runtime setup dialog announces success and error outcomes accessibly", () => {
  const completed = renderToStaticMarkup(
    createElement(RuntimeSetupDialog, {
      operation: {
        ...operation("completed"),
        message: "Runtime is ready",
      },
    }),
  );
  const failed = renderToStaticMarkup(
    createElement(RuntimeSetupDialog, {
      operation: {
        ...operation("failed"),
        message: "Runtime setup failed",
        error: { code: "RUNTIME_FAILED", message: "Package installation failed" },
      },
    }),
  );

  assert.match(completed, /role="status"/);
  assert.match(completed, /Runtime is ready/);
  assert.match(completed, /aria-valuenow="100"/);
  assert.match(failed, /role="alert"/);
  assert.match(failed, /Package installation failed/);
  assert.doesNotMatch(failed, />Cancel</);
});
