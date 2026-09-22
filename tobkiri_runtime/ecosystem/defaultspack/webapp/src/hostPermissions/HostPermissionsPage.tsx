import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  RefreshCw,
  ShieldCheck,
  ShieldQuestion,
} from "lucide-react";

import { ErrorNotice } from "../components/ErrorNotice";
import { cn } from "../lib/cn";
import { openHostPermissionSettings } from "../lib/desktopApproval";
import { isDesktopSystemInfoAvailable } from "../lib/desktopSystemInfo";
import {
  fetchHostPermissionsSnapshot,
  type HostPermissionsSnapshot,
} from "./hostPermissionsClient";
import {
  hostPermissionStatusLabel,
  safeHostPermissionDiagnostic,
  type HostPermissionBucket,
  type HostPermissionRow,
} from "./hostPermissions";

type LoadState = "loading" | "ready" | "error";
type Notice = { tone: "status" | "error"; text: string };

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
    const previousSignature = snapshot ? hostPermissionStatusSignature(snapshot.rows) : "";
    setLoadState("loading");
    setNotice(null);
    setDiagnostic("");
    try {
      const nextSnapshot = await fetchHostPermissionsSnapshot();
      const nextSignature = hostPermissionStatusSignature(nextSnapshot.rows);
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
  const settingsDestination = hostSettingsDestination(snapshot?.info?.platform);

  return (
    <main
      aria-labelledby="host-permissions-title"
      className="host-permissions-page flex h-screen min-h-0 flex-col overflow-hidden bg-[#09090b] text-zinc-200"
    >
      <header className="shrink-0 border-b border-zinc-800/70 px-4 py-3">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <span
              aria-hidden="true"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-sky-500/25 bg-sky-500/10 text-sky-200"
            >
              <ShieldCheck size={17} />
            </span>
            <div className="min-w-0">
              <h1 id="host-permissions-title" className="text-base font-semibold text-zinc-50">
                Host Permissions
              </h1>
              <p className="break-words text-xs text-zinc-500">{sourceLabel}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              if (loadState !== "loading") void refresh();
            }}
            aria-disabled={loadState === "loading"}
            aria-label={loadState === "loading" ? "Refreshing host permissions" : "Refresh host permissions"}
            className="inline-flex min-h-11 min-w-11 items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950 px-3 text-xs font-semibold text-zinc-300 transition-colors hover:border-zinc-700 hover:bg-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300 aria-disabled:cursor-not-allowed aria-disabled:opacity-60 motion-reduce:transition-none"
          >
            {loadState === "loading"
              ? <Loader2 aria-hidden="true" size={14} className="animate-spin motion-reduce:animate-none" />
              : <RefreshCw aria-hidden="true" size={14} />}
            {loadState === "loading" ? "Refreshing" : "Refresh"}
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

          <HostPermissionsTable
            rows={rows}
            loading={loadState === "loading"}
            failed={loadState === "error"}
            tauriAvailable={tauriAvailable}
            openingPermissionId={openingPermissionId}
            settingsDestination={settingsDestination}
            onOpenSettings={async (row) => {
              if (!tauriAvailable) {
                setNotice({
                  tone: "error",
                  text: `${settingsDestination} for ${row.label} is available only in Tobkiri Launcher.`,
                });
                return;
              }
              setOpeningPermissionId(row.id);
              setNotice({ tone: "status", text: `Opening ${settingsDestination} for ${row.label}.` });
              setDiagnostic("");
              try {
                const opened = await openHostPermissionSettings(row.id);
                setNotice({
                  tone: opened ? "status" : "error",
                  text: opened
                    ? `${settingsDestination} opened for ${row.label}. Return here and refresh after changing the permission.`
                    : `${settingsDestination} could not be opened. Use Tobkiri Launcher and try again.`,
                });
              } catch (error) {
                setNotice({
                  tone: "error",
                  text: `${settingsDestination} could not be opened for ${row.label}. Try again from Tobkiri Launcher.`,
                });
                setDiagnostic(safeHostPermissionDiagnostic(error));
              } finally {
                setOpeningPermissionId(null);
              }
            }}
          />
        </div>
      </div>
    </main>
  );
}

