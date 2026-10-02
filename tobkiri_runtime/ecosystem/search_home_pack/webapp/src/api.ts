import { defaultspackApiHeaders } from "../../../defaultspack/webapp/src/lib/apiAuth";
import { evaluateDestination, evaluateExplicitDestinationInput } from "./destinationPolicy";
import type { RouteDecision } from "./routerTypes";

export const MODEL_SETTINGS_KEY = "preferred" + "_model";
// Search is an alternate surface of the captured Defaults application.
export const SEARCH_HOME_CONTRACT_ENDPOINT = "/api/contracts/defaultspack/";

export type SearchHomeContractRoute = {
  readonly kind: "search-home-contract-route";
  readonly apiPath: string;
};

export function searchHomeContractRoute(apiPath: string): SearchHomeContractRoute {
  const normalized = apiPath.startsWith("/") ? apiPath : `/${apiPath}`;
  const segments = normalized.split("/");
  if (
    segments[1] !== "api"
    || normalized.startsWith("/api/contracts/")
    || normalized.includes("//")
    || segments.some((segment) => segment === "." || segment === "..")
  ) {
    throw new Error("invalid search home contract route");
  }
  return { kind: "search-home-contract-route", apiPath: normalized };
}

export function searchHomeContractUrl(
  route: SearchHomeContractRoute,
  method = "GET",
): string {
  return `${SEARCH_HOME_CONTRACT_ENDPOINT}${encodeURIComponent(`${method.toUpperCase()} ${route.apiPath}`)}`;
}

export type SearchHomeModel = {
  profile_id: string;
  qualified_model_id?: string;
  label?: string;
  display_name?: string;
  provider_display_name?: string;
  provider_id?: string;
  model_id?: string;
  configured?: boolean;
  local?: boolean;
  requires_api_key?: boolean;
  supports_tool_calling?: boolean;
  supports_image_input?: boolean;
  supports_vision?: boolean;
  supports_thinking?: boolean;
  supports_fast?: boolean;
  speed_tier?: string;
  quality_tier?: string;
  knowledge_level?: number;
  availability?: {
    status?: string;
    configured?: boolean;
    active?: boolean;
    available?: boolean;
    [key: string]: unknown;
  };
  metadata?: Record<string, unknown>;
};

export type ModelsResponse = {
  models: SearchHomeModel[];
  filters_applied?: Record<string, unknown>;
};

export type ModelSettingsResponse = {
  models?: Record<string, unknown>;
};

export type SearchAnswerResponse = {
  status: "ok" | "error";
  answer?: string;
  model?: string;
  conversation_id?: string;
  used_tools?: string[];
  used_defaultspack_node?: boolean;
  defaultspack_node?: string;
  tool_calling_unavailable_reason?: string;
  error?: {
    code?: string;
    message?: string;
  };
};

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function errorMessage(value: unknown, fallback: string): string {
  const record = objectRecord(value);
  if (typeof value === "string" && value.trim()) return value;
  return typeof record?.message === "string" && record.message.trim()
    ? record.message
    : fallback;
}

async function requestJson(
  route: SearchHomeContractRoute,
  init?: RequestInit,
  preserveAnswerError = false,
): Promise<unknown> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = defaultspackApiHeaders(method, init?.headers);
  headers.set("X-Tobkiri-Request-ID", crypto.randomUUID());
  const response = await fetch(searchHomeContractUrl(route, method), {
    ...init,
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`Tobkiri Defaults returned an invalid response (${response.status}).`);
  }
  const host = objectRecord(payload);
  if (host && typeof host.success === "boolean") {
    if (!host.success || host.error != null) {
      throw new Error(errorMessage(host.error, `Request failed (${response.status})`));
    }
    if (!("data" in host)) throw new Error("Tobkiri Defaults returned no result data.");
    payload = host.data;
  }
  if (!host || typeof host.success !== "boolean") {
    // The Search surface must not accept an unrelated raw localhost service.
    throw new Error("Tobkiri Defaults returned no authenticated Host data envelope.");
  }
  // A Pack may retain its status envelope inside the authenticated Host data.
  const pack = objectRecord(payload);
  if (pack?.status === "ok" && "data" in pack) payload = pack.data;
  const result = objectRecord(payload);
  if (preserveAnswerError && result?.status === "error") return result;
  if (!response.ok || result?.status === "error" || result?.state === "error") {
    throw new Error(errorMessage(result?.error ?? result, `Request failed (${response.status})`));
  }
  return payload;
}

function checkedInput(input: string): string {
  if (!input.trim() || new TextEncoder().encode(input).length > 60 * 1024) {
    throw new Error("検索内容を入力してください。入力は60 KiBまでです。");
  }
  return input;
}

