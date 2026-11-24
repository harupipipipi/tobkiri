import { useEffect, useMemo, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, RefreshCw, ShieldCheck, ShieldQuestion } from "lucide-react";

import { ErrorNotice } from "../components/ErrorNotice";
import { cn } from "../lib/cn";
import { openHostPermissionSettings } from "../lib/desktopApproval";
import { isDesktopSystemInfoAvailable } from "../lib/desktopSystemInfo";
import { fetchHostPermissionsSnapshot, type HostPermissionsSnapshot } from "./hostPermissionsClient";
import { hostPermissionStatusLabel, type HostPermissionBucket, type HostPermissionRow } from "./hostPermissions";

type LoadState = "loading" | "ready" | "error";

type PageNotice = {
  message: string;
  severity: "error" | "warning" | "success";
};

export function HostPermissionsPage() {
  const [snapshot, setSnapshot] = useState<HostPermissionsSnapshot | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [notice, setNotice] = useState<PageNotice | null>(null);
  const [openingPermissionId, setOpeningPermissionId] = useState<string | null>(null);
  const tauriAvailable = useMemo(() => isDesktopSystemInfoAvailable(), []);

  const refresh = async () => {
    setLoadState("loading");
    setNotice(null);
    try {
      const nextSnapshot = await fetchHostPermissionsSnapshot();
      setSnapshot(nextSnapshot);
      setLoadState("ready");
      if (nextSnapshot.authorityError) {
        setNotice({
          message: `Tobkiri approval history is unavailable: ${nextSnapshot.authorityError}`,
          severity: "warning",
        });
      }
    } catch (error) {
      setLoadState("error");
      setNotice({
        message: error instanceof Error ? error.message : "Host permissions could not be loaded.",
        severity: "error",
      });
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

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

          {notice?.severity === "success" ? (
            <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs leading-5 text-zinc-400">
              {notice.message}
            </div>
          ) : null}
          {notice?.severity === "error" || notice?.severity === "warning" ? (
            <ErrorNotice
              className="rounded-lg px-3 py-2 text-xs leading-5"
              copyLabel={notice.severity === "error" ? "Copy host permissions error" : "Copy host permissions warning"}
              copyText={notice.message}
              errorIcon={`host-permissions-${notice.severity}`}
              message={notice.message}
              severity={notice.severity}
            />
          ) : null}

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
              <div className="grid grid-cols-[minmax(190px,1.2fr)_minmax(120px,0.7fr)_minmax(120px,0.7fr)_minmax(78px,0.45fr)_minmax(90px,0.5fr)_minmax(180px,1fr)_minmax(116px,0.55fr)] gap-3 border-b border-zinc-800 bg-zinc-900/50 px-3 py-2 text-[11px] font-semibold text-zinc-500 max-lg:hidden">
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
                    opening={openingPermissionId === row.id}
                    onOpenSettings={async () => {
                      if (!tauriAvailable) {
                        setNotice({
                          message: "Open OS Settings is available only in Tobkiri Launcher.",
                          severity: "warning",
                        });
                        return;
                      }
                      setOpeningPermissionId(row.id);
                      setNotice(null);
                      try {
                        const opened = await openHostPermissionSettings(row.id);
                        setNotice(opened
                          ? { message: `${row.label} settings opened.`, severity: "success" }
                          : {
                            message: "Open OS Settings is available only in Tobkiri Launcher.",
                            severity: "warning",
                          });
                      } catch (error) {
                        setNotice({
                          message: error instanceof Error ? error.message : "OS settings could not be opened.",
                          severity: "error",
                        });
                      } finally {
                        setOpeningPermissionId(null);
                      }
                    }}
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
  opening,
  onOpenSettings,
}: {
  row: HostPermissionRow;
  tauriAvailable: boolean;
  opening: boolean;
  onOpenSettings: () => void;
}) {
  return (
    <div className="grid gap-3 px-3 py-3 text-sm lg:grid-cols-[minmax(190px,1.2fr)_minmax(120px,0.7fr)_minmax(120px,0.7fr)_minmax(78px,0.45fr)_minmax(90px,0.5fr)_minmax(180px,1fr)_minmax(116px,0.55fr)] lg:items-center">
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
      <div className="flex justify-start lg:justify-end">
        <button
          type="button"
          onClick={onOpenSettings}
          disabled={!tauriAvailable || opening}
          className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 text-xs font-semibold text-zinc-300 transition-colors hover:border-zinc-700 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
          title={tauriAvailable ? `Open OS settings for ${row.label}` : "Requires Tobkiri Launcher desktop bridge"}
        >
          {opening ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}
          {tauriAvailable ? "Open" : "Desktop only"}
        </button>
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
