import { useEffect, useMemo, useState } from "react";
import { Bot, ClipboardCheck, Copy, Cpu, KeyRound, Link2, ListChecks, Monitor, Network, PackageCheck, Shield, UserCheck } from "lucide-react";

import { cn } from "../../lib/cn";
import { sandboxesApi } from "../../features/sandboxes/api";
import type { DesktopInstance, RuntimeIsolationFacts } from "../../features/sandboxes/types";
import { ErrorNotice } from "../ErrorNotice";

type DesktopInspectorProps = {
  desktop: DesktopInstance | null;
  hasLease: boolean;
  leaseError?: string | null;
  actionError?: string | null;
  accessMessage?: string | null;
  onRequestAccess?: (seatId: string) => void;
  onGrantAccess?: (seatId: string, requestId: string) => void;
};

function factRow(label: string, value: string, tone: "default" | "warning" = "default") {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-zinc-900 py-2 last:border-b-0">
      <span className="text-zinc-500">{label}</span>
      <span className={cn("max-w-[180px] text-right text-zinc-200", tone === "warning" && "text-amber-200")}>{value}</span>
    </div>
  );
}

function isolationRows(isolation: RuntimeIsolationFacts | null | undefined, providerId?: string | null) {
  if (!isolation) {
    return [factRow("Facts", "Unavailable from backend", "warning")];
  }
  const rows = [
    factRow("Mode", isolation.summary || isolation.mode || "Backend-defined"),
  ];
  if (providerId === "linux_native" || isolation.mode === "linux_native") {
    rows.push(factRow("VM isolation", "No VM claimed", "warning"));
  } else {
    rows.push(factRow("VM isolation", isolation.vm ? "Yes" : "No"));
  }
  rows.push(factRow("Container", isolation.container ? "Yes" : "No"));
  rows.push(factRow("Host process namespace", isolation.host_process_namespace ? "Shared" : "Isolated", isolation.host_process_namespace ? "warning" : "default"));
  rows.push(factRow("Host filesystem", isolation.host_filesystem_shared ? "Shared" : "Backend-limited", isolation.host_filesystem_shared ? "warning" : "default"));
  rows.push(factRow("Host network", isolation.host_network_shared ? "Shared" : "Backend-limited", isolation.host_network_shared ? "warning" : "default"));
  if (typeof isolation.sandbox_workspace_shared === "boolean") {
    rows.push(factRow("Sandbox workspace", isolation.sandbox_workspace_shared ? "Shared" : "Per instance", isolation.sandbox_workspace_shared ? "warning" : "default"));
  }
  if (typeof isolation.sandbox_process_namespace_shared === "boolean") {
    rows.push(factRow("Sandbox process namespace", isolation.sandbox_process_namespace_shared ? "Shared" : "Isolated", isolation.sandbox_process_namespace_shared ? "warning" : "default"));
  }
  if (typeof isolation.sandbox_network_namespace_shared === "boolean") {
    rows.push(factRow("Sandbox network namespace", isolation.sandbox_network_namespace_shared ? "Shared" : "Isolated", isolation.sandbox_network_namespace_shared ? "warning" : "default"));
  }
  if (isolation.sandbox_cgroup_scope) {
    rows.push(factRow("Sandbox cgroup scope", isolation.sandbox_cgroup_scope, isolation.sandbox_cgroup_scope === "not_claimed" ? "warning" : "default"));
  }
  if (isolation.sandbox_operation_binding) {
    rows.push(factRow("Operation binding", isolation.sandbox_operation_binding));
  }
  return rows;
}

function desktopRuleIds(desktop: DesktopInstance): string[] {
  if (Array.isArray(desktop.rules)) return desktop.rules;
  return desktop.rules?.rule_ids ?? [];
}

function desktopRole(desktop: DesktopInstance): string | null {
  if (Array.isArray(desktop.rules)) return desktop.role ?? null;
  return desktop.rules?.role ?? desktop.role ?? null;
}

