import type { RegisteredProviderConnection } from "../../lib/api";
import { connectionCatalogProviderId } from "../../lib/providerCatalog";

/** Label a connection without changing its saved execution identity. */
export function modelRouteConnectionLabel(
  connection: RegisteredProviderConnection,
  connections: readonly RegisteredProviderConnection[] = [],
): string {
  const provider = connectionCatalogProviderId(connection);
  const name = connection.display_name.trim() || connection.provider_instance_id;
  const normalizedName = name.toLowerCase();
  const normalizedProvider = provider.toLowerCase();
  const alreadyNamed = normalizedProvider && (
    normalizedName === normalizedProvider
    || ["-", " ", "/", ".", ":", "_", "（", "("].some((separator) => (
      normalizedName.startsWith(`${normalizedProvider}${separator}`)
    ))
  );
  const label = provider && !alreadyNamed ? `${provider}-${name}` : name;
  const duplicate = connections.some((other) => (
    other.provider_instance_id !== connection.provider_instance_id
    && modelRouteConnectionLabel(other).toLowerCase() === label.toLowerCase()
  ));
  return duplicate ? `${label} (${connection.provider_instance_id})` : label;
}