/** Route locally without importing a legacy Pack function or probing websites. */
export async function routeInput(
  input: string,
  _model = "",
  intent: "smart" | "open" = "smart",
): Promise<RouteDecision> {
  const query = checkedInput(input).trim();
  const shortcut = /^(?:!g\s+|google:)/i.test(query);
  const searchQuery = shortcut ? query.replace(/^(?:!g\s+|google:)\s*/i, "") : query;
  const fallbackUrl = `https://www.google.com/search?q=${encodeURIComponent(searchQuery)}`;
  const base: RouteDecision = {
    query: searchQuery,
    target_url: "",
    target_candidates: [],
    selected_index: -1,
    fallback_url: fallbackUrl,
    used_ai_judge: false,
    used_visual_judge: false,
    metadata: {},
  };
  const explicit = evaluateExplicitDestinationInput(query);
  const bareDomain = !explicit && /^[a-z0-9][a-z0-9.-]*\.[a-z]{2,63}(?::\d{1,5})?(?:[/?#]\S*)?$/i.test(query);
  const destination = explicit ?? (bareDomain ? evaluateDestination(`https://${query}`) : null);
  if (destination?.verdict === "block") {
    return { ...base, route_type: "BLOCKED_DESTINATION_INPUT", resolution_reason: `input_policy:${destination.reason}` };
  }
  if (destination) {
    return {
      ...base,
      route_type: "URL_NAVIGATE",
      target_url: destination.normalized_url,
      target_candidates: [{ url: destination.normalized_url, title: destination.display_host, source: "direct_url" }],
      selected_index: 0,
      resolution_reason: "入力したURLを確認して開きます。",
    };
  }
  if (shortcut || intent === "open") {
    return {
      ...base,
      route_type: "GOOGLE_REDIRECT",
      target_url: fallbackUrl,
      target_candidates: [{ url: fallbackUrl, title: "Googleで検索", source: "google_search" }],
      selected_index: 0,
      resolution_reason: "Googleの検索結果を確認して開きます。",
    };
  }
  return { ...base, route_type: "ASK_AI", resolution_reason: "選択したDefaultsモデルに質問します。" };
}

export async function answerInput(input: string, model = ""): Promise<SearchAnswerResponse> {
  const query = checkedInput(input);
  const selectedModel = model.trim() || String((await loadModelSettings()).models?.[MODEL_SETTINGS_KEY] ?? "").trim();
  if (!selectedModel || selectedModel.length > 256) {
    throw new Error("Tobkiri Defaultsで利用するモデルを選択してください。");
  }
  const value = await requestJson(searchHomeContractRoute("api/search/answer"), {
    method: "POST",
    body: JSON.stringify({ input: query, model: selectedModel }),
  }, true);
  const result = objectRecord(value);
  if (!result || !["ok", "error"].includes(String(result.status))
    || (result.answer !== undefined && typeof result.answer !== "string")
    || (result.model !== undefined && typeof result.model !== "string")) {
    throw new Error("Tobkiri Defaults returned an invalid answer payload.");
  }
  return result as SearchAnswerResponse;
}

export async function loadModels(): Promise<ModelsResponse> {
  const value = await requestJson(searchHomeContractRoute("api/ai/models/search"), {
    method: "POST",
    body: JSON.stringify({ max_results: 100, offset: 0 }),
  });
  const result = objectRecord(value);
  if (!result || !Array.isArray(result.models)
    || result.models.some((item) => !objectRecord(item) || typeof item.profile_id !== "string")) {
    throw new Error("Tobkiri Defaults returned an invalid model catalog.");
  }
  return result as ModelsResponse;
}

type ModelState = { namespace: string; revision: number; values: Record<string, unknown> };

async function loadModelState(): Promise<ModelState> {
  const value = await requestJson(searchHomeContractRoute("api/ui/model-state"));
  const result = objectRecord(value);
  if (!result || typeof result.namespace !== "string" || !result.namespace
    || !Number.isSafeInteger(result.revision) || (result.revision as number) < 0
    || !objectRecord(result.values)) {
    throw new Error("Tobkiri Defaults returned an invalid model settings revision.");
  }
  return result as ModelState;
}

export async function loadModelSettings(): Promise<ModelSettingsResponse> {
  return { models: (await loadModelState()).values };
}

export async function setPreferredModel(model: string): Promise<void> {
  // "Defaultsの既定モデル" follows the existing owner value without replacing it.
  const selectedModel = model.trim();
  if (!selectedModel) {
    await loadModelState();
    return;
  }
  if (selectedModel.length > 256) throw new Error("選択したモデルIDが長すぎます。");
  const snapshot = await loadModelState();
  if (snapshot.values[MODEL_SETTINGS_KEY] === selectedModel) return;
  const mutationId = crypto.randomUUID();
  const value = await requestJson(searchHomeContractRoute("api/ui/model-state"), {
    method: "PUT",
    body: JSON.stringify({
      kind: MODEL_SETTINGS_KEY,
      value: selectedModel,
      expected_revision: snapshot.revision,
      mutation_id: mutationId,
    }),
  });
  const result = objectRecord(value);
  if (!result || result.kind !== MODEL_SETTINGS_KEY || result.value !== selectedModel
    || result.namespace !== snapshot.namespace || result.mutation_id !== mutationId
    || result.revision !== snapshot.revision + 1
    || typeof result.receipt !== "string" || !/^sha256:[0-9a-f]{64}$/.test(result.receipt)) {
    throw new Error("モデル選択の保存結果を確認できません。再送せず共有設定を確認してください。");
  }
}
