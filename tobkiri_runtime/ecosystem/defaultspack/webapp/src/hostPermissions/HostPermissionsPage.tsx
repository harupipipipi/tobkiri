import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, RefreshCw, ShieldCheck, ShieldQuestion } from "lucide-react";

import { ErrorNotice } from "../components/ErrorNotice";
import { cn } from "../lib/cn";
import { openHostPermissionSettings } from "../lib/desktopApproval";
import { isDesktopSystemInfoAvailable } from "../lib/desktopSystemInfo";
import { fetchHostPermissionsSnapshot, type HostPermissionsSnapshot } from "./hostPermissionsClient";
import { hostPermissionStatusLabel, type HostPermissionBucket, type HostPermissionRow } from "./hostPermissions";
import {
  HOST_PERMISSION_RECHECK_DELAYS_MS,
  beginHostPermissionReconciliation,
  classifyHostPermissionRecheck,
  hostPermissionReconciliationLabel,
  hostPermissionReturnAction,
  hostPermissionSettingsInstruction,
  hostPermissionSnapshotFailure,
  isHostPermissionReconciliationBusy,
  markHostPermissionReconciliationFailure,
  markHostPermissionSettingsOpened,
  type HostPermissionReconciliation,
} from "./hostPermissionReconciliation";

type LoadState = "loading" | "ready" | "error";

