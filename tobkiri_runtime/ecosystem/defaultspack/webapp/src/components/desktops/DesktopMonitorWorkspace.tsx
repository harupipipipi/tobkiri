import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Trash2, X } from "lucide-react";

import { sandboxesApi } from "../../features/sandboxes/api";
import { diagnosticsText } from "../../features/sandboxes/runtimeStatus";
import type { CreateDesktopRequest, DesktopInputAction, DesktopInstance } from "../../features/sandboxes/types";
import { useDesktopControlLease } from "../../features/sandboxes/useDesktopControlLease";
import { useDesktopInstances } from "../../features/sandboxes/useSandboxInstances";
import { useRuntimeDoctor } from "../../features/sandboxes/useRuntimeDoctor";
import { useSandboxTemplates } from "../../features/sandboxes/useSandboxTemplates";
import { cn } from "../../lib/cn";
import { ErrorNotice } from "../ErrorNotice";
import { DesktopCreateDialog } from "./DesktopCreateDialog";
import { DesktopGrid } from "./DesktopGrid";
import { DesktopInspector } from "./DesktopInspector";
import { DesktopProviderNotice } from "./DesktopProviderNotice";
import { type DesktopDensity, type DesktopFilter, DesktopToolbar } from "./DesktopToolbar";

export function shouldShowDesktopList({
  runtimeReady,
  desktopCount,
  loading,
  error,
}: {
  runtimeReady: boolean;
  desktopCount: number;
  loading: boolean;
  error?: string | null;
}) {
  return runtimeReady || desktopCount > 0 || loading || Boolean(error);
}

export function resolveVisibleSelectedDesktop(
  visibleDesktops: DesktopInstance[],
  selectedSeatId: string | null,
  options: { preserveSelected?: boolean } = {},
): DesktopInstance | null {
  const selectedDesktop = visibleDesktops.find((desktop) => desktop.seat_id === selectedSeatId);
  if (selectedDesktop && (options.preserveSelected || selectedDesktop.status === "running")) {
    return selectedDesktop;
  }
  return visibleDesktops.find((desktop) => desktop.status === "running")
    ?? selectedDesktop
    ?? visibleDesktops[0]
    ?? null;
}

export function resolveVisibleSelectedSeatId(
  visibleDesktops: DesktopInstance[],
  selectedSeatId: string | null,
  options: { preserveSelected?: boolean } = {},
): string | null {
  return resolveVisibleSelectedDesktop(visibleDesktops, selectedSeatId, options)?.seat_id ?? null;
}

export function clearLegacyDesktopCredentialsFromUrl(): boolean {
  if (typeof window === "undefined") return false;
  const query = new URLSearchParams(window.location.search);
  const found = query.has("desktop_access_key") || query.has("access_key");
  if (!found) return false;
  query.delete("desktop_access_key");
  query.delete("access_key");
  const nextUrl = `${window.location.pathname}${query.toString() ? `?${query.toString()}` : ""}${window.location.hash}`;
  window.history.replaceState(window.history.state, "", nextUrl);
  return true;
}

