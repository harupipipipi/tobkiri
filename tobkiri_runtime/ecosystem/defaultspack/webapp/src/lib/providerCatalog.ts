import providerModelCatalog from "./providerModelCatalog.generated.json";
import type { RegisteredProviderConnection } from "./api";

export type ProviderCatalogModel = Readonly<{
  model_id: string;
  display_name: string;
  type: string;
}>;

const providers = providerModelCatalog.providers as Readonly<Record<string, {
  models: readonly ProviderCatalogModel[];
}>>;

/** Advisory choices only; account access and tool evidence remain Host-owned. */
export function providerCatalogModels(providerId: string): readonly ProviderCatalogModel[] {
  return providers[providerId]?.models ?? [];
}

/** Preserve the exact saved connection; use catalog metadata only for choices. */
export function connectionCatalogProviderId(connection: RegisteredProviderConnection): string {
  if (connection.catalog_provider_id) return connection.catalog_provider_id;
  // Compatibility for connections saved before discovery metadata was added.
  // This identifies a UI catalog, never an execution or credential identity.
  return Object.keys(providers).find((id) =>
    connection.provider_instance_id === `provider.${id}`
    || connection.provider_instance_id.startsWith(`provider.${id}.`),
  ) ?? "";
}

/** Make stable opaque IDs while keeping a Unicode connection name for display. */
export async function providerConnectionName(providerId: string, apiName: string): Promise<string> {
  const simple = `${providerId}.${apiName}`;
  if (/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(simple)) return simple;
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(apiName));
  const suffix = Array.from(new Uint8Array(bytes).slice(0, 12), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `${providerId}.key-${suffix}`;
}

/** Derive a route ID from the exact connection and selected catalog model. */
export async function catalogModelProfileId(connectionId: string, modelId: string): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(JSON.stringify([connectionId, modelId])),
  );
  return "model." + Array.from(new Uint8Array(bytes), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