export function HostPermissionsPage() {
  const [snapshot, setSnapshot] = useState<HostPermissionsSnapshot | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [message, setMessage] = useState<string | null>(null);
  const [lastVerifiedAt, setLastVerifiedAt] = useState<number | null>(null);
  const [staleSince, setStaleSince] = useState<number | null>(null);
  const [reconciliation, setReconciliation] = useState<HostPermissionReconciliation | null>(null);
  const tauriAvailable = useMemo(() => isDesktopSystemInfoAvailable(), []);
  const snapshotRef = useRef<HostPermissionsSnapshot | null>(null);
  const reconciliationRef = useRef<HostPermissionReconciliation | null>(null);
  const recheckInFlightRef = useRef(false);
  const pendingFinalRecheckRef = useRef(false);
  const recheckTimersRef = useRef<number[]>([]);
  const settingsButtonsRef = useRef(new Map<string, HTMLButtonElement>());

  const updateReconciliation = useCallback((next: HostPermissionReconciliation | null) => {
    reconciliationRef.current = next;
    setReconciliation(next);
  }, []);

  const clearRecheckTimers = useCallback(() => {
    for (const timer of recheckTimersRef.current) window.clearTimeout(timer);
    recheckTimersRef.current = [];
  }, []);

  const restoreSettingsFocus = useCallback((permissionId: string) => {
    window.setTimeout(() => settingsButtonsRef.current.get(permissionId)?.focus(), 0);
  }, []);

  const applySnapshot = useCallback((nextSnapshot: HostPermissionsSnapshot) => {
    snapshotRef.current = nextSnapshot;
    setSnapshot(nextSnapshot);
    setLastVerifiedAt(Date.now());
    setStaleSince(null);
    setLoadState("ready");
    setMessage(nextSnapshot.authorityError
      ? `Tobkiri approval history is unavailable: ${nextSnapshot.authorityError}`
      : null);
  }, []);

  const refresh = useCallback(async (showLoading = true) => {
    if (showLoading) setLoadState("loading");
    setMessage(null);
    try {
      const nextSnapshot = await fetchHostPermissionsSnapshot();
      const loadFailure = hostPermissionSnapshotFailure(nextSnapshot);
      if (loadFailure) throw new Error(loadFailure);
      applySnapshot(nextSnapshot);
      return nextSnapshot;
    } catch (error) {
      const detail = error instanceof Error ? error.message : "Host permissions could not be loaded.";
      setLoadState(snapshotRef.current ? "ready" : "error");
      setStaleSince(Date.now());
      setMessage(detail);
      return null;
    }
  }, [applySnapshot]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runRecheck = useCallback(async (finalAttempt = false) => {
    const active = reconciliationRef.current;
    if (!active || !isHostPermissionReconciliationBusy(active)) return;
    if (recheckInFlightRef.current) {
      pendingFinalRecheckRef.current ||= finalAttempt;
      return;
    }
    recheckInFlightRef.current = true;
    const checking = { ...active, phase: "checking" as const };
    updateReconciliation(checking);
    try {
      const nextSnapshot = await fetchHostPermissionsSnapshot();
      const loadFailure = hostPermissionSnapshotFailure(nextSnapshot);
      if (loadFailure) throw new Error(loadFailure);
      applySnapshot(nextSnapshot);
      const current = reconciliationRef.current;
      if (!current || current.permissionId !== active.permissionId) return;
      const attempt = current.attempt + 1;
      const next = classifyHostPermissionRecheck(
        current,
        nextSnapshot.rows.find((row) => row.id === current.permissionId),
        attempt,
        finalAttempt,
        Date.now(),
      );
      updateReconciliation(next);
      if (!isHostPermissionReconciliationBusy(next)) {
        clearRecheckTimers();
        restoreSettingsFocus(next.permissionId);
      }
    } catch (error) {
      const current = reconciliationRef.current;
      if (!current || current.permissionId !== active.permissionId) return;
      const detail = error instanceof Error ? error.message : "Host permission status could not be checked.";
      const failed = markHostPermissionReconciliationFailure(current, "error", detail, Date.now());
      setLoadState(snapshotRef.current ? "ready" : "error");
      setStaleSince(Date.now());
      setMessage(detail);
      updateReconciliation(failed);
      clearRecheckTimers();
      restoreSettingsFocus(failed.permissionId);
    } finally {
      recheckInFlightRef.current = false;
      if (pendingFinalRecheckRef.current) {
        pendingFinalRecheckRef.current = false;
        void runRecheck(true);
      }
    }
  }, [applySnapshot, clearRecheckTimers, restoreSettingsFocus, updateReconciliation]);

  useEffect(() => {
    const reconcileOnReturn = () => {
      const action = hostPermissionReturnAction(document.visibilityState, reconciliationRef.current);
      if (action === "none") return;
      if (action === "reconcile") {
        void runRecheck(false);
      } else {
        void refresh(false);
      }
    };
    window.addEventListener("focus", reconcileOnReturn);
    document.addEventListener("visibilitychange", reconcileOnReturn);
    return () => {
      window.removeEventListener("focus", reconcileOnReturn);
      document.removeEventListener("visibilitychange", reconcileOnReturn);
    };
  }, [refresh, runRecheck]);

  useEffect(() => () => clearRecheckTimers(), [clearRecheckTimers]);

  const openSettings = useCallback(async (row: HostPermissionRow) => {
    if (isHostPermissionReconciliationBusy(reconciliationRef.current)) return;
    const opening = beginHostPermissionReconciliation(row, Date.now());
    updateReconciliation(opening);
    setMessage(null);
    if (!tauriAvailable) {
      const unavailable = markHostPermissionReconciliationFailure(
        opening,
        "unavailable",
        "Open OS Settings requires the Tobkiri Launcher desktop bridge.",
        Date.now(),
      );
      updateReconciliation(unavailable);
      restoreSettingsFocus(row.id);
      return;
    }
    try {
      const opened = await openHostPermissionSettings(row.id);
      if (!opened) {
        const unavailable = markHostPermissionReconciliationFailure(
          opening,
          "unavailable",
          "The desktop bridge did not confirm the requested OS Settings destination.",
          Date.now(),
        );
        updateReconciliation(unavailable);
        restoreSettingsFocus(row.id);
        return;
      }
      const waiting = markHostPermissionSettingsOpened(opening, Date.now());
      updateReconciliation(waiting);
      clearRecheckTimers();
      recheckTimersRef.current = HOST_PERMISSION_RECHECK_DELAYS_MS.map((delay, index) => window.setTimeout(
        () => void runRecheck(index === HOST_PERMISSION_RECHECK_DELAYS_MS.length - 1),
        delay,
      ));
    } catch (error) {
      const detail = error instanceof Error ? error.message : "OS settings could not be opened.";
      const failed = markHostPermissionReconciliationFailure(opening, "error", detail, Date.now());
      updateReconciliation(failed);
      restoreSettingsFocus(row.id);
    }
  }, [clearRecheckTimers, restoreSettingsFocus, runRecheck, tauriAvailable, updateReconciliation]);

  const rows = snapshot?.rows ?? [];
  const sourceLabel = snapshot?.info
    ? `${snapshot.info.app_name || "Tobkiri Launcher"} · ${snapshot.info.source}`
    : "Desktop system info unavailable";

  return (
    <main className="flex h-screen min-h-0 flex-col overflow-hidden bg-[#09090b] text-zinc-200">
      <header className="shrink-0 border-b border-zinc-800/70 px-4 py-3">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-sky-500/25 bg-sky-500/10 text-sky-200">
                <ShieldCheck size={17} />
              </span>
              <div className="min-w-0">
                <h1 className="truncate text-base font-semibold text-zinc-50">Host Permissions</h1>
                <p className="truncate text-xs text-zinc-500">{sourceLabel}</p>
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loadState === "loading"}
            className="inline-flex h-8 items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950 px-3 text-xs font-semibold text-zinc-300 transition-colors hover:border-zinc-700 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loadState === "loading" ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            Refresh
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
          <StatusStrip snapshot={snapshot} loading={loadState === "loading"} />

          <p className="text-xs text-zinc-500" role="status">
            {lastVerifiedAt ? <>Last verified <time dateTime={new Date(lastVerifiedAt).toISOString()}>{formatTimestamp(lastVerifiedAt)}</time>.</> : "Not verified yet."}
            {staleSince ? <> Last-known values are stale since <time dateTime={new Date(staleSince).toISOString()}>{formatTimestamp(staleSince)}</time>.</> : null}
          </p>

          {!tauriAvailable && (
            <ErrorNotice
              className="rounded-lg px-3 py-2 text-xs leading-5"
              copyLabel="Copy desktop bridge warning"
              copyText="OS settings buttons are disabled because this page is not running inside the Tobkiri Launcher desktop bridge."
              errorIcon="desktop-bridge"
              message="OS settings buttons are disabled because this page is not running inside the Tobkiri Launcher desktop bridge."
              severity="warning"
            />
          )}

          {message && (
            <ErrorNotice
              className="rounded-lg px-3 py-2 text-xs leading-5"
              copyLabel={staleSince ? "Copy host permissions error" : "Copy host permissions warning"}
              copyText={message}
              errorIcon={staleSince ? "host-permissions-error" : "host-permissions-warning"}
              message={message}
              severity={staleSince ? "error" : "warning"}
            />
          )}

          {loadState === "error" ? (
            <ErrorNotice
              className="rounded-lg px-3 py-4 text-sm"
              copyLabel="Copy host permissions load error"
              copyText="Host permission status could not be loaded."
              errorIcon="host-permissions-load"
              message="Host permission status could not be loaded."
            />
          ) : (
            <section className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950/70">
              <div className="grid grid-cols-[minmax(190px,1.2fr)_minmax(120px,0.7fr)_minmax(120px,0.7fr)_minmax(78px,0.45fr)_minmax(90px,0.5fr)_minmax(180px,1fr)_minmax(220px,0.8fr)] gap-3 border-b border-zinc-800 bg-zinc-900/50 px-3 py-2 text-xs font-semibold text-zinc-500 max-lg:hidden">
                <span>Permission</span>
                <span>Tobkiri approval</span>
                <span>OS permission</span>
                <span>Risk</span>
                <span>Stream</span>
                <span>Required by functions</span>
                <span className="text-right">Settings</span>
              </div>
              <div className="divide-y divide-zinc-800/80">
                {rows.length > 0 ? rows.map((row) => (
                  <HostPermissionListRow
                    key={row.id}
                    row={row}
                    tauriAvailable={tauriAvailable}
                    permissionSubject={snapshot?.info?.permission_subject || snapshot?.info?.app_name || "Tobkiri Launcher"}
                    reconciliation={reconciliation?.permissionId === row.id ? reconciliation : null}
                    anyReconciliationBusy={isHostPermissionReconciliationBusy(reconciliation)}
                    buttonRef={(button) => {
                      if (button) settingsButtonsRef.current.set(row.id, button);
                      else settingsButtonsRef.current.delete(row.id);
                    }}
                    onOpenSettings={() => void openSettings(row)}
                  />
                )) : (
                  <div className="px-3 py-10 text-center text-sm text-zinc-500">
                    {loadState === "loading" ? "Loading host permissions..." : "No host permissions were found."}
                  </div>
                )}
              </div>
            </section>
          )}
        </div>
      </div>
    </main>
  );
}

