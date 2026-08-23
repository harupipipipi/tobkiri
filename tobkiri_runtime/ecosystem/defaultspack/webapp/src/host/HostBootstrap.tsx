import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import { TobkiriLoadingScreen } from "../components/TobkiriLoadingScreen";
import { defaultspackApiFetch, defaultspackContractRoute } from "../lib/api";
import { ErrorNotice } from "../components/ErrorNotice";
import { AuthorityApprovalWindow } from "../components/AuthorityApprovalWindow";
import {
  DynamicFrontendHost,
  contributionsForRoute,
} from "./DynamicFrontendHost";
import type {
  CapturedCapabilityInvocation,
  FrontendCapabilityInvoker,
  FrontendCatalog,
} from "./frontendContracts";
import {
  applicationPathname,
  parseProfileScreenPath,
  profileScreenPath,
} from "../lib/profileRoute";

type ApiEnvelope<T> = {
  success: boolean;
  data?: T;
  error?: string | null;
};

type UiCatalogEnvelope = {
  dynamic_host?: FrontendCatalog | null;
};

export type UiReadinessProbe = {
  status: "UP" | "DOWN" | "DEGRADED" | "UNKNOWN";
  code: string;
};

export type UiReadinessSnapshot = {
  schema: "io.tobkiri.ui-readiness.v1";
  status: "UP" | "DOWN" | "DEGRADED";
  ready: boolean;
  mode?: string;
  probes: Record<string, UiReadinessProbe>;
};

const REQUIRED_UI_READINESS_PROBES = [
  "static_bundle",
  "chat_route",
  "ui_catalog",
  "settings",
  "model_catalog",
  "tool_catalog",
  "conversation_bootstrap",
  "default_conversation_load",
  "auth_session",
] as const;

export class FrontendCapabilityError extends Error {
  code?: string;

  constructor(message: string, code?: string) {
    super(message);
    this.name = "FrontendCapabilityError";
    this.code = code;
  }
}

export async function fetchDynamicCatalog(): Promise<FrontendCatalog> {
  const response = await defaultspackApiFetch(defaultspackContractRoute("api/ui/catalog"), {
    cache: "no-store",
  });
  const envelope = await response.json() as ApiEnvelope<UiCatalogEnvelope>;
  const catalog = envelope.data?.dynamic_host;
  if (!response.ok || envelope.success !== true || !catalog) {
    throw new Error("dynamic_frontend_catalog_unavailable");
  }
  return catalog;
}

export function uiReadinessFailureSummary(snapshot: UiReadinessSnapshot): string {
  const failures = Object.entries(snapshot.probes)
    .filter(([, probe]) => probe.status !== "UP")
    .map(([name, probe]) => `${name} (${probe.code || "INVALID_PROBE"})`);
  return failures.length > 0
    ? `UI readiness ${snapshot.status}: ${failures.join(", ")}`
    : `UI readiness ${snapshot.status}: readiness contract was not satisfied`;
}

export function validUiReadiness(snapshot: UiReadinessSnapshot): boolean {
  const complete = REQUIRED_UI_READINESS_PROBES.every((name) => {
    const probe = snapshot.probes?.[name];
    return probe
      && ["UP", "DOWN", "DEGRADED", "UNKNOWN"].includes(probe.status)
      && typeof probe.code === "string"
      && probe.code.trim().length > 0;
  });
  if (!complete || snapshot.schema !== "io.tobkiri.ui-readiness.v1") return false;
  if (snapshot.status === "UP") return snapshot.ready === true;
  return snapshot.status === "DEGRADED"
    && snapshot.ready === true
    && snapshot.mode === "profile_reconfirmation_required";
}

