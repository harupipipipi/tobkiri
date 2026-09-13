import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, ChevronDown, Copy, KeyRound, Link2, Loader2, RefreshCw, Route, Server, ShieldAlert, ShieldCheck } from "lucide-react";

import { cn } from "../../lib/cn";
import { ErrorNotice } from "../../components/ErrorNotice";
import type { SettingsFieldRendererProps } from "../../renderers/settings/fieldRendererRegistry";
import {
  continuityApi,
  type ContinuityHandoffOperation,
  type ContinuityHandoffPlan,
  type ContinuityNode,
  type ContinuityPairingStartResponse,
  type ContinuityPreflightResult,
  type ContinuityProviderRoute,
} from "./api";

type ContinuityFieldConfig = {
  sandbox_id: string;
  destination_node_id: string;
  route_id: string;
  mode: string;
  last_operation_id?: string;
};

type ContinuityInitialData = {
  local_node?: ContinuityNode | null;
  nodes: ContinuityNode[];
  routes: ContinuityProviderRoute[];
  operations: ContinuityHandoffOperation[];
  plan?: ContinuityHandoffPlan | null;
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function stringValue(value: unknown, fallback = ""): string {
  const text = String(value ?? "").trim();
  return text || fallback;
}

function configFromValue(value: unknown): ContinuityFieldConfig {
  const raw = asRecord(value);
  return {
    sandbox_id: stringValue(raw.sandbox_id ?? raw.seat_id, "logical-sandbox"),
    destination_node_id: stringValue(raw.destination_node_id ?? raw.node_id),
    route_id: stringValue(raw.route_id),
    mode: stringValue(raw.mode, "move"),
    last_operation_id: stringValue(raw.last_operation_id) || undefined,
  };
}

function listFromRecord<T>(value: unknown): T[] {
  return Array.isArray(value) ? value.filter((item): item is T => Boolean(item && typeof item === "object")) : [];
}

function initialDataFromDefault(value: unknown): ContinuityInitialData {
  const raw = asRecord(value);
  return {
    local_node: (raw.local_node && typeof raw.local_node === "object" ? raw.local_node as ContinuityNode : null),
    nodes: listFromRecord<ContinuityNode>(raw.nodes),
    routes: listFromRecord<ContinuityProviderRoute>(raw.routes),
    operations: listFromRecord<ContinuityHandoffOperation>(raw.operations),
    plan: raw.plan && typeof raw.plan === "object" ? raw.plan as ContinuityHandoffPlan : null,
  };
}

function routeLabel(route: ContinuityProviderRoute | undefined): string {
  if (!route) return "No provider route";
  return route.qualified_route || [route.provider_id, route.api_id, route.model_id].filter(Boolean).join("/");
}

function nodeLabel(node: ContinuityNode | undefined): string {
  if (!node) return "No destination";
  return node.display_name || node.node_id;
}

function statusTone(ok: boolean | undefined): string {
  if (ok === true) return "border-emerald-500/30 bg-emerald-500/10 text-emerald-200";
  if (ok === false) return "border-amber-500/30 bg-amber-500/10 text-amber-200";
  return "border-zinc-800 bg-zinc-950 text-zinc-400";
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : "Continuity request failed.";
}

function latestOperation(operations: ContinuityHandoffOperation[]): ContinuityHandoffOperation | null {
  return operations[0] ?? null;
}

function operationStatusLabel(status: string | undefined): string {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "completed") return "Completed";
  if (normalized === "failed") return "Failed";
  if (normalized === "running" || normalized === "handoff-started") return "In progress";
  if (normalized === "cancelled" || normalized === "canceled") return "Canceled";
  return status || "Not started";
}