function StatusStrip({ snapshot, loading }: { snapshot: HostPermissionsSnapshot | null; loading: boolean }) {
  const summary = snapshot?.summary;
  const items = [
    { label: "Tobkiri approvals", value: summary ? `${summary.approved}/${summary.total}` : "..." },
    { label: "OS ready", value: summary ? `${summary.osReady}/${summary.total}` : "..." },
    { label: "Permission host", value: snapshot?.info?.permission_subject || snapshot?.info?.app_name || "Unknown" },
    { label: "Reliability", value: snapshot?.info ? (snapshot.info.reliable ? "Verified" : "Unverified") : "Unavailable" },
  ];
  return (
    <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {items.map((item) => (
        <div key={item.label} className="rounded-lg border border-zinc-800 bg-zinc-950/70 px-3 py-2">
          <p className="text-[11px] text-zinc-500">{item.label}</p>
          <p className="mt-1 truncate text-sm font-semibold text-zinc-100">
            {loading && item.value === "..." ? <Loader2 size={14} className="animate-spin" /> : item.value}
          </p>
        </div>
      ))}
    </section>
  );
}

function HostPermissionListRow({
  row,
  tauriAvailable,
  permissionSubject,
  reconciliation,
  anyReconciliationBusy,
  buttonRef,
  onOpenSettings,
}: {
  row: HostPermissionRow;
  tauriAvailable: boolean;
  permissionSubject: string;
  reconciliation: HostPermissionReconciliation | null;
  anyReconciliationBusy: boolean;
  buttonRef: (button: HTMLButtonElement | null) => void;
  onOpenSettings: () => void;
}) {
  const opening = reconciliation?.phase === "opening_settings";
  return (
    <div className="grid gap-3 px-3 py-3 text-sm lg:grid-cols-[minmax(190px,1.2fr)_minmax(120px,0.7fr)_minmax(120px,0.7fr)_minmax(78px,0.45fr)_minmax(90px,0.5fr)_minmax(180px,1fr)_minmax(220px,0.8fr)] lg:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot status={row.rumiStatus === "approved" && (row.osStatus === "approved" || row.osStatus === "unsupported") ? "approved" : row.rumiStatus} />
          <div className="min-w-0">
            <p className="truncate font-medium text-zinc-100">{row.label}</p>
            <p className="truncate font-mono text-[11px] text-zinc-600">{row.id}</p>
          </div>
        </div>
        <p className="mt-1 text-xs leading-5 text-zinc-500 lg:hidden">{row.description}</p>
      </div>
      <LabeledCell label="Tobkiri approval">
        <StatusBadge status={row.rumiStatus} />
      </LabeledCell>
      <LabeledCell label="OS permission">
        <StatusBadge status={row.osStatus} />
      </LabeledCell>
      <LabeledCell label="Risk">
        <span className={cn("inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold capitalize", riskClassName(row.riskLevel))}>
          {row.riskLevel || "unknown"}
        </span>
      </LabeledCell>
      <LabeledCell label="Stream">
        <span className="text-xs font-medium text-zinc-300">{row.streamAllowed === null ? "Unknown" : row.streamAllowed ? "Allowed" : "No"}</span>
      </LabeledCell>
      <LabeledCell label="Required by functions">
        <span className="line-clamp-2 text-xs leading-5 text-zinc-400">{row.requiredByFunctions.join(", ") || "None"}</span>
      </LabeledCell>
      <div className="flex min-w-0 flex-col items-start gap-1.5 lg:items-end">
        <button
          ref={buttonRef}
          type="button"
          onClick={onOpenSettings}
          disabled={!tauriAvailable || anyReconciliationBusy}
          className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 text-xs font-semibold text-zinc-300 transition-colors hover:border-zinc-700 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
          title={tauriAvailable ? `Open OS settings for ${row.label}` : "Requires Tobkiri Launcher desktop bridge"}
        >
          {opening ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}
          {tauriAvailable ? "Open" : "Desktop only"}
        </button>
        {reconciliation && (
          <div className="w-full rounded-md border border-zinc-800 bg-black/20 px-2 py-1.5 text-left text-xs leading-4 lg:text-right" role="status" aria-live="polite">
            <strong className={cn("font-semibold", reconciliationPhaseClassName(reconciliation.phase))}>
              {hostPermissionReconciliationLabel(reconciliation.phase)}
            </strong>
            {(reconciliation.phase === "opening_settings" || reconciliation.phase === "waiting_for_return" || reconciliation.phase === "checking") && (
              <span className="mt-0.5 block text-zinc-500">
                {hostPermissionSettingsInstruction(row, permissionSubject)}
              </span>
            )}
            {reconciliation.detail && <span className="mt-0.5 block text-zinc-500">{reconciliation.detail}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function LabeledCell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 lg:block">
      <span className="text-[11px] font-semibold text-zinc-600 lg:hidden">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: HostPermissionBucket }) {
  return (
    <span className={cn("inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold", statusClassName(status))}>
      {hostPermissionStatusLabel(status)}
    </span>
  );
}

function StatusDot({ status }: { status: HostPermissionBucket }) {
  if (status === "approved") return <CheckCircle2 size={15} className="shrink-0 text-emerald-300" />;
  if (status === "unknown" || status === "unsupported") return <ShieldQuestion size={15} className="shrink-0 text-zinc-500" />;
  return <AlertTriangle size={15} className="shrink-0 text-amber-300" />;
}

function statusClassName(status: HostPermissionBucket): string {
  switch (status) {
    case "approved":
      return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
    case "pending":
      return "border-sky-500/30 bg-sky-500/10 text-sky-300";
    case "missing":
      return "border-amber-500/30 bg-amber-500/10 text-amber-200";
    case "denied":
    case "blocked":
      return "border-rose-500/30 bg-rose-500/10 text-rose-300";
    case "unsupported":
      return "border-zinc-800 bg-zinc-900 text-zinc-400";
    default:
      return "border-zinc-800 bg-zinc-950 text-zinc-500";
  }
}

function riskClassName(risk: string): string {
  switch (risk) {
    case "critical":
      return "border-rose-400/40 bg-rose-500/15 text-rose-200";
    case "high":
      return "border-orange-500/30 bg-orange-500/10 text-orange-200";
    case "medium":
      return "border-amber-500/30 bg-amber-500/10 text-amber-200";
    case "low":
      return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
    default:
      return "border-zinc-800 bg-zinc-900 text-zinc-400";
  }
}

function reconciliationPhaseClassName(phase: HostPermissionReconciliation["phase"]): string {
  if (phase === "changed") return "text-emerald-300";
  if (phase === "denied" || phase === "error") return "text-rose-300";
  if (phase === "unchanged" || phase === "unavailable") return "text-amber-200";
  return "text-sky-300";
}

function formatTimestamp(timestamp: number): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(timestamp));
}