export function DesktopInspector({
  desktop,
  hasLease,
  leaseError,
  actionError,
  accessMessage,
  onRequestAccess,
  onGrantAccess,
}: DesktopInspectorProps) {
  const [copiedAction, setCopiedAction] = useState<string | null>(null);
  const [grantRequestId, setGrantRequestId] = useState("");
  const [grants, setGrants] = useState<Array<Record<string, unknown>>>([]);
  const shareLink = useMemo(() => {
    if (!desktop?.seat_id || typeof window === "undefined") return "";
    const url = new URL(window.location.href);
    url.searchParams.set("desktop", desktop.seat_id);
    url.searchParams.delete("desktop_access_key");
    url.searchParams.delete("access_key");
    return url.toString();
  }, [desktop?.seat_id]);

  useEffect(() => {
    if (!desktop?.seat_id) {
      setGrants([]);
      return;
    }
    let active = true;
    void sandboxesApi.listDesktopGrants(desktop.seat_id)
      .then((result) => { if (active) setGrants(result.grants); })
      .catch(() => { if (active) setGrants([]); });
    return () => { active = false; };
  }, [desktop?.seat_id]);

  const revokeGrant = (grantId: string) => {
    if (!desktop?.seat_id) return;
    void sandboxesApi.revokeDesktopGrant(desktop.seat_id, grantId).then(() => {
      setGrants((current) => current.map((grant) => (
        grant.credential_id === grantId || grant.code_id === grantId
          ? { ...grant, status: "revoked" }
          : grant
      )));
    });
  };

  const copyText = (label: string, text: string) => {
    if (!navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(text).then(() => {
      setCopiedAction(label);
      window.setTimeout(() => setCopiedAction(null), 1600);
    });
  };

  if (!desktop) {
    return (
      <aside className="rounded-lg border border-zinc-800/70 bg-[#0a0a0c] p-4">
        <Monitor size={22} className="text-zinc-600" />
        <p className="mt-3 text-sm font-semibold text-zinc-200">No desktop selected</p>
        <p className="mt-1 text-xs text-zinc-500">Desktop status and isolation facts appear after the backend returns a seat.</p>
      </aside>
    );
  }

  return (
    <aside className="grid gap-3 rounded-lg border border-zinc-800/70 bg-[#0a0a0c] p-4 text-xs">
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold text-zinc-100">{desktop.name}</p>
        <div className="mt-1 flex items-center gap-1">
          <p className="min-w-0 flex-1 truncate font-mono text-[11px] text-zinc-500">{desktop.seat_id}</p>
          <button
            type="button"
            title="Copy desktop id"
            onClick={() => copyText("id", desktop.seat_id)}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-900 hover:text-zinc-100"
          >
            <Copy size={13} />
          </button>
          <button
            type="button"
            title="Copy desktop link"
            onClick={() => copyText("link", shareLink)}
            disabled={!shareLink}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-900 hover:text-zinc-100 disabled:opacity-40"
          >
            <Link2 size={13} />
          </button>
          <button
            type="button"
            title="Copy use-this-desktop prompt"
            onClick={() => copyText("use", `Use desktop ${desktop.seat_id} for this task.`)}
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-900 hover:text-zinc-100"
          >
            <ClipboardCheck size={13} />
          </button>
        </div>
        {copiedAction && <p className="mt-1 text-[11px] text-emerald-300">Copied {copiedAction}.</p>}
      </div>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          <Cpu size={13} />
          <span className="font-semibold">Status/provider</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {factRow("Status", desktop.status)}
          {factRow("Provider", desktop.provider_label || desktop.provider_id || "Pending")}
          {factRow("Template", desktop.template_id || "Unknown")}
          {factRow("Resolution", desktop.resolution ? `${desktop.resolution.width} x ${desktop.resolution.height}` : "Unknown")}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          <Shield size={13} />
          <span className="font-semibold">Isolation</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {isolationRows(desktop.isolation, desktop.provider_id).map((row, index) => (
            <div key={index}>{row}</div>
          ))}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          <Network size={13} />
          <span className="font-semibold">Workspace/network</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {factRow("Workspace", desktop.workspace?.label || desktop.workspace?.workspace_id || "None")}
          {factRow("Access", desktop.workspace?.access || "Backend policy")}
          {factRow("Network", desktop.network_policy?.summary || desktop.network_policy?.default || "Backend policy")}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          <ListChecks size={13} />
          <span className="font-semibold">Role/rules</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {factRow("Role", desktopRole(desktop) || "Default")}
          {factRow("Rules", desktopRuleIds(desktop).length ? desktopRuleIds(desktop).join(", ") : "None")}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          <KeyRound size={13} />
          <span className="font-semibold">Access</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {factRow("Mode", desktop.access_policy?.mode || "owner_only")}
          {factRow("Credential", "Scoped session (secret hidden)")}
          {factRow("Link", desktop.access_policy?.link_enabled ? "Enabled" : "No", desktop.access_policy?.link_enabled ? "warning" : "default")}
          {factRow("Request", desktop.access_policy?.request_required ? "Required" : "No")}
        </div>
        {desktop.access_policy?.request_required && (
          <div className="mt-2 grid gap-2">
            <button
              type="button"
              onClick={() => onRequestAccess?.(desktop.seat_id)}
              className="h-8 rounded-md border border-zinc-800 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900"
            >
              Request access
            </button>
            <div className="flex gap-2">
              <label className="min-w-0 flex-1">
                <span className="sr-only">Request id</span>
                <input
                  value={grantRequestId}
                  onChange={(event) => setGrantRequestId(event.target.value)}
                  placeholder="Request id"
                  className="h-8 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-100 outline-none focus:border-zinc-600"
                />
              </label>
              <button
                type="button"
                onClick={() => {
                  const requestId = grantRequestId.trim();
                  if (requestId) onGrantAccess?.(desktop.seat_id, requestId);
                }}
                disabled={!grantRequestId.trim()}
                className="h-8 rounded-md border border-zinc-800 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Grant
              </button>
            </div>
          </div>
        )}
        {accessMessage && <p className="mt-2 text-[11px] text-emerald-300">{accessMessage}</p>}
        <div className="mt-2 grid gap-2" aria-label="Scoped desktop access grants">
          {grants.map((grant) => {
            const grantId = String(grant.credential_id || grant.code_id || "");
            const status = String(grant.status || "unknown");
            return (
              <div key={grantId} className="rounded-md border border-zinc-800 p-2">
                <p className="truncate text-zinc-300">{String(grant.principal_id || "Unknown principal")}</p>
                <p className="truncate text-[10px] text-zinc-500">
                  Device {String(grant.device_id || "unknown")} · {status} · expires {String(grant.expires_at || "unknown")}
                </p>
                {status === "active" && (
                  <button
                    type="button"
                    onClick={() => revokeGrant(grantId)}
                    className="mt-1 rounded border border-red-500/30 px-2 py-1 text-[10px] text-red-200 hover:bg-red-500/10"
                  >
                    Revoke access
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          <PackageCheck size={13} />
          <span className="font-semibold">Provisioning</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {factRow("Apps", desktop.provisioning?.apps?.length ? desktop.provisioning.apps.join(", ") : "Template default")}
          {factRow("MCP", desktop.provisioning?.mcp_servers?.length ? desktop.provisioning.mcp_servers.join(", ") : "None")}
          {factRow("Status", desktop.provisioning?.status || "declared")}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center gap-1.5 text-zinc-300">
          {hasLease ? <UserCheck size={13} /> : <Bot size={13} />}
          <span className="font-semibold">Agent/control</span>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3">
          {factRow("Assigned agent", desktop.assigned_agent || "Unassigned")}
          {factRow("Control", hasLease ? "Human takeover" : desktop.control?.holder === "ai" ? "AI" : "Available")}
          {desktop.control?.message && factRow("Control note", desktop.control.message)}
        </div>
      </section>

      {(desktop.last_error || leaseError || actionError) && (
        <ErrorNotice
          message={actionError
            || leaseError
            || (typeof desktop.last_error === "string" ? desktop.last_error : desktop.last_error?.message)
            || "Unknown desktop error."}
          title="Latest issue"
          copyLabel="デスクトップのエラーをコピー"
        />
      )}
    </aside>
  );
}