function checkLabel(value: unknown): string {
  const text = String(value || "Check").replace(/[_-]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Check";
}

function formatBytes(value: unknown): string {
  const bytes = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "Size pending";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${Math.round(bytes / (1024 * 1024))} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function copyText(text: string, onCopied: (label: string) => void) {
  if (!navigator.clipboard?.writeText) return;
  void navigator.clipboard.writeText(text).then(() => onCopied("copied")).catch(() => undefined);
}

export function ContinuitySettingsField({
  sectionId,
  field,
  value,
  onChange,
}: SettingsFieldRendererProps) {
  const initial = useMemo(() => initialDataFromDefault(field.default), [field.default]);
  const config = useMemo(() => configFromValue(value ?? field.default), [field.default, value]);
  const [localNode, setLocalNode] = useState<ContinuityNode | null>(initial.local_node ?? null);
  const [nodes, setNodes] = useState<ContinuityNode[]>(initial.nodes);
  const [routes, setRoutes] = useState<ContinuityProviderRoute[]>(initial.routes);
  const [operations, setOperations] = useState<ContinuityHandoffOperation[]>(initial.operations);
  const [selectedNodeId, setSelectedNodeId] = useState(config.destination_node_id);
  const [selectedRouteId, setSelectedRouteId] = useState(config.route_id);
  const [sandboxId, setSandboxId] = useState(config.sandbox_id);
  const [mode, setMode] = useState(config.mode);
  const [pairing, setPairing] = useState<ContinuityPairingStartResponse | null>(null);
  const [probe, setProbe] = useState<ContinuityPreflightResult | null>(null);
  const [plan, setPlan] = useState<ContinuityHandoffPlan | null>(initial.plan ?? null);
  const [loading, setLoading] = useState(false);
  const [action, setAction] = useState<"idle" | "pairing" | "probe" | "plan" | "handoff">("idle");
  const [error, setError] = useState("");
  const [copyState, setCopyState] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);

  const destinationNodes = nodes.filter((node) => node.destination_kind !== "source");
  const primaryNode = nodes.find((node) => node.destination_kind === "source") ?? localNode;
  const selectedNode = destinationNodes.find((node) => node.node_id === selectedNodeId) ?? destinationNodes[0];
  const portableRoutes = routes.filter((route) => route.portable);
  const selectedRoute = routes.find((route) => route.route_id === selectedRouteId) ?? portableRoutes[0] ?? routes[0];
  const operation = latestOperation(operations);
  const canRun = Boolean(selectedNode?.node_id && selectedRoute?.route_id && selectedRoute.portable);
  const completed = String(operation?.status || "").toLowerCase() === "completed";
  const estimate = asRecord(plan?.checkpoint_estimate);

  const persistConfig = (patch: Partial<ContinuityFieldConfig>) => {
    const next = {
      ...config,
      sandbox_id: sandboxId,
      destination_node_id: selectedNode?.node_id || selectedNodeId,
      route_id: selectedRoute?.route_id || selectedRouteId,
      mode,
      ...patch,
    };
    onChange(sectionId, field.id, next);
  };

  const refresh = async () => {
    setLoading(true);
    setError("");
    try {
      const [nodeResult, routeResult, operationResult] = await Promise.all([
        continuityApi.listNodes(),
        continuityApi.listProviderRoutes(),
        continuityApi.listHandoffs(),
      ]);
      setLocalNode(nodeResult.local_node ?? null);
      setNodes(nodeResult.nodes ?? []);
      setRoutes(routeResult.routes ?? []);
      setOperations(operationResult.operations ?? []);
      const nextDestination = (nodeResult.nodes ?? []).find((node) => node.destination_kind !== "source");
      const nextRoute = (routeResult.routes ?? []).find((route) => route.portable) ?? (routeResult.routes ?? [])[0];
      if (!selectedNodeId && nextDestination?.node_id) setSelectedNodeId(nextDestination.node_id);
      if (!selectedRouteId && nextRoute?.route_id) setSelectedRouteId(nextRoute.route_id);
    } catch (refreshError) {
      setError(formatError(refreshError));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    // The first load should happen once; field.default already seeds SSR and tests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handoffPayload = () => ({
    sandbox_id: sandboxId.trim() || "logical-sandbox",
    destination_node_id: selectedNode?.node_id || selectedNodeId,
    route_id: selectedRoute?.route_id || selectedRouteId,
    mode,
    credential_ttl_seconds: 3600,
  });

  const handleProbe = async () => {
    if (!selectedRoute?.route_id) return;
    setAction("probe");
    setError("");
    try {
      const result = await continuityApi.probeProviderRoute(selectedRoute.route_id, selectedNode?.node_id || selectedNodeId);
      setProbe(result);
    } catch (probeError) {
      setError(formatError(probeError));
    } finally {
      setAction("idle");
    }
  };

  const handlePlan = async () => {
    if (!canRun) return;
    setAction("plan");
    setError("");
    try {
      const result = await continuityApi.planHandoff(handoffPayload());
      setPlan(result.plan);
      persistConfig({});
    } catch (planError) {
      setError(formatError(planError));
    } finally {
      setAction("idle");
    }
  };

  const handlePairingCode = async () => {
    setAction("pairing");
    setError("");
    try {
      const result = await continuityApi.startPairing("Rumi destination");
      setPairing(result);
    } catch (pairingError) {
      setError(formatError(pairingError));
    } finally {
      setAction("idle");
    }
  };

  const handoffPrompt = [
    "Use this continuity destination for the next handoff.",
    `sandbox_id: ${sandboxId.trim() || "logical-sandbox"}`,
    `destination_node_id: ${selectedNode?.node_id || selectedNodeId || "unselected"}`,
    `provider_route: ${routeLabel(selectedRoute)}`,
  ].join("\n");

  return (
    <div className="space-y-4" data-settings-renderer="continuity" data-continuity-settings>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-medium text-zinc-200">
            <Server size={15} />
            <span>{field.label || "Continuity Handoff"}</span>
          </div>
          <p className="mt-1 text-xs text-zinc-500">
            {localNode?.display_name || "This device"} · {destinationNodes.length} destinations · {routes.length} provider routes
          </p>
          <p className="mt-1 text-xs text-zinc-400">
            Current primary: <span className="text-zinc-200">{nodeLabel(primaryNode ?? undefined)}</span>
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-2.5 text-xs text-zinc-300 hover:border-zinc-700"
        >
          {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          Refresh
        </button>
      </div>

      {error && (
        <ErrorNotice
          className="text-xs"
          copyLabel="継続性設定エラーをコピー"
          message={error}
        />
      )}

      <div className="rounded-md border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
        Handoff is planning-only in this build. The source remains primary; Rumi records a portable checkpoint plan but does not switch devices automatically.
      </div>

      <div className="grid gap-1.5 sm:grid-cols-3 lg:grid-cols-7">
        {["Source", "Destination", "Provider", "Preflight", "Plan", "Checkpoint", "Source primary"].map((step, index) => (
          <div
            key={step}
            className={cn(
              "rounded-md border px-2.5 py-2 text-[11px]",
              index <= (completed ? 6 : plan ? 3 : probe ? 3 : selectedNode ? 1 : 0)
                ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-100"
                : "border-zinc-800 bg-zinc-950 text-zinc-500",
            )}
          >
            {step}
          </div>
        ))}
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <label className="space-y-1.5">
          <span className="text-[11px] uppercase tracking-[0.18em] text-zinc-600">Sandbox</span>
          <input
            value={sandboxId}
            onChange={(event) => {
              setSandboxId(event.target.value);
              persistConfig({ sandbox_id: event.target.value });
            }}
            className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-600"
            placeholder="logical-sandbox"
          />
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] uppercase tracking-[0.18em] text-zinc-600">Destination</span>
          <select
            value={selectedNode?.node_id ?? selectedNodeId}
            onChange={(event) => {
              setSelectedNodeId(event.target.value);
              persistConfig({ destination_node_id: event.target.value });
            }}
            className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 text-xs text-zinc-200 outline-none focus:border-zinc-600"
          >
            {destinationNodes.length === 0 && <option value="">No paired destination</option>}
            {destinationNodes.map((node) => (
              <option key={node.node_id} value={node.node_id}>
                {nodeLabel(node)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] uppercase tracking-[0.18em] text-zinc-600">Mode</span>
          <select
            value={mode}
            onChange={(event) => {
              setMode(event.target.value);
              persistConfig({ mode: event.target.value });
            }}
            className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 text-xs text-zinc-200 outline-none focus:border-zinc-600"
          >
            <option value="move">Plan primary move</option>
            <option value="checkpoint">Checkpoint only</option>
            <option value="shadow">Shadow resume</option>
          </select>
        </label>
      </div>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="space-y-2 rounded-md border border-zinc-800 bg-zinc-950/60 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-zinc-300">
              <Route size={14} />
              Provider route
            </span>
            <span className={cn("rounded-full border px-2 py-0.5 text-[10px]", statusTone(selectedRoute?.portable))}>
              {selectedRoute?.portable ? "portable" : selectedRoute?.blocked_reason || "not ready"}
            </span>
          </div>
          <select
            value={selectedRoute?.route_id ?? selectedRouteId}
            onChange={(event) => {
              setSelectedRouteId(event.target.value);
              persistConfig({ route_id: event.target.value });
            }}
            className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 text-xs text-zinc-200 outline-none focus:border-zinc-600"
          >
            {routes.length === 0 && <option value="">No configured API route</option>}
            {routes.map((route) => (
              <option key={route.route_id} value={route.route_id}>
                {routeLabel(route)}
              </option>
            ))}
          </select>
          <p className="min-h-5 truncate font-mono text-[11px] text-zinc-500">
            {selectedRoute?.endpoint_class || "Endpoint pending"}
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!selectedRoute?.route_id || action !== "idle"}
              onClick={() => void handleProbe()}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-2.5 text-xs text-zinc-300 hover:border-zinc-700 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {action === "probe" ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}
              Check route
            </button>
            <button
              type="button"
              onClick={() => copyText(handoffPrompt, setCopyState)}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-2.5 text-xs text-zinc-300 hover:border-zinc-700"
              title="Copy handoff instruction"
            >
              <Copy size={13} />
              {copyState ? "Copied" : "Copy instruction"}
            </button>
          </div>
        </div>

        <div className="space-y-2 rounded-md border border-zinc-800 bg-zinc-950/60 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-zinc-300">
              <KeyRound size={14} />
              Paired destination
            </span>
            <span className="rounded-full border border-zinc-800 bg-zinc-950 px-2 py-0.5 text-[10px] text-zinc-500">
              {selectedNode?.online ? "online" : "needs check"}
            </span>
          </div>
          {pairing ? (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-3 rounded-md border border-zinc-800 bg-zinc-950 px-3 py-2">
                <span className="font-mono text-sm text-zinc-100">{pairing.code}</span>
                <button
                  type="button"
                  onClick={() => copyText(pairing.code, setCopyState)}
                  className="rounded p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
                  title="Copy pairing code"
                >
                  <Copy size={13} />
                </button>
              </div>
              <p className="text-[11px] text-zinc-500">
                Enter this code on the destination device, then refresh.
              </p>
            </div>
          ) : (
            <button
              type="button"
              disabled={action !== "idle"}
              onClick={() => void handlePairingCode()}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-2.5 text-xs text-zinc-300 hover:border-zinc-700 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {action === "pairing" ? <Loader2 size={13} className="animate-spin" /> : <Link2 size={13} />}
              Pair a new device
            </button>
          )}
          <p className="text-[11px] text-zinc-500">
            {selectedNode ? `${nodeLabel(selectedNode)} · ${selectedNode.platform || "platform unknown"} · ${selectedNode.online ? "online" : "offline"}` : "Add a paired destination before handoff."}
          </p>
        </div>
      </div>

      <div className="rounded-md border border-zinc-800 bg-zinc-950/40">
        <button
          type="button"
          onClick={() => setShowAdvanced((current) => !current)}
          className="flex min-h-10 w-full items-center justify-between gap-3 px-3 text-left text-xs text-zinc-300"
        >
          <span>Advanced routing details</span>
          <ChevronDown size={14} className={cn("transition-transform", showAdvanced && "rotate-180")} />
        </button>
        {showAdvanced && (
          <div className="grid gap-2 border-t border-zinc-800 px-3 py-3 text-[11px] text-zinc-500 sm:grid-cols-2">
            <div>
              <span className="block text-zinc-600">Credential reference</span>
              <span className="font-mono text-zinc-300">{selectedRoute?.credential_ref || "Not configured"}</span>
            </div>
            <div>
              <span className="block text-zinc-600">Envelope</span>
              <span className="font-mono text-zinc-300">X25519 handoff envelope</span>
            </div>
            <div>
              <span className="block text-zinc-600">Route id</span>
              <span className="font-mono text-zinc-300">{selectedRoute?.route_id || "Unselected"}</span>
            </div>
            <div>
              <span className="block text-zinc-600">Destination id</span>
              <span className="font-mono text-zinc-300">{selectedNode?.node_id || "Unselected"}</span>
            </div>
          </div>
        )}
      </div>

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px]">
        <div className="rounded-md border border-zinc-800 bg-zinc-950/60 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-zinc-300">
              {probe?.ok || plan?.status === "ready" ? <CheckCircle2 size={14} /> : <ShieldAlert size={14} />}
              Preflight
            </span>
            <span className={cn("rounded-full border px-2 py-0.5 text-[10px]", statusTone(probe?.ok ?? (plan ? plan.status === "ready" : undefined)))}>
              {probe ? (probe.ok ? "pass" : "blocked") : plan ? plan.status : "not run"}
            </span>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {(probe?.checks ?? []).slice(0, 3).map((check, index) => (
              <div key={`${String(check.code ?? "check")}-${index}`} className="rounded border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-[11px] text-zinc-400">
                <span className="text-zinc-200">{checkLabel(check.label ?? check.code)}</span>
              </div>
            ))}
            {probe?.ok === false && (probe.errors ?? []).slice(0, 3).map((item, index) => (
              <ErrorNotice
                className="px-2.5 py-2 text-[11px]"
                copyLabel="事前確認エラーをコピー"
                errorIcon="continuity-probe"
                key={`${String(item.code ?? "error")}-${index}`}
                message={checkLabel(item.message ?? item.code ?? "Needs attention")}
              />
            ))}
            {!probe && !plan && (
              <div className="rounded border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-[11px] text-zinc-500">
                Destination, route, credential envelope, and source lease will be checked before creating a handoff plan.
              </div>
            )}
          </div>
        </div>

        <div className="space-y-2">
          <div className="rounded-md border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px] text-zinc-400">
            <div className="flex items-center justify-between gap-2">
              <span>Estimated package</span>
              <span className="text-zinc-200">{formatBytes(estimate.total_bytes ?? estimate.bytes)}</span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2">
              <span>ETA</span>
              <span className="text-zinc-200">{stringValue(estimate.eta ?? estimate.eta_seconds, "Pending")}</span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2">
              <span>Source</span>
              <span className="text-zinc-200">Remains primary</span>
            </div>
          </div>
          <button
            type="button"
            disabled={!canRun || action !== "idle"}
            onClick={() => void handlePlan()}
            className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950 px-3 text-xs text-zinc-200 hover:border-zinc-700 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {action === "plan" ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}
            Review plan
          </button>
        </div>
      </div>

      {operation && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-xs">
          <span className="min-w-0 truncate text-zinc-400">
            Last operation <span className="font-mono text-zinc-200">{operation.operation_id}</span>
          </span>
          <span className={cn(
            "rounded-full border px-2 py-0.5",
            operation.status === "COMPLETED"
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
              : "border-zinc-800 bg-zinc-950 text-zinc-400",
          )}>
            {operationStatusLabel(operation.status)}
          </span>
        </div>
      )}
    </div>
  );
}
