import type { ModelProfile, ModelSearchItem } from "../../lib/api";
import type { ModelSelectOption } from "./modelSelect";
import { isModelPinIdentity } from "./modelPins";
import { modelProfileConnectionId } from "./modelSelectionIdentity";

function identifier(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function modelPinIdentity(
  modelId: unknown,
  connectionId: unknown,
  providerId: unknown,
): string | null {
  const model = identifier(modelId);
  if (!model) return null;
  const connection = identifier(connectionId);
  const provider = identifier(providerId);
  const identity = connection ? JSON.stringify(["connection", connection, model])
    : provider ? JSON.stringify(["catalog", provider, model]) : null;
  return isModelPinIdentity(identity) ? identity : null;
}

/** Pin the model on a saved route without changing its selection Profile ID. */
export function modelPinIdentityForProfile(profile: ModelProfile): string | null {
  return modelPinIdentity(profile.model_id, modelProfileConnectionId(profile), null);
}

/** Join bound catalog and saved options; unbound catalog pins remain separate. */
export function modelPinIdentityForOption(option: ModelSelectOption): string | null {
  // An incomplete saved-route adapter cannot become a public catalog identity.
  if (option.registered_profile_id && !identifier(option.connection_id)) return null;
  return modelPinIdentity(
    option.model_id,
    option.connection_id,
    option.catalog_provider_id ?? option.provider_id ?? option.provider,
  );
}

/** Join an omitted connection only to an exact, already loaded saved route. */
export function modelPinIdentityForSearchItem(
  item: ModelSearchItem,
  savedProfiles: readonly ModelProfile[] = [],
  savedProfilesReady = true,
): string | null {
  if (!identifier(item.connection_id) && !savedProfilesReady) return null;
  if (!identifier(item.connection_id) && identifier(item.profile_id)
    && identifier(item.provider_id) && identifier(item.model_id)) {
    const connections = new Set(savedProfiles
      .filter((profile) => profile.profile_id === item.profile_id
        && profile.provider_id === item.provider_id && profile.model_id === item.model_id)
      .map(modelProfileConnectionId).filter(Boolean));
    if (connections.size > 1) return null;
    const connection = connections.values().next().value;
    if (connection) return modelPinIdentity(item.model_id, connection, null);
  }
  return modelPinIdentity(item.model_id, item.connection_id, item.provider_id);
}
