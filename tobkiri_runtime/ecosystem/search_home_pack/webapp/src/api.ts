import type { RouteDecision, RouteSessionState } from "./routerTypes";

export const MODEL_SETTINGS_KEY = "preferred" + "_model";
export const SEARCH_HOME_CONTRACT_ENDPOINT = "/api/contracts/search_home_pack/";

export type SearchHomeContractRoute = {
  readonly kind: "search-home-contract-route";
  readonly apiPath: string;
};

export function searchHomeContractRoute(apiPath: string): SearchHomeContractRoute {
  const normalized = apiPath.startsWith("/") ? apiPath : `/${apiPath}`;
  const segments = normalized.split("/");
  if (
    segments[1] !== "api"
    || normalized.startsWith(SEARCH_HOME_CONTRACT_ENDPOINT)
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

async function requestJson<T>(route: SearchHomeContractRoute, init?: RequestInit): Promise<T> {
  const path = route.apiPath;
  const method = (init?.method ?? "GET").toUpperCase();
  const response = await fetch(searchHomeContractUrl(route, method), {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    method,
    ...init,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = (await response.json()) as { error?: { message?: string } };
      if (payload?.error?.message) {
        message = payload.error.message;
      }
    } catch {
      // Ignore malformed error payloads and surface the HTTP status instead.
    }
    throw new Error(message);
  }
  return validateResponse(path, await parseResponseJson(response, path)) as T;
}

export async function routeInput(input: string, model = ""): Promise<RouteDecision> {
  return requestJson<RouteDecision>(searchHomeContractRoute("api/route"), {
    method: "POST",
    body: JSON.stringify({ input, model }),
  });
}

export async function answerInput(input: string, model = ""): Promise<SearchAnswerResponse> {
  return requestJson<SearchAnswerResponse>(searchHomeContractRoute("api/answer"), {
    method: "POST",
    body: JSON.stringify({ input, model, use_search: true }),
  });
}

export async function loadModels(): Promise<ModelsResponse> {
  return requestJson<ModelsResponse>(searchHomeContractRoute("api/models"));
}

export async function loadModelSettings(): Promise<ModelSettingsResponse> {
  return requestJson<ModelSettingsResponse>(searchHomeContractRoute("api/settings"));
}

export async function setPreferredModel(model: string): Promise<void> {
  await requestJson<unknown>(searchHomeContractRoute("api/settings/model"), {
    method: "POST",
    body: JSON.stringify({ model }),
  });
}

export async function loadRouteState(): Promise<Record<string, unknown> | null> {
  try {
    return await requestJson<Record<string, unknown>>(searchHomeContractRoute("api/route-state"));
  } catch {
    return null;
  }
}

export function persistRouteStateRemotely(state: RouteSessionState): void {
  const payload = JSON.stringify(state);
  const route = searchHomeContractRoute("api/route-state");
  if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
    const blob = new Blob([payload], { type: "application/json" });
    if (navigator.sendBeacon(searchHomeContractUrl(route, "POST"), blob)) {
      return;
    }
  }
  void fetch(searchHomeContractUrl(route, "POST"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => undefined);
}

export function clearRouteStateRemotely(): void {
  const issuedAt = new Date();
  const random = globalThis.crypto.getRandomValues(new Uint8Array(16));
  persistRouteStateRemotely({
    query: "",
    target_url: "",
    fallback_url: "",
    selected_index: -1,
    target_candidates: [],
    updated_at: issuedAt.toISOString(),
    state_id: Array.from(random, (byte) => byte.toString(16).padStart(2, "0")).join(""),
    issued_at: issuedAt.toISOString(),
    expires_at: new Date(issuedAt.getTime() + 5 * 60 * 1000).toISOString(),
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function responseLabel(path: string): string {
  if (path.endsWith("/models")) return "Model catalog";
  if (path.endsWith("/settings")) return "Model settings";
  if (path.endsWith("/settings/model")) return "Model preference";
  if (path.endsWith("/route")) return "Routing service";
  if (path.endsWith("/answer")) return "Answer service";
  return "Search Home service";
}

async function parseResponseJson(response: Response, path: string): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new Error(`${responseLabel(path)} returned malformed JSON.`);
  }
}

function validateResponse(path: string, payload: unknown): unknown {
  if (!isRecord(payload)) {
    throw new Error(`${responseLabel(path)} returned malformed data.`);
  }
  if (path.endsWith("/models")) {
    if (!Array.isArray(payload.models) || !payload.models.every(isRecord)) {
      throw new Error("Model catalog returned malformed data.");
    }
  } else if (path.endsWith("/settings")) {
    if (!isRecord(payload.models)) {
      throw new Error("Model settings returned malformed data.");
    }
  } else if (path.endsWith("/settings/model")) {
    if (payload.status !== "ok") {
      throw new Error("Model preference was not saved.");
    }
  } else if (path.endsWith("/route")) {
    if (
      typeof payload.route_type !== "string"
      || typeof payload.query !== "string"
      || !Array.isArray(payload.target_candidates)
    ) {
      throw new Error("Routing service returned malformed data.");
    }
  } else if (path.endsWith("/answer")) {
    if (payload.status !== "ok" && payload.status !== "error") {
      throw new Error("Answer service returned malformed data.");
    }
  }
  return payload;
}