async function fetchUiReadiness(): Promise<UiReadinessSnapshot> {
  const response = await defaultspackApiFetch("/ui-readiness", {
    cache: "no-store",
    credentials: "same-origin",
  });
  const envelope = await response.json() as ApiEnvelope<UiReadinessSnapshot>;
  const snapshot = envelope.data;
  if (!response.ok || envelope.success !== true || !snapshot) {
    throw new Error("readiness_endpoint (READINESS_ENDPOINT_UNAVAILABLE)");
  }
  if (!validUiReadiness(snapshot)) {
    throw new Error(uiReadinessFailureSummary(snapshot));
  }
  return snapshot;
}

export async function invokeCapability(
  request: CapturedCapabilityInvocation,
): Promise<unknown> {
  const response = await defaultspackApiFetch(defaultspackContractRoute("api/ui/capability/invoke"), {
    method: "POST",
    cache: "no-store",
    body: JSON.stringify({
      request_id: crypto.randomUUID(),
      expires_at: Date.now() / 1000 + 30,
      profile_id: request.profileId,
      profile_revision: request.profileRevision,
      activation_id: request.activationId,
      plan_hash: request.planHash,
      catalog_hash: request.catalogHash,
      contribution_id: request.contributionId,
      owner_pack_id: request.ownerPackId,
      contract_id: request.contractId,
      payload: request.payload,
    }),
  });
  const envelope = await response.json() as ApiEnvelope<unknown>;
  if (!response.ok || envelope.success !== true) {
    const failureData = asRecord(envelope.data);
    const message = typeof envelope.error === "string"
      ? envelope.error
      : typeof failureData?.message === "string"
        ? failureData.message
        : undefined;
    const code = typeof failureData?.code === "string"
      ? failureData.code
      : undefined;
    throw new FrontendCapabilityError(message || "capability_unavailable", code);
  }
  return envelope.data;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export type ProfileScreenResolution =
  | { kind: "route"; route: string }
  | { kind: "redirect"; destination: string }
  | { kind: "reject"; reason: string };

/** Resolve identity-bearing screen navigation against one captured Host catalog. */
export function resolveProfileScreenRequest(
  pathname: string,
  catalog: FrontendCatalog,
): ProfileScreenResolution {
  const requested = parseProfileScreenPath(pathname);
  if (!requested) {
    return {
      kind: "reject",
      reason: "The screen URL does not contain a valid Runtime Profile identity.",
    };
  }
  if (requested.profileId !== catalog.profile_id) {
    return {
      kind: "reject",
      reason: "The screen URL does not match the active captured Profile.",
    };
  }
  if (requested.applicationRoute === null) {
    const selectedEntries = contributionsForRoute(
      catalog,
      catalog.selected_entry_route,
      catalog.plan_hash,
    );
    if (selectedEntries.length !== 1) {
      return {
        kind: "reject",
        reason: "The active Profile's selected Application entry is unavailable.",
      };
    }
    try {
      return {
        kind: "redirect",
        destination: profileScreenPath(
          catalog.profile_id,
          catalog.selected_entry_route,
        ),
      };
    } catch {
      return {
        kind: "reject",
        reason: "The active Profile's selected Application entry is unavailable.",
      };
    }
  }
  const hasRoute = contributionsForRoute(
    catalog,
    requested.applicationRoute,
    catalog.plan_hash,
  ).length > 0;
  return hasRoute
    ? { kind: "route", route: requested.applicationRoute }
    : {
      kind: "reject",
      reason: "This screen is not available in the active Profile. Check the selected Application in Tobkiri Launcher, then retry.",
    };
}

export function HostBootstrap({
  pathname,
}: {
  pathname: string;
}) {
  const [catalog, setCatalog] = useState<FrontendCatalog | null>(null);
  const [failed, setFailed] = useState(false);
  const requested = useMemo(() => parseProfileScreenPath(pathname), [pathname]);
  // Host-scoped mounts such as `/approval` intentionally omit the Runtime
  // Profile prefix; they render their sealed Application screen directly.
  const hostRoute = useMemo(
    () => (requested === null ? applicationPathname(pathname) : null),
    [pathname, requested],
  );

  const refreshCatalog = useCallback(async (): Promise<FrontendCatalog> => {
    const value = await fetchDynamicCatalog();
    setCatalog(value);
    setFailed(false);
    return value;
  }, []);

  const refreshBootstrap = useCallback(async (): Promise<void> => {
    setReadinessError(null);
    setReadiness(null);
    try {
      const snapshot = await fetchUiReadiness();
      setReadiness(snapshot);
      try {
        await refreshCatalog();
      } catch {
        if (snapshot.mode === "profile_reconfirmation_required") {
          setFailed(true);
          return;
        }
        setReadinessError("ui_catalog (CATALOG_UNAVAILABLE_AFTER_READINESS)");
      }
    } catch (error) {
      setReadinessError(
        error instanceof Error && error.message.trim()
          ? error.message
          : "readiness_endpoint (INVALID_READINESS)",
      );
    }
  }, [refreshCatalog]);

  useEffect(() => {
    if (hostRoute !== null) return undefined;
    let active = true;
    void refreshBootstrap().catch(() => {
      if (active) setReadinessError("readiness_endpoint (READINESS_ENDPOINT_UNAVAILABLE)");
    });
    return () => {
      active = false;
    };
  }, [refreshCatalog, hostRoute]);

  const capabilities = useMemo<FrontendCapabilityInvoker>(() => {
    const invoke = async (
      request: CapturedCapabilityInvocation,
    ): Promise<unknown> => {
      try {
        return await invokeCapability(request);
      } catch (error) {
        if (
          error instanceof FrontendCapabilityError
          && (error.code === "STALE_RESOLUTION" || error.code === "STALE_CATALOG")
        ) {
          void refreshCatalog().catch(() => undefined);
        }
        throw error;
      }
    };
    return {
      invokeAction: invoke,
      readDataSource: invoke,
    };
  }, [refreshCatalog]);

  if (readinessError) {
    return (
      <TobkiriLoadingScreen
        error={readinessError}
        onRetry={() => void refreshBootstrap()}
      />
    );
  }
  if (!readiness) return <TobkiriLoadingScreen />;
  const retry = () => {
    void refreshBootstrap().catch(() => undefined);
  };
  if (!requested) {
    if (hostRoute === "/approval") {
      return <AuthorityApprovalWindow />;
    }
    return (
      <HostBootstrapFallback
        onRetry={retry}
        reason="The screen URL does not contain a valid Runtime Profile identity."
        route={pathname}
      />
    );
  }
  if (failed) {
    return (
      <HostBootstrapFallback
        onRetry={retry}
        reason="The selected Application could not be loaded."
        route={pathname}
      />
    );
  }
  if (!catalog) return <TobkiriLoadingScreen />;
  const resolution = resolveProfileScreenRequest(pathname, catalog);
  if (resolution.kind === "reject") {
    return (
      <HostBootstrapFallback
        onRetry={retry}
        reason={resolution.reason}
        route={pathname}
      />
    );
  }
  if (resolution.kind === "redirect") {
    window.location.replace(`${resolution.destination}${window.location.search}${window.location.hash}`);
    return <TobkiriLoadingScreen />;
  }
  const route = resolution.route;
  return (
    <DynamicFrontendHost
      catalog={catalog}
      route={route}
      activePlanHash={catalog.plan_hash}
      capabilities={capabilities}
    />
  );
}

/** A missing, stale or quarantined route never selects another Application. */
export function HostBootstrapFallback({
  route,
  reason,
  onRetry,
}: {
  route: string;
  reason: string;
  onRetry: () => void;
}) {
  return (
    <main data-frontend-unavailable={route} className="min-h-screen bg-[#09090b] p-8 text-zinc-100">
      <h1>This screen is unavailable</h1>
      <ErrorNotice copyLabel="Copy screen availability error" copyText={reason}
        errorIcon="frontend-availability" message={reason} />
      <button type="button" onClick={onRetry}>Retry</button>
    </main>
  );
}
