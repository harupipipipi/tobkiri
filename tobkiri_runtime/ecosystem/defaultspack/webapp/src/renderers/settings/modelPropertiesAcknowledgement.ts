import type { ModelPropertiesRequest } from "../../features/search/modelPropertiesNavigation";
import type { SettingsProfileRecord } from "./settingsProfileModel";

/** Match the complete requested route before acknowledging its properties selection. */
export function matchesModelPropertiesProfile(
  request: ModelPropertiesRequest,
  profile: SettingsProfileRecord,
): boolean {
  const identity = request.identity;
  if (!request.registered || !identity.profileId || profile.id !== identity.profileId
    || profile.providerId !== identity.providerId) return false;
  const modelId = typeof profile.raw.model_id === "string"
    ? profile.raw.model_id : profile.modelId;
  if (modelId !== identity.modelId) return false;
  if (identity.qualifiedModelId && profile.raw.qualified_model_id !== identity.qualifiedModelId) return false;
  if (!identity.connectionId) return true;
  const metadata = profile.raw.metadata as Record<string, unknown> | undefined;
  const availability = profile.raw.availability as Record<string, unknown> | undefined;
  const connection = profile.raw.connection_id ?? profile.raw.provider_instance_id
    ?? profile.raw.provider_connection_id ?? metadata?.connection_id
    ?? metadata?.provider_instance_id
    ?? metadata?.provider_connection_id ?? availability?.provider_instance_id
    ?? availability?.provider_connection_id ?? availability?.connection_id
    ?? profile.raw.provider_id;
  return connection === identity.connectionId;
}
