import { normalizeModelAccess, OPENROUTER_FILTER_REVISION, type ModelAccessPolicy, type NativeDiscovery } from "../modelAccessPolicy";

export type ModelAccessScope = { profile_id: string; provider_instance_id: string };
export type ModelAccessSnapshot = ModelAccessScope & {
  registry_revision: number; model_access: ModelAccessPolicy | null;
  native_capability: typeof OPENROUTER_FILTER_REVISION | null;
};
export type ModelAccessCatalog = ModelAccessScope & { models: { model_id: string; display_name: string }[]; status: "live" | "unavailable" | "unsupported" };
export type ProviderModelAccessPorts = {
  getModelAccess(scope: ModelAccessScope): Promise<unknown>;
  setModelAccess(input: ModelAccessScope & { expected_revision: number; model_access: ModelAccessPolicy }): Promise<unknown>;
  getModelAccessCatalog(input: ModelAccessScope & { discovery_filters?: NativeDiscovery }): Promise<unknown>;
};
function snapshot(raw: unknown, scope: ModelAccessScope): ModelAccessSnapshot {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) throw new Error("モデル許可設定を確認できません。");
  const value = raw as Record<string, unknown>;
  if (Object.keys(value).some((key) => !["profile_id", "provider_instance_id", "registry_revision", "model_access", "native_capability"].includes(key)) || value.profile_id !== scope.profile_id || value.provider_instance_id !== scope.provider_instance_id || !Number.isSafeInteger(value.registry_revision) || Number(value.registry_revision) < 0 || ![null, OPENROUTER_FILTER_REVISION].includes(value.native_capability as null)) throw new Error("モデル許可の接続または形式を確認できません。");
  return { ...scope, registry_revision: value.registry_revision as number, native_capability: value.native_capability as ModelAccessSnapshot["native_capability"], model_access: value.model_access === null ? null : normalizeModelAccess(value.model_access, value.native_capability as string | null) };
}
/** Use finite authenticated API ports; no API key material enters this resource. */
export function createProviderModelAccessResources(ports: ProviderModelAccessPorts) {
  return {
    async load(scope: ModelAccessScope) { return snapshot(await ports.getModelAccess({ ...scope }), scope); },
    async save(current: ModelAccessSnapshot, draft: ModelAccessPolicy) {
      const scope = { profile_id: current.profile_id, provider_instance_id: current.provider_instance_id };
      const policy = normalizeModelAccess(draft, current.native_capability);
      const result = snapshot(await ports.setModelAccess({ ...scope, expected_revision: current.registry_revision, model_access: policy }), scope);
      if (result.registry_revision <= current.registry_revision || JSON.stringify(result.model_access) !== JSON.stringify(policy)) throw new Error("保存結果が要求したモデル許可と一致しません。");
      return result;
    },
    async catalog(scope: ModelAccessScope, discovery?: NativeDiscovery) {
      const raw = await ports.getModelAccessCatalog({ ...scope, ...(discovery ? { discovery_filters: discovery } : {}) });
      const invalid = () => new Error("モデル一覧を確認できません。");
      const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
      const exact = (value: Record<string, unknown>, fields: string[]) => Object.keys(value).length === fields.length && fields.every((field) => Object.prototype.hasOwnProperty.call(value, field));
      const nonempty = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
      if (!record(raw) || !exact(raw, ["profile_id", "provider_instance_id", "models", "status"])
        || !nonempty(raw.profile_id) || !nonempty(raw.provider_instance_id)
        || raw.profile_id !== scope.profile_id || raw.provider_instance_id !== scope.provider_instance_id
        || !["live", "unavailable", "unsupported"].includes(String(raw.status))
        || typeof raw.status !== "string" || !Array.isArray(raw.models)) throw invalid();
      const models = raw.models.map((model) => {
        if (!record(model) || !exact(model, ["model_id", "display_name"])
          || !nonempty(model.model_id) || !nonempty(model.display_name)) throw invalid();
        return { model_id: model.model_id, display_name: model.display_name };
      });
      return { ...scope, models, status: raw.status as ModelAccessCatalog["status"] };
    },
  };
}
export type ProviderModelAccessResources = ReturnType<typeof createProviderModelAccessResources>;
