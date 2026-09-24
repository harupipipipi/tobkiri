import type { HostPermissionBucket, HostPermissionRow } from "./hostPermissions";

export type HostPermissionReconciliationPhase =
  | "opening_settings"
  | "waiting_for_return"
  | "checking"
  | "changed"
  | "unchanged"
  | "denied"
  | "unavailable"
  | "error";

export type HostPermissionReconciliation = {
  permissionId: string;
  beforeStatus: HostPermissionBucket;
  phase: HostPermissionReconciliationPhase;
  openedAt: number;
  attempt: number;
  checkedAt?: number;
  detail?: string;
};

export const HOST_PERMISSION_RECHECK_DELAYS_MS = [600, 1_800, 4_000] as const;

const BUSY_PHASES = new Set<HostPermissionReconciliationPhase>([
  "opening_settings",
  "waiting_for_return",
  "checking",
]);

export function hostPermissionSnapshotFailure(snapshot: {
  info: unknown;
  authorityError?: string;
}): string | null {
  if (snapshot.info || !snapshot.authorityError) return null;
  return `Host permission refresh failed: ${snapshot.authorityError}`;
}

export function beginHostPermissionReconciliation(
  row: HostPermissionRow,
  now: number,
): HostPermissionReconciliation {
  return {
    permissionId: row.id,
    beforeStatus: row.osStatus,
    phase: "opening_settings",
    openedAt: now,
    attempt: 0,
  };
}

export function markHostPermissionSettingsOpened(
  state: HostPermissionReconciliation,
  now: number,
): HostPermissionReconciliation {
  return {
    ...state,
    phase: "waiting_for_return",
    openedAt: now,
    detail: undefined,
  };
}

export function classifyHostPermissionRecheck(
  state: HostPermissionReconciliation,
  row: HostPermissionRow | undefined,
  attempt: number,
  finalAttempt: boolean,
  now: number,
): HostPermissionReconciliation {
  if (!row) {
    return {
      ...state,
      phase: "unavailable",
      attempt,
      checkedAt: now,
      detail: "This permission is no longer reported by the desktop host.",
    };
  }

  if (row.osStatus === "denied" || row.osStatus === "blocked") {
    return {
      ...state,
      phase: "denied",
      attempt,
      checkedAt: now,
      detail: `${statusLabel(state.beforeStatus)} → ${statusLabel(row.osStatus)}`,
    };
  }

  if (row.osStatus !== state.beforeStatus) {
    return {
      ...state,
      phase: "changed",
      attempt,
      checkedAt: now,
      detail: `${statusLabel(state.beforeStatus)} → ${statusLabel(row.osStatus)}`,
    };
  }

  if (finalAttempt) {
    return {
      ...state,
      phase: "unchanged",
      attempt,
      checkedAt: now,
      detail: `Still ${statusLabel(row.osStatus)} after the bounded re-check window.`,
    };
  }

  return {
    ...state,
    phase: "checking",
    attempt,
    checkedAt: now,
    detail: `Check ${attempt} of ${HOST_PERMISSION_RECHECK_DELAYS_MS.length}; waiting for the OS to settle.`,
  };
}

export function markHostPermissionReconciliationFailure(
  state: HostPermissionReconciliation,
  phase: "unavailable" | "error",
  detail: string,
  now: number,
): HostPermissionReconciliation {
  return { ...state, phase, detail, checkedAt: now };
}

export function isHostPermissionReconciliationBusy(
  state: HostPermissionReconciliation | null,
): boolean {
  return Boolean(state && BUSY_PHASES.has(state.phase));
}

export function hostPermissionReturnAction(
  visibilityState: DocumentVisibilityState,
  state: HostPermissionReconciliation | null,
): "none" | "reconcile" | "refresh" {
  if (visibilityState === "hidden") return "none";
  return isHostPermissionReconciliationBusy(state) ? "reconcile" : "refresh";
}

export function hostPermissionReconciliationLabel(
  phase: HostPermissionReconciliationPhase,
): string {
  switch (phase) {
    case "opening_settings":
      return "Opening settings";
    case "waiting_for_return":
      return "Waiting for return";
    case "checking":
      return "Checking";
    case "changed":
      return "Changed";
    case "unchanged":
      return "Unchanged";
    case "denied":
      return "Denied";
    case "unavailable":
      return "Unavailable";
    case "error":
      return "Error";
  }
}

export function hostPermissionSettingsInstruction(
  row: HostPermissionRow,
  permissionSubject: string,
): string {
  if (row.settingsHint?.trim()) return row.settingsHint.trim();
  return `In OS Settings, change the ${row.label} control for ${permissionSubject}, then return to Tobkiri.`;
}

function statusLabel(status: HostPermissionBucket): string {
  return status.replace(/^./, (value) => value.toUpperCase());
}
