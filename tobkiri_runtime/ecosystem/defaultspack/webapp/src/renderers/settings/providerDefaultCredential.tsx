import type { ReactElement } from "react";

import { cn } from "../../lib/cn";

export type ProviderCredentialRow = Record<string, unknown>;

const sourceLabel = (source: unknown): string => {
  switch (String(source ?? "").trim()) {
    case "environment":
    case "env":
      return "environment";
    case "secret_store":
      return "local secret store";
    case "oauth":
      return "OAuth";
    case "device_transfer":
      return "device transfer";
    case "opaque_handle":
      return "Host broker";
    default:
      return "provider default";
  }
};

const usabilityLabel = (status: unknown): string => {
  switch (String(status ?? "").trim()) {
    case "verified_usable":
      return "verified usable";
    case "verified_limited":
      return "verified limited";
    case "invalid":
      return "invalid";
    case "unavailable":
      return "provider unavailable";
    case "unknown_stale":
      return "last check stale";
    case "present_unverified":
    default:
      return "present, unverified";
  }
};

export function providerDefaultApiRows(providers: ProviderCredentialRow[]): ProviderCredentialRow[] {
  return providers
    .filter((provider) => Boolean(provider.default_api_key_configured))
    .map((provider) => {
      const providerId = String(provider.provider_id ?? "").trim();
      return {
        provider_id: providerId,
        api_id: "provider-default",
        name: "Provider default credential",
        key: `${providerId}:provider-default`,
        label: `${providerId}:provider-default:***`,
        configured: true,
        default_api_key: true,
        read_only: true,
        kind: provider.kind,
        credential_presence: provider.credential_presence ?? "present",
        credential_source: provider.credential_source ?? "provider_default",
        credential_usability: provider.credential_usability ?? "present_unverified",
        credential_health: provider.credential_health,
      };
    })
    .filter((api) => Boolean(api.provider_id));
}

export function ProviderCredentialBadges({ api }: { api: ProviderCredentialRow }): ReactElement {
  const status = String(api.credential_usability ?? "present_unverified");
  const health = api.credential_health && typeof api.credential_health === "object"
    ? api.credential_health as ProviderCredentialRow
    : {};
  const freshness = String(health.freshness ?? "unknown");
  const reasonCode = String(health.safe_reason_code ?? "unknown");
  const usable = status === "verified_usable";
  const warning = status === "present_unverified" || status === "unknown_stale" || status === "verified_limited";
  return (
    <>
      <span
        className="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] text-sky-200"
        data-credential-source={String(api.credential_source ?? "provider_default")}
      >
        {sourceLabel(api.credential_source)}
      </span>
      <span
        className={cn(
          "rounded-full border px-2 py-0.5 text-[10px]",
          usable
            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
            : warning
              ? "border-amber-500/30 bg-amber-500/10 text-amber-100"
              : "border-rose-500/30 bg-rose-500/10 text-rose-100",
        )}
        data-credential-presence="present"
        data-credential-usability={status}
      >
        {usabilityLabel(status)}
      </span>
      <span className="rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] text-zinc-400">
        read only
      </span>
      <span
        className="rounded-full border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-[10px] text-zinc-400"
        data-credential-freshness={freshness}
        data-credential-reason={reasonCode}
      >
        health {freshness.replace("_", " ")}
      </span>
    </>
  );
}

export function ProviderDefaultCredentialNotice({ api }: { api: ProviderCredentialRow }): ReactElement {
  return (
    <div
      className="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-3 text-sm text-sky-100"
      data-provider-default-credential
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Provider default credential</span>
        <span className="font-mono text-xs text-sky-200/80">
          {String(api.provider_id ?? "provider")}:provider-default:***
        </span>
        <ProviderCredentialBadges api={api} />
      </div>
      <p className="mt-2 text-xs leading-5 text-sky-100/75">
        The Host has a default credential for this provider. Presence does not prove usability;
        the provider adapter validates current scope, account, quota, region, and health at invocation.
        Save a named key to create selectable credential variants.
      </p>
    </div>
  );
}
