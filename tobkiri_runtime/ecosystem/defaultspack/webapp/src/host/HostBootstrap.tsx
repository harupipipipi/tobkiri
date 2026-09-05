import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { TobkiriLoadingScreen } from "../components/TobkiriLoadingScreen";
import { defaultspackApiFetch, defaultspackContractRoute } from "../lib/api";
import { ConversationV4Unavailable } from "./ConversationV4View";
import {
  DynamicFrontendHost,
  contributionsForRoute,
} from "./DynamicFrontendHost";
import type {
  CapturedCapabilityInvocation,
  FrontendCapabilityInvoker,
  FrontendCatalog,
} from "./frontendContracts";

type ApiEnvelope<T> = {
  success: boolean;
  data?: T;
  error?: string | null;
};

type UiCatalogEnvelope = {
  dynamic_host?: FrontendCatalog | null;
};

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

export function HostBootstrap({
  route,
  fallback,
}: {
  route: string;
  fallback: ReactNode;
}) {
  const [catalog, setCatalog] = useState<FrontendCatalog | null>(null);
  const [failed, setFailed] = useState(false);

  const refreshCatalog = useCallback(async (): Promise<FrontendCatalog> => {
    const value = await fetchDynamicCatalog();
    setCatalog(value);
    setFailed(false);
    return value;
  }, []);

  useEffect(() => {
    let active = true;
    void refreshCatalog().catch(
      () => {
        if (active) setFailed(true);
      },
    );
    return () => {
      active = false;
    };
  }, [refreshCatalog]);

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

  const retry = () => {
    void refreshCatalog().catch(() => undefined);
  };
  if (failed) {
    return (
      <HostBootstrapFallback
        fallback={fallback}
        onRetry={retry}
        reason="The active Pack v4 conversation could not be loaded."
        route={route}
      />
    );
  }
  if (!catalog) return <TobkiriLoadingScreen />;
  const hasRoute = contributionsForRoute(
    catalog,
    route,
    catalog.plan_hash,
  ).length > 0;
  if (!hasRoute) {
    return (
      <HostBootstrapFallback
        fallback={fallback}
        onRetry={retry}
        reason="The active profile does not provide a Pack v4 conversation."
        route={route}
      />
    );
  }
  return (
    <DynamicFrontendHost
      catalog={catalog}
      route={route}
      activePlanHash={catalog.plan_hash}
      capabilities={capabilities}
    />
  );
}

/** Keep legacy compatibility outside the Pack v4 conversation entry point. */
export function HostBootstrapFallback({
  route,
  reason,
  onRetry,
  fallback,
}: {
  route: string;
  reason: string;
  onRetry: () => void;
  fallback: ReactNode;
}) {
  if (route === "/chat" || route === "/chat/") {
    return <ConversationV4Unavailable reason={reason} onRetry={onRetry} />;
  }
  return <>{fallback}</>;
}
