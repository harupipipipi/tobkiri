export const MODEL_ACCESS_VERSION = "tobkiri.connection-model-access.v1" as const;
export const OPENROUTER_FILTER_REVISION = "openrouter.official-filters.2026-10-07.v1" as const;
export type NativeDiscovery = { output_modalities?: string[]; supported_parameters?: string[]; category?: string };
export type NativeRouting = {
  only?: string[]; ignore?: string[]; order?: string[]; quantizations?: string[];
  require_parameters?: boolean; data_collection?: "allow" | "deny"; zdr?: boolean;
  max_price?: { prompt?: number; completion?: number; request?: number; image?: number };
  sort?: "price" | "latency" | "throughput";
};
export type ModelAccessPolicy = {
  version: typeof MODEL_ACCESS_VERSION;
  mode: "all" | "explicit";
  model_ids: string[];
  native_filters?: { revision: typeof OPENROUTER_FILTER_REVISION; discovery: NativeDiscovery; routing: NativeRouting };
};
const isObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const exactKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).every((key) => keys.includes(key));
const token = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const modalities = ["text", "image", "embeddings", "audio", "video", "rerank", "decisions", "speech", "transcription", "all"];
const categories = ["programming", "roleplay", "marketing", "marketing/seo", "technology", "science", "translation", "legal", "finance", "health", "trivia", "academia"];
const quantizations = ["int4", "int8", "fp4", "mxfp4", "nvfp4", "fp6", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown"];
function list(value: unknown, choices?: string[]): string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 64 || value.some((item) => typeof item !== "string" || !token.test(item) || (choices && !choices.includes(item)))) throw new Error("公式フィルターの選択値が無効です。");
  return [...new Set(value)] as string[];
}
/** Validate drafts and returned policy; unknown native fields never pass through. */
export function normalizeModelAccess(value: unknown, capability: string | null): ModelAccessPolicy {
  if (!isObject(value) || !exactKeys(value, ["version", "mode", "model_ids", "native_filters"]) || value.version !== MODEL_ACCESS_VERSION || (value.mode !== "all" && value.mode !== "explicit")) throw new Error("モデル許可の保存形式が無効です。");
  const ids = value.model_ids ?? [];
  if (!Array.isArray(ids) || ids.length > 4096 || ids.some((id) => typeof id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}$/.test(id)) || (value.mode === "all" && ids.length)) throw new Error("モデルIDまたは許可モードが無効です。");
  const result: ModelAccessPolicy = { version: MODEL_ACCESS_VERSION, mode: value.mode as ModelAccessPolicy["mode"], model_ids: [...new Set(ids)].sort() };
  if (value.native_filters !== undefined && value.native_filters !== null) {
    const native = value.native_filters;
    if (capability !== OPENROUTER_FILTER_REVISION || !isObject(native) || !exactKeys(native, ["revision", "discovery", "routing"]) || native.revision !== capability) throw new Error("この接続の公式フィルターは未確認または古い形式です。");
    const discovery = native.discovery ?? {}, routing = native.routing ?? {};
    if (!isObject(discovery) || !exactKeys(discovery, ["output_modalities", "supported_parameters", "category"]) || !isObject(routing) || !exactKeys(routing, ["only", "ignore", "order", "quantizations", "require_parameters", "data_collection", "zdr", "max_price", "sort"])) throw new Error("未対応の公式フィルターが含まれています。");
    const d: NativeDiscovery = {}, r: NativeRouting = {};
    if (discovery.category !== undefined) {
      if (typeof discovery.category !== "string" || !categories.includes(discovery.category)) throw new Error("カテゴリが無効です。");
      d.category = discovery.category;
    }
    if (discovery.output_modalities !== undefined) {
      d.output_modalities = list(discovery.output_modalities, modalities);
      if (d.output_modalities.includes("all") && d.output_modalities.length !== 1) throw new Error("全形式と個別形式は同時に選択できません。");
    }
    if (discovery.supported_parameters !== undefined) d.supported_parameters = list(discovery.supported_parameters);
    for (const key of ["only", "ignore", "order", "quantizations"] as const) if (routing[key] !== undefined) r[key] = list(routing[key], key === "quantizations" ? quantizations : undefined);
    for (const key of ["require_parameters", "zdr"] as const) if (routing[key] !== undefined) {
      if (typeof routing[key] !== "boolean") throw new Error("公式フィルターの値が無効です。");
      r[key] = routing[key];
    }
    if (routing.data_collection !== undefined) {
      if ((typeof routing.data_collection !== "string" || !["allow", "deny"].includes(routing.data_collection))) throw new Error("データ収集条件が無効です。");
      r.data_collection = routing.data_collection as "allow" | "deny";
    }
    if (routing.sort !== undefined) {
      if ((typeof routing.sort !== "string" || !["price", "latency", "throughput"].includes(routing.sort))) throw new Error("経路の優先条件が無効です。");
      r.sort = routing.sort as NativeRouting["sort"];
    }
    if (routing.max_price !== undefined) {
      const prices = routing.max_price;
      if (!isObject(prices) || !Object.keys(prices).length || !exactKeys(prices, ["prompt", "completion", "request", "image"]) || Object.values(prices).some((price) => typeof price !== "number" || !Number.isFinite(price) || price < 0)) throw new Error("価格上限には0以上の有限USD値を指定してください。");
      r.max_price = { ...prices };
    }
    if (r.only?.some((id) => r.ignore?.includes(id))) throw new Error("同じ実行事業者を許可と除外に指定できません。");
    result.native_filters = { revision: OPENROUTER_FILTER_REVISION, discovery: d, routing: r };
  }
  return result;
}
/** Preserve every explicit ID even when a page changes or a model disappears. */
export function toggleAllowedModel(policy: ModelAccessPolicy, id: string): ModelAccessPolicy {
  const ids = new Set(policy.model_ids);
  if (ids.has(id)) ids.delete(id); else ids.add(id);
  return { ...policy, mode: "explicit", model_ids: [...ids].sort() };
}
export function modelAllowed(policy: ModelAccessPolicy | null, id: string): boolean {
  return policy === null || policy.mode === "all" || policy.model_ids.includes(id);
}