export function DesktopMonitorWorkspace() {
  const legacyCredentialWasRemoved = useRef(clearLegacyDesktopCredentialsFromUrl()).current;
  const runtime = useRuntimeDoctor({ autoRunDoctor: true });
  const runtimeReady = runtime.availability.status === "ready";
  const templates = useSandboxTemplates({ enabled: runtimeReady });
  const desktopInstances = useDesktopInstances({ pollIntervalMs: 2500 });
  const processedLinkedSeatIdRef = useRef<string | null>(null);
  const explicitSelectedSeatIdRef = useRef<string | null>(null);
  const [filter, setFilter] = useState<DesktopFilter>("all");
  const [density, setDensity] = useState<DesktopDensity>("comfortable");
  const [selectedSeatId, setSelectedSeatId] = useState<string | null>(null);
  const [pendingTakeoverSeatId, setPendingTakeoverSeatId] = useState<string | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(
    legacyCredentialWasRemoved
      ? "This desktop link used a retired access key. Ask the owner for fresh access."
      : null,
  );
  const [accessKeys, setAccessKeys] = useState<Record<string, string>>({});
  const [accessMessage, setAccessMessage] = useState<string | null>(null);
  const [diagnosticsCopied, setDiagnosticsCopied] = useState(false);
  const [stopTargetSeatId, setStopTargetSeatId] = useState<string | null>(null);
  const [deleteTargetSeatId, setDeleteTargetSeatId] = useState<string | null>(null);

  const runningCount = useMemo(
    () => desktopInstances.desktops.filter((desktop) => desktop.status === "running").length,
    [desktopInstances.desktops],
  );
  const visibleDesktops = useMemo(
    () => filter === "running"
      ? desktopInstances.desktops.filter((desktop) => desktop.status === "running")
      : desktopInstances.desktops,
    [desktopInstances.desktops, filter],
  );
  const preserveSelected = selectedSeatId !== null && explicitSelectedSeatIdRef.current === selectedSeatId;
  const selectedDesktop = resolveVisibleSelectedDesktop(visibleDesktops, selectedSeatId, { preserveSelected });
  const visibleSelectedSeatId = selectedDesktop?.seat_id ?? null;
  const selectedAccessKey = visibleSelectedSeatId ? accessKeys[visibleSelectedSeatId] || "" : "";
  const control = useDesktopControlLease(visibleSelectedSeatId, sandboxesApi, selectedAccessKey);
  const stopTarget = desktopInstances.desktops.find((desktop) => desktop.seat_id === stopTargetSeatId) ?? null;
  const deleteTarget = desktopInstances.desktops.find((desktop) => desktop.seat_id === deleteTargetSeatId) ?? null;

  useEffect(() => {
    if (typeof window === "undefined") return;
    const query = new URLSearchParams(window.location.search);
    const linkedSeatId = query.get("desktop");
    if (!linkedSeatId) return;
    if (processedLinkedSeatIdRef.current === linkedSeatId) return;
    if (desktopInstances.desktops.some((desktop) => desktop.seat_id === linkedSeatId)) {
      processedLinkedSeatIdRef.current = linkedSeatId;
      explicitSelectedSeatIdRef.current = linkedSeatId;
      setSelectedSeatId(linkedSeatId);
    }
  }, [desktopInstances.desktops]);

  useEffect(() => {
    const missingSeatIds = desktopInstances.desktops
      .map((desktop) => desktop.seat_id)
      .filter((seatId) => !accessKeys[seatId]);
    for (const seatId of missingSeatIds) {
      const operations = [
        "desktop.read", "desktop.frame", "desktop.start", "desktop.restart",
        "desktop.stop", "desktop.delete", "desktop.input", "desktop.ai_input",
        "desktop.rules.update", "desktop.control.acquire", "desktop.control.renew",
        "desktop.control.release",
      ];
      void sandboxesApi.issueDesktopExchange(seatId, operations)
        .then(({ exchange_code }) => sandboxesApi.redeemDesktopExchange(exchange_code))
        .then(({ session_credential }) => {
          setAccessKeys((current) => current[seatId]
            ? current
            : { ...current, [seatId]: session_credential });
        })
        .catch((error) => setActionError(
          error instanceof Error ? error.message : "Desktop session setup failed.",
        ));
    }
  }, [accessKeys, desktopInstances.desktops]);

  useEffect(() => {
    const preserveSelected = selectedSeatId !== null && explicitSelectedSeatIdRef.current === selectedSeatId;
    const nextSelectedSeatId = resolveVisibleSelectedSeatId(visibleDesktops, selectedSeatId, { preserveSelected });
    if (nextSelectedSeatId === selectedSeatId) return;
    if (explicitSelectedSeatIdRef.current === selectedSeatId) {
      explicitSelectedSeatIdRef.current = null;
    }
    setSelectedSeatId(nextSelectedSeatId);
  }, [selectedSeatId, visibleDesktops]);

  useEffect(() => {
    setAccessMessage(null);
  }, [selectedSeatId]);

  useEffect(() => {
    if (!pendingTakeoverSeatId || selectedSeatId !== pendingTakeoverSeatId) return;
    setPendingTakeoverSeatId(null);
    void control.acquire();
  }, [control, pendingTakeoverSeatId, selectedSeatId]);

  const handleCopyDiagnostics = useCallback(() => {
    const text = diagnosticsText({
      providersResponse: runtime.providersResponse,
      doctor: runtime.doctor,
      error: runtime.error || desktopInstances.error || templates.error,
    });
    setDiagnosticsCopied(false);
    if (!navigator.clipboard?.writeText) {
      setActionError(text);
      return;
    }
    void navigator.clipboard.writeText(text).then(() => {
      setDiagnosticsCopied(true);
      window.setTimeout(() => setDiagnosticsCopied(false), 1800);
    }).catch((copyError) => {
      setActionError(copyError instanceof Error ? copyError.message : "Diagnostics copy failed.");
    });
  }, [desktopInstances.error, runtime.doctor, runtime.error, runtime.providersResponse, templates.error]);

  const handleCreateDesktop = useCallback(async (request: CreateDesktopRequest) => {
    setCreating(true);
    setCreateError(null);
    try {
      const desktop = await sandboxesApi.createDesktop(request);
      explicitSelectedSeatIdRef.current = desktop.seat_id;
      setSelectedSeatId(desktop.seat_id);
      setIsCreateOpen(false);
      await desktopInstances.refresh();
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : "Desktop creation failed.");
    } finally {
      setCreating(false);
    }
  }, [desktopInstances]);

  const runDesktopAction = useCallback(async (
    seatId: string,
    action: "start" | "restart" | "stop" | "delete",
  ) => {
    setActionError(null);
    try {
      const accessKey = accessKeys[seatId] || undefined;
      if (action === "start") await sandboxesApi.startDesktop(seatId, accessKey);
      if (action === "restart") await sandboxesApi.restartDesktop(seatId, accessKey);
      if (action === "stop") await sandboxesApi.stopDesktop(seatId, accessKey);
      if (action === "delete") await sandboxesApi.deleteDesktop(seatId, accessKeys[seatId] || undefined);
      if (action === "stop") setStopTargetSeatId(null);
      if (action === "delete" && seatId === visibleSelectedSeatId) {
        if (explicitSelectedSeatIdRef.current === seatId) {
          explicitSelectedSeatIdRef.current = null;
        }
        setSelectedSeatId(null);
        setDeleteTargetSeatId(null);
      }
      await desktopInstances.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : `Desktop ${action} failed.`);
    }
  }, [accessKeys, desktopInstances, visibleSelectedSeatId]);

  const handleSelectDesktop = useCallback((seatId: string) => {
    explicitSelectedSeatIdRef.current = seatId;
    setSelectedSeatId(seatId);
  }, []);

  const handleTakeOver = useCallback((seatId: string) => {
    setActionError(null);
    if (seatId !== visibleSelectedSeatId) {
      explicitSelectedSeatIdRef.current = seatId;
      setSelectedSeatId(seatId);
      setPendingTakeoverSeatId(seatId);
      return;
    }
    void control.acquire();
  }, [control, visibleSelectedSeatId]);

  const handleDesktopInput = useCallback((seatId: string, input: DesktopInputAction) => {
    const token = control.lease?.lease_token;
    if (!token || seatId !== visibleSelectedSeatId) return;
    void sandboxesApi.sendDesktopInput(seatId, {
      ...input,
      lease_token: token,
      desktop_session_credential: accessKeys[seatId] || undefined,
    }).then(() => {
      setActionError(null);
    }).catch((error) => {
      setActionError(error instanceof Error ? error.message : "Desktop input failed.");
      void desktopInstances.refresh();
    });
  }, [accessKeys, control.lease?.lease_token, desktopInstances, visibleSelectedSeatId]);


  const handleRequestAccess = useCallback((seatId: string) => {
    setAccessMessage(null);
    void sandboxesApi.requestDesktopAccess(seatId, "Requested from the Desktops workspace.")
      .then((result) => {
        setAccessMessage(result.request_id ? `Access request ${result.request_id} recorded.` : result.message || "Access request recorded.");
      })
      .catch((error) => {
        setActionError(error instanceof Error ? error.message : "Desktop access request failed.");
      });
  }, []);

  const handleGrantAccess = useCallback((seatId: string, requestId: string) => {
    setAccessMessage(null);
    void sandboxesApi.grantDesktopAccess(seatId, requestId)
      .then((result) => {
        setAccessMessage(result.message || "Access request granted. A scoped session will be issued through the authenticated channel.");
      })
      .catch((error) => {
        setActionError(error instanceof Error ? error.message : "Desktop access grant failed.");
      });
  }, []);

  const providerNotice = (runtime.availability.status !== "ready" || runtime.operation || runtime.error || diagnosticsCopied) ? (
    <DesktopProviderNotice
      availability={runtime.availability}
      operation={runtime.operation}
      doctorLoading={runtime.doctorLoading}
      setupLoading={runtime.setupLoading}
      operationCancelLoading={runtime.operationCancelLoading}
      onSetup={() => void runtime.ensureRuntime(runtime.availability.selectedProvider?.provider_id)}
      onDoctor={() => void runtime.runDoctor()}
      onCancelOperation={() => void runtime.cancelRuntimeOperation()}
      onCopyDiagnostics={handleCopyDiagnostics}
    />
  ) : null;

  const canCreate = runtimeReady && !templates.loading && templates.desktopTemplates.length > 0;
  const setupMessage = diagnosticsCopied ? "Diagnostics copied." : null;
  const surfaceError = actionError || desktopInstances.error || templates.error || setupMessage;
  const showDesktopList = shouldShowDesktopList({
    runtimeReady,
    desktopCount: desktopInstances.desktops.length,
    loading: desktopInstances.loading,
    error: desktopInstances.error,
  });

  return (
    <section className="relative flex h-full min-h-0 flex-1 flex-col bg-[#09090b] text-zinc-300" aria-label="Desktops workspace">
      <DesktopToolbar
        totalCount={desktopInstances.desktops.length}
        runningCount={runningCount}
        filter={filter}
        density={density}
        doctorLoading={runtime.doctorLoading}
        canCreate={canCreate}
        onFilterChange={setFilter}
        onDensityChange={setDensity}
        onCreate={() => setIsCreateOpen(true)}
        onDoctor={() => void runtime.runDoctor()}
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        <div className="grid gap-2">
          {providerNotice}
          {setupMessage ? (
            <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-100">
              {setupMessage}
            </div>
          ) : surfaceError ? (
            <ErrorNotice
              className="text-xs"
              copyLabel="デスクトップワークスペースのエラーをコピー"
              message={surfaceError}
            />
          ) : null}

          {showDesktopList && (
            <div className="grid min-h-0 gap-2 min-[1280px]:grid-cols-[minmax(0,1fr)_300px] min-[1536px]:grid-cols-[minmax(0,1fr)_340px]">
              <DesktopGrid
                desktops={visibleDesktops}
                loading={desktopInstances.loading}
                selectedSeatId={visibleSelectedSeatId}
                density={density}
                leaseSeatId={control.lease?.seat_id ?? null}
                emptyReason={desktopInstances.desktops.length > 0 ? "filter" : "backend"}
                accessKeys={accessKeys}
                controlBusy={control.busy}
                onSelect={handleSelectDesktop}
                onTakeOver={handleTakeOver}
                onReturnToAI={() => void control.release()}
                onInput={handleDesktopInput}
                onStart={(seatId) => void runDesktopAction(seatId, "start")}
                onRestart={(seatId) => void runDesktopAction(seatId, "restart")}
                onStop={setStopTargetSeatId}
                onDelete={setDeleteTargetSeatId}
              />
              <DesktopInspector
                desktop={selectedDesktop}
                hasLease={Boolean(control.lease)}
                leaseError={control.error}
                actionError={actionError}
                accessMessage={accessMessage}
                onRequestAccess={handleRequestAccess}
                onGrantAccess={handleGrantAccess}
              />
            </div>
          )}
        </div>
      </div>

      <DesktopCreateDialog
        isOpen={isCreateOpen}
        templates={templates.desktopTemplates}
        providers={runtime.availability.providers}
        selectedProviderId={runtime.availability.selectedProvider?.provider_id}
        loading={creating}
        error={createError}
        onClose={() => {
          setIsCreateOpen(false);
          setCreateError(null);
        }}
        onCreate={handleCreateDesktop}
      />

      {stopTarget && (
        <div className="absolute inset-0 rumi-layer-modal flex items-center justify-center bg-black/60 p-4">
          <div role="dialog" aria-modal="true" aria-labelledby="desktop-stop-title" className="w-[min(420px,100%)] rounded-lg border border-amber-500/25 bg-[#0b0b0d] shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-amber-500/20 px-4 py-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-amber-500/25 bg-amber-500/10 text-amber-100">
                  <AlertTriangle size={15} />
                </span>
                <div className="min-w-0">
                  <p id="desktop-stop-title" className="truncate text-sm font-semibold text-zinc-100">Stop Desktop</p>
                  <p className="truncate text-xs text-zinc-500">{stopTarget.name}</p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setStopTargetSeatId(null)}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                aria-label="Close stop confirmation"
              >
                <X size={15} />
              </button>
            </div>
            <div className="px-4 py-4 text-sm leading-6 text-zinc-300">
              This stops the desktop session and releases its cached frame and active control lease.
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-zinc-800/70 px-4 py-3">
              <button
                type="button"
                onClick={() => setStopTargetSeatId(null)}
                className="h-8 rounded-md border border-zinc-800 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void runDesktopAction(stopTarget.seat_id, "stop")}
                className="h-8 rounded-md bg-amber-400 px-3 text-xs font-semibold text-zinc-950 hover:bg-amber-300"
              >
                Stop
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="absolute inset-0 rumi-layer-modal flex items-center justify-center bg-black/60 p-4">
          <div role="dialog" aria-modal="true" aria-labelledby="desktop-delete-title" className="w-[min(420px,100%)] rounded-lg border border-red-500/25 bg-[#0b0b0d] shadow-2xl">
            <div className="flex items-start justify-between gap-3 border-b border-red-500/20 px-4 py-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-red-500/25 bg-red-500/10 text-red-200">
                  <AlertTriangle size={15} />
                </span>
                <div className="min-w-0">
                  <p id="desktop-delete-title" className="truncate text-sm font-semibold text-zinc-100">Delete Desktop</p>
                  <p className="truncate text-xs text-zinc-500">{deleteTarget.name}</p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setDeleteTargetSeatId(null)}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                aria-label="Close delete confirmation"
              >
                <X size={15} />
              </button>
            </div>
            <div className="px-4 py-4 text-sm leading-6 text-zinc-300">
              This removes the desktop session and clears its cached frame and control lease.
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-zinc-800/70 px-4 py-3">
              <button
                type="button"
                onClick={() => setDeleteTargetSeatId(null)}
                className="h-8 rounded-md border border-zinc-800 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void runDesktopAction(deleteTarget.seat_id, "delete")}
                className="inline-flex h-8 items-center gap-1.5 rounded-md bg-red-500 px-3 text-xs font-semibold text-white hover:bg-red-400"
              >
                <Trash2 size={13} />
                <span>Delete</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
