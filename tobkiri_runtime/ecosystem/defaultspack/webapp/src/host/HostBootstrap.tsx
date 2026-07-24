import { useEffect, useMemo, useState, type ReactNode } from "react";

import { defaultspackApiFetch } from "../lib/api";
import { DynamicFrontendHost } from "./DynamicFrontendHost";
import type {
  CapabilityInvocation,
  FrontendCapabilityClient,
  FrontendCatalog,
} from "./frontendContracts";

type ApiEnvelope<T> = {
  status: "ok" | "error";
  data?: T;
  error?: { message?: string; code?: string } | string;
};

type UiCatalogEnvelope = {
  dynamic_host?: FrontendCatalog | null;
};

async function fetchDynamicCatalog(): Promise<FrontendCatalog> {
  const response = await defaultspackApiFetch("/api/ui/catalog", {
    cache: "no-store",
  });
  const envelope = await response.json() as ApiEnvelope<UiCatalogEnvelope>;
  const catalog = envelope.data?.dynamic_host;
  if (!response.ok || envelope.status !== "ok" || !catalog) {
    throw new Error("dynamic_frontend_catalog_unavailable");
  }
  return catalog;
}

async function invokeCapability(
  profileId: string,
  request: CapabilityInvocation,
): Promise<unknown> {
  const response = await defaultspackApiFetch("/api/ui/capability/invoke", {
    method: "POST",
    cache: "no-store",
    body: JSON.stringify({
      request_id: crypto.randomUUID(),
      expires_at: Date.now() / 1000 + 30,
      profile_id: profileId,
      plan_hash: request.planHash,
      contribution_id: request.contributionId,
      owner_pack_id: request.ownerPackId,
      contract_id: request.contractId,
      payload: request.payload,
    }),
  });
  const envelope = await response.json() as ApiEnvelope<unknown>;
  if (!response.ok || envelope.status !== "ok") {
    const message = typeof envelope.error === "string"
      ? envelope.error
      : envelope.error?.message;
    throw new Error(message || "capability_unavailable");
  }
  return envelope.data;
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

  useEffect(() => {
    let active = true;
    void fetchDynamicCatalog().then(
      (value) => {
        if (active) setCatalog(value);
      },
      () => {
        if (active) setFailed(true);
      },
    );
    return () => {
      active = false;
    };
  }, []);

  const capabilities = useMemo<FrontendCapabilityClient | null>(() => {
    if (!catalog) return null;
    return {
      invokeAction: (request) => invokeCapability(catalog.profile_id, request),
      readDataSource: (request) => invokeCapability(catalog.profile_id, request),
    };
  }, [catalog]);

  if (failed) return <>{fallback}</>;
  if (!catalog || !capabilities) return <main role="status">Loading selected interface…</main>;
  const hasRoute = catalog.contributions.some(
    (item) => item.kind === "route" && item.route === route,
  );
  if (!hasRoute) return <>{fallback}</>;
  return (
    <DynamicFrontendHost
      catalog={catalog}
      route={route}
      activePlanHash={catalog.plan_hash}
      capabilities={capabilities}
    />
  );
}