export function StatusStrip({
  snapshot,
  loading,
}: {
  snapshot: HostPermissionsSnapshot | null;
  loading: boolean;
}) {
  const summary = snapshot?.summary;
  const items = [
    { label: "Tobkiri approvals", value: summary ? `${summary.approved}/${summary.total}` : "..." },
    { label: "OS ready", value: summary ? `${summary.osReady}/${summary.total}` : "..." },
    { label: "Permission host", value: snapshot?.info?.permission_subject || snapshot?.info?.app_name || "Unknown" },
    { label: "Reliability", value: snapshot?.info ? (snapshot.info.reliable ? "Verified" : "Unverified") : "Unavailable" },
  ];
  return (
    <section aria-labelledby="host-permission-summary-title" aria-busy={loading}>
      <h2 id="host-permission-summary-title" className="sr-only">Host permission summary</h2>
      <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {items.map((item) => (
          <div key={item.label} className="rounded-lg border border-zinc-800 bg-zinc-950/70 px-3 py-2">
            <dt className="text-[11px] text-zinc-500">{item.label}</dt>
            <dd className="mt-1 break-words text-sm font-semibold text-zinc-100">
              {loading && !snapshot ? (
                <span className="inline-flex items-center gap-2">
                  <Loader2 aria-hidden="true" size={14} className="animate-spin motion-reduce:animate-none" />
                  Loading
                </span>
              ) : item.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export function HostPermissionsTable({
  rows,
  loading,
  failed,
  tauriAvailable,
  openingPermissionId,
  settingsDestination,
  onOpenSettings,
}: {
  rows: HostPermissionRow[];
  loading: boolean;
  failed: boolean;
  tauriAvailable: boolean;
  openingPermissionId: string | null;
  settingsDestination: string;
  onOpenSettings: (row: HostPermissionRow) => void;
}) {
  const emptyText = loading
    ? "Loading host permissions..."
    : failed
      ? "Host permission status is unavailable. Use Refresh to try again."
      : "No host permissions were found.";

  return (
    <section
      aria-labelledby="host-permission-table-title"
      aria-busy={loading}
      className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950/70"
    >
      <h2 id="host-permission-table-title" className="sr-only">Permission status and settings</h2>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-sm">
          <caption className="sr-only">
            Tobkiri approval, operating-system permission, risk, stream allowance, required functions, and settings action for each host permission.
          </caption>
          <thead className="bg-zinc-900/50 text-[11px] font-semibold text-zinc-500 max-lg:sr-only">
            <tr>
              <th scope="col" className="px-3 py-2">Permission</th>
              <th scope="col" className="px-3 py-2">Tobkiri approval</th>
              <th scope="col" className="px-3 py-2">OS permission</th>
              <th scope="col" className="px-3 py-2">Risk</th>
              <th scope="col" className="px-3 py-2">Stream</th>
              <th scope="col" className="px-3 py-2">Required by functions</th>
              <th scope="col" className="px-3 py-2 text-right">Settings</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/80">
            {rows.length > 0 ? rows.map((row) => (
              <HostPermissionTableRow
                key={row.id}
                row={row}
                tauriAvailable={tauriAvailable}
                opening={openingPermissionId === row.id}
                settingsDestination={settingsDestination}
                onOpenSettings={() => onOpenSettings(row)}
              />
            )) : (
              <tr>
                <td colSpan={7} className="px-3 py-10 text-center text-sm text-zinc-500">
                  {emptyText}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HostPermissionTableRow({
  row,
  tauriAvailable,
  opening,
  settingsDestination,
  onOpenSettings,
}: {
  row: HostPermissionRow;
  tauriAvailable: boolean;
  opening: boolean;
  settingsDestination: string;
  onOpenSettings: () => void;
}) {
  const overallStatus = row.rumiStatus !== "approved"
    ? row.rumiStatus
    : row.osStatus === "approved" || row.osStatus === "unsupported"
      ? "approved"
      : row.osStatus;
  const descriptionId = `host-permission-${cssSafeId(row.id)}-description`;

  return (
    <tr className="block px-3 py-3 lg:table-row lg:px-0 lg:py-0">
      <th
        scope="row"
        aria-describedby={descriptionId}
        className="block min-w-48 py-2 align-top font-normal lg:table-cell lg:px-3 lg:py-3"
      >
        <div className="flex items-start gap-2">
          <StatusDot status={overallStatus} />
          <div className="min-w-0">
            <p className="font-medium text-zinc-100">{row.label}</p>
            <p className="break-all font-mono text-[11px] text-zinc-500">{row.id}</p>
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
      </TableCell>
      <TableCell label="Stream">
        <span className="text-xs font-medium text-zinc-300">
          {row.streamAllowed === null ? "Unknown" : row.streamAllowed ? "Allowed" : "Not allowed"}
        </span>
      </TableCell>
      <TableCell label="Required by functions">
        <span className="break-all text-xs leading-5 text-zinc-300">
          {row.requiredByFunctions.join(", ") || "None"}
        </span>
      </TableCell>
      <td className="block py-2 align-top lg:table-cell lg:px-3 lg:py-3">
        <span aria-hidden="true" className="mb-1 block text-[11px] font-semibold text-zinc-500 lg:hidden">
          Settings
        </span>
        <div className="flex justify-start lg:justify-end">
          <button
            type="button"
            onClick={() => {
              if (!opening) onOpenSettings();
            }}
            disabled={!tauriAvailable}
            aria-disabled={!tauriAvailable || opening}
            aria-label={tauriAvailable
              ? `${opening ? "Opening" : "Open"} ${settingsDestination} for ${row.label}`
              : `${settingsDestination} for ${row.label} is unavailable; requires Tobkiri Launcher`}
            className="inline-flex min-h-11 min-w-11 items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-3 text-xs font-semibold text-zinc-300 transition-colors hover:border-zinc-700 hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300 disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 motion-reduce:transition-none"
          >
            {opening
              ? <Loader2 aria-hidden="true" size={13} className="animate-spin motion-reduce:animate-none" />
              : <ExternalLink aria-hidden="true" size={13} />}
            {opening ? "Opening" : tauriAvailable ? "Open settings" : "Desktop only"}
          </button>
        </div>
      </td>
    </tr>
  );
}

function TableCell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <td className="block py-2 align-top lg:table-cell lg:px-3 lg:py-3">
      <span aria-hidden="true" className="mb-1 block text-[11px] font-semibold text-zinc-500 lg:hidden">
        {label}
      </span>
      <div className="min-w-0">{children}</div>
    </td>
  );
}

function StatusBadge({ status }: { status: HostPermissionBucket }) {
  return (
    <span
      className={cn(
        "inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold",
        statusClassName(status),
      )}
    >
      {hostPermissionStatusLabel(status)}
    </span>
  );
}

function StatusDot({ status }: { status: HostPermissionBucket }) {
  const label = `Overall status: ${hostPermissionStatusLabel(status)}`;
  if (status === "approved") {
    return (
      <span className="shrink-0 text-emerald-300">
        <CheckCircle2 aria-hidden="true" size={15} />
        <span className="sr-only">{label}</span>
      </span>
    );
  }
  if (status === "unknown" || status === "unsupported") {
    return (
      <span className="shrink-0 text-zinc-500">
        <ShieldQuestion aria-hidden="true" size={15} />
        <span className="sr-only">{label}</span>
      </span>
    );
  }
  return (
    <span className="shrink-0 text-amber-300">
      <AlertTriangle aria-hidden="true" size={15} />
      <span className="sr-only">{label}</span>
    </span>
  );
}

function hostPermissionStatusSignature(rows: HostPermissionRow[]): string {
  return rows
    .map((row) => `${row.id}:${row.rumiStatus}:${row.osStatus}`)
    .join("|");
}

function hostSettingsDestination(platform: string | undefined): string {
  const value = String(platform ?? "").toLowerCase();
  if (value === "darwin" || value === "macos") return "macOS System Settings";
  if (value.startsWith("win")) return "Windows Settings";
  if (value === "linux") return "Linux permission settings";
  return "OS permission settings";
}

function cssSafeId(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]+/g, "-");
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
