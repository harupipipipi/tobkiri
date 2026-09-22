import assert from "node:assert/strict";
import test from "node:test";

import type { DesktopInstance, RuntimeOperation } from "../../features/sandboxes/types";
import {
  desktopLifecycleSafeError,
  desktopLifecycleSuccessMessage,
  desktopActionIsAuthoritative,
  desktopOperationError,
  lookupDesktopOperationOutcome,
  reconcileDesktopLifecycle,
  reserveDesktopLifecycleAttempt,
} from "./desktopLifecycle";

const desktop = (status: DesktopInstance["status"]): DesktopInstance => ({
  seat_id: "seat-1",
  name: "Test seat",
  status,
});

test("desktop lifecycle success requires the authoritative seat state", () => {
  assert.equal(desktopActionIsAuthoritative([desktop("stopped")], "seat-1", "stop"), true);
  assert.equal(desktopActionIsAuthoritative([desktop("running")], "seat-1", "stop"), false);
  assert.equal(desktopActionIsAuthoritative([], "seat-1", "delete"), true);
  assert.equal(desktopActionIsAuthoritative([desktop("destroyed")], "seat-1", "delete"), true);
  assert.equal(desktopActionIsAuthoritative([desktop("running")], "seat-1", "delete"), false);
  assert.equal(desktopActionIsAuthoritative([desktop("running")], "seat-1", "restart"), true);
});

test("desktop lifecycle reconciles external stop and delete explicitly", () => {
  assert.deepEqual(
    reconcileDesktopLifecycle([desktop("stopped")], "seat-1", "stop"),
    { authoritative: true, outcome: "external-stop" },
  );
  assert.deepEqual(
    reconcileDesktopLifecycle([], "seat-1", "stop"),
    { authoritative: true, outcome: "external-delete" },
  );
  assert.match(
    desktopLifecycleSuccessMessage(
      "Test seat",
      "stop",
      { authoritative: true, outcome: "external-delete" },
    ),
    /deleted elsewhere/,
  );
});

test("desktop lifecycle reservation rejects double activation and conflicting key repeat", () => {
  const pending = new Set<string>();
  const created: string[] = [];
  const createId = (action: string) => {
    const id = `${action}-${created.length + 1}`;
    created.push(id);
    return id;
  };

  assert.equal(reserveDesktopLifecycleAttempt(pending, "seat-1", "stop", undefined, createId), "stop-1");
  assert.equal(reserveDesktopLifecycleAttempt(pending, "seat-1", "stop", undefined, createId), null);
  assert.equal(reserveDesktopLifecycleAttempt(pending, "seat-1", "delete", undefined, createId), null);
  assert.deepEqual(created, ["stop-1"]);
});

test("ambiguous retries reuse identity while definitive failures get a new identity", () => {
  const createId = (action: string) => `${action}-new`;
  const ambiguous = {
    action: "delete" as const,
    operationId: "delete-stable",
    phase: "failed" as const,
  };
  const definitive = { ...ambiguous, retryWithNewOperation: true };

  assert.equal(
    reserveDesktopLifecycleAttempt(new Set(), "seat-1", "delete", ambiguous, createId),
    "delete-stable",
  );
  assert.equal(
    reserveDesktopLifecycleAttempt(new Set(), "seat-1", "delete", definitive, createId),
    "delete-new",
  );
});

test("operation lookup polls running work and returns terminal timeout-after-commit outcome", async () => {
  const statuses: RuntimeOperation["status"][] = ["running", "running", "completed"];
  const waits: number[] = [];
  const result = await lookupDesktopOperationOutcome(
    "op-1",
    async () => ({ operation_id: "op-1", status: statuses.shift() || "completed" }),
    { delays: [0, 10, 20], wait: async (delay) => { waits.push(delay); } },
  );
  assert.equal(result?.status, "completed");
  assert.deepEqual(waits, [10, 20]);
});

test("operation lookup tolerates status endpoint absence and exposes safe terminal failures", async () => {
  const missing = await lookupDesktopOperationOutcome("op-2", async () => {
    throw Object.assign(new Error("404"), { status: 404 });
  });
  assert.equal(missing, null);
  assert.equal(desktopOperationError({
    operation_id: "op-3",
    status: "failed",
    action: "stop",
    error: { code: "STALE_SEAT", message: "provider path /secret must not leak" },
  }), "Tobkiri could not safely confirm the desktop stop outcome. Check the latest desktop state before retrying.");
  assert.doesNotMatch(
    desktopLifecycleSafeError({ code: "STALE_SEAT", message: "/secret" }, "delete"),
    /secret/,
  );
});

test("operation lookup retries transient transport failure without hiding authorization errors", async () => {
  let attempts = 0;
  const recovered = await lookupDesktopOperationOutcome(
    "op-transient",
    async () => {
      attempts += 1;
      if (attempts === 1) throw new TypeError("temporary network failure");
      return { operation_id: "op-transient", status: "completed" };
    },
    { delays: [0, 1], wait: async () => undefined },
  );
  assert.equal(recovered?.status, "completed");
  await assert.rejects(
    lookupDesktopOperationOutcome(
      "op-forbidden",
      async () => { throw Object.assign(new Error("forbidden"), { status: 403 }); },
      { delays: [0, 1], wait: async () => undefined },
    ),
    /forbidden/,
  );
});
