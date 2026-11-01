import type { ModelSearchItem } from "../../lib/api";

export type ModelPropertiesRequest = {
  requestId: number;
  intent: "properties";
  scopeId?: string;
  model: ModelSearchItem;
  identity: { profileId?: string; providerId: string; modelId: string; qualifiedModelId?: string; connectionId?: string };
  registered: boolean;
};
let pendingRequest: ModelPropertiesRequest | null = null;
let sequence = 0;
const listeners = new Set<() => void>();

/** Queue an in-memory properties request, without switching or registering a model. */
export function requestModelProperties(model: ModelSearchItem, registered: boolean, connectionId?: string, scopeId?: string): ModelPropertiesRequest | null {
  if (!model.provider_id || !model.model_id) return null;
  if (connectionId && model.connection_id && connectionId !== model.connection_id) return null;
  const boundConnectionId = connectionId || model.connection_id;
  // Construct the request from explicit metadata rather than copying unknown API fields.
  pendingRequest = {
    requestId: ++sequence, intent: "properties", registered, scopeId,
    identity: { profileId: registered ? model.profile_id : undefined,
      providerId: model.provider_id, modelId: model.model_id,
      qualifiedModelId: model.qualified_model_id, connectionId: boundConnectionId },
    model: {
      profile_id: model.profile_id, display_name: model.display_name,
      provider_id: model.provider_id, provider_display_name: model.provider_display_name,
      model_id: model.model_id, qualified_model_id: model.qualified_model_id,
      configured: model.configured, route_configured: model.route_configured, label: model.label,
      connection_id: boundConnectionId, provenance: model.provenance, reachability: model.reachability,
      supports_vision: model.supports_vision, supports_image_input: model.supports_image_input,
      supports_tool_calling: model.supports_tool_calling, supports_thinking: model.supports_thinking,
      supports_fast: model.supports_fast, thinking_levels: model.thinking_levels,
      default_thinking_level: model.default_thinking_level,
      capability_tags: model.capability_tags, speed_tier: model.speed_tier,
      quality_tier: model.quality_tier, cost_tier: model.cost_tier,
    },
  };
  for (const listener of listeners) listener();
  return pendingRequest;
}

/** Read pending navigation even when Settings mounted after the global selection. */
export function getModelPropertiesRequest(scopeId?: string): ModelPropertiesRequest | null {
  return scopeId !== undefined && pendingRequest?.scopeId !== scopeId ? null : pendingRequest;
}

/** Discard navigation captured before a Profile, activation or authority change. */
export function invalidateModelPropertiesScope(scopeId: string): void {
  if (!pendingRequest || pendingRequest.scopeId === scopeId) return;
  clearModelPropertiesRequest(pendingRequest.requestId);
}

/** Subscribe without writing model identity into URLs, storage or logs. */
export function subscribeModelPropertiesRequest(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** Consume only the request the adapter actually handled; never clear a newer request. */
export function clearModelPropertiesRequest(requestId: number): void {
  if (pendingRequest?.requestId !== requestId) return;
  pendingRequest = null;
  for (const listener of listeners) listener();
}
