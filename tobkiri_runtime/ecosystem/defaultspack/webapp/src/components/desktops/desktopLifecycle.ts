import type { DesktopInstance, RuntimeOperation } from "../../features/sandboxes/types";

export type DesktopLifecycleAction = "start" | "restart" | "stop" | "delete";

export type DesktopLifecycleFeedback = {
  action: DesktopLifecycleAction;
  operationId: string;
  phase: "pending" | "failed";
  error?: string;
  retryWithNewOperation?: boolean;
};

export type DesktopLifecycleReconciliation = {
  authoritative: boolean;
  outcome: "requested" | "external-stop" | "external-delete" | "conflict";
};

export function createDesktopLifecycleOperationId(action: DesktopLifecycleAction): string {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `desktop-${action}-${suffix}`;
}

export function desktopActionIsAuthoritative(
  desktops: DesktopInstance[],
  seatId: string,
  action: DesktopLifecycleAction,
): boolean {
  return reconcileDesktopLifecycle(desktops, seatId, action).authoritative;
}

export function reconcileDesktopLifecycle(
  desktops: DesktopInstance[],
  seatId: string,
  action: DesktopLifecycleAction,
): DesktopLifecycleReconciliation {
  const desktop = desktops.find((candidate) => candidate.seat_id === seatId);
  const deleted = !desktop
    || desktop.status === "destroyed"
    || String(desktop.status) === "deleted";
  if (action === "delete") {
    return {
      authoritative: deleted,
      outcome: deleted ? "requested" : "conflict",
    };
  }
  if (action === "stop") {
    if (deleted) return { authoritative: true, outcome: "external-delete" };
    if (desktop.status === "stopped") {
      return { authoritative: true, outcome: "external-stop" };
    }
    return { authoritative: false, outcome: "conflict" };
  }
  return {
    authoritative: desktop?.status === "running",
    outcome: desktop?.status === "running" ? "requested" : "conflict",
  };
}

export function reserveDesktopLifecycleAttempt(
  pendingSeatIds: Set<string>,
  seatId: string,
  action: DesktopLifecycleAction,
  previous: DesktopLifecycleFeedback | undefined,
  createOperationId: (action: DesktopLifecycleAction) => string = createDesktopLifecycleOperationId,
): string | null {
  if (pendingSeatIds.has(seatId)) return null;
  pendingSeatIds.add(seatId);
  if (
    previous?.action === action
    && previous.phase === "failed"
    && previous.retryWithNewOperation !== true
  ) {
    return previous.operationId;
  }
  return createOperationId(action);
}

export function desktopLifecycleSuccessMessage(
  desktopName: string,
  action: DesktopLifecycleAction,
  reconciliation: DesktopLifecycleReconciliation,
): string {
  if (reconciliation.outcome === "external-delete") {
    return `${desktopName} was deleted elsewhere; no further ${action} action was needed.`;
  }
  if (action === "delete") return `${desktopName} was deleted.`;
  if (action === "stop") return `${desktopName} is stopped.`;
  if (action === "restart") return `${desktopName} restarted and is running.`;
  return `${desktopName} started and is running.`;
}

export function desktopLifecycleSafeError(
  error: unknown,
  action: DesktopLifecycleAction,
): string {
  const code = typeof error === "object" && error !== null && "code" in error
    ? String(error.code || "")
    : "";
  if (code === "SANDBOX_NOT_FOUND") {
    return "The desktop no longer exists. Refresh the workspace before trying another action.";
  }
  if (code === "DESKTOP_OPERATION_IN_PROGRESS") {
    return "Another lifecycle action is still running for this desktop. Check its operation status before retrying.";
  }
  if (code === "DESKTOP_OPERATION_ID_CONFLICT") {
    return "The operation identity belongs to a different lifecycle action. Retry will use a new identity.";
  }
  if (code.includes("ACCESS") || code.includes("OWNER") || code.includes("FORBIDDEN")) {
    return "Tobkiri could not verify permission for this desktop action. Request fresh access and try again.";
  }
  return `Tobkiri could not safely confirm the desktop ${action} outcome. Check the latest desktop state before retrying.`;
}

export function desktopLifecycleRetryNeedsNewOperation(error: unknown): boolean {
  const code = typeof error === "object" && error !== null && "code" in error
    ? String(error.code || "")
    : "";
  return code === "DESKTOP_OPERATION_ID_CONFLICT";
}

export function desktopOperationError(operation: RuntimeOperation): string | null {
  if (operation.status !== "failed" && operation.status !== "cancelled") return null;
  const errorCode = operation.error && typeof operation.error === "object"
    ? operation.error.code
    : undefined;
  const action = operation.action;
  if (action === "start" || action === "restart" || action === "stop" || action === "delete") {
    return desktopLifecycleSafeError({ code: errorCode }, action);
  }
  return "The desktop lifecycle operation did not complete.";
}

export async function lookupDesktopOperationOutcome(
  operationId: string,
  getOperation: (operationId: string) => Promise<RuntimeOperation>,
  options: {
    delays?: number[];
    wait?: (milliseconds: number) => Promise<void>;
  } = {},
): Promise<RuntimeOperation | null> {
  const delays = options.delays ?? [0, 500, 1500, 3000];
  const wait = options.wait ?? ((milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds)));
  for (const [index, delay] of delays.entries()) {
    if (delay > 0) await wait(delay);
    try {
      const operation = await getOperation(operationId);
      if (["completed", "failed", "cancelled"].includes(operation.status)) return operation;
    } catch (error) {
      const status = typeof error === "object" && error !== null && "status" in error
        ? Number(error.status)
        : 0;
      if (status === 404) return null;
      if (status >= 400 && status < 500) throw error;
      if (index === delays.length - 1) throw error;
    }
  }
  return null;
}
