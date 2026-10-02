import type { RouteDecision, RouteSessionState } from "./routerTypes";
import type { SearchHomeAttachment } from "./attachments";
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

type SearchHomeRequestErrorCode =
  | "ATTACHMENT_MODEL_UNSUPPORTED"
  | "INVALID_ATTACHMENT"
  | "INVALID_INPUT";

const REQUEST_ERROR_MESSAGES: Record<SearchHomeRequestErrorCode, string> = {
  ATTACHMENT_MODEL_UNSUPPORTED:
    "The selected model does not advertise image input. Choose a vision-capable model or remove the image.",
  INVALID_ATTACHMENT:
    "The server rejected the attachment. Remove it or choose a supported file and retry.",
  INVALID_INPUT:
    "Search Home rejected the request. Review the query and attachment, then retry.",
};

export class SearchHomeRequestError extends Error {
  readonly code: string;

  constructor(code: string) {
    super("Search Home request failed");
    this.name = "SearchHomeRequestError";
    this.code = code;
  }
}

export function searchHomeRequestMessage(error: unknown, fallback: string): string {
  if (error instanceof SearchHomeRequestError && error.code in REQUEST_ERROR_MESSAGES) {
    return REQUEST_ERROR_MESSAGES[error.code as SearchHomeRequestErrorCode];
  }
  return fallback;
}

async function requestJson<T>(route: SearchHomeContractRoute, init?: RequestInit): Promise<T> {
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
    let errorCode = "";
    try {
      const payload = (await response.json()) as { error?: { code?: string } };
      errorCode = String(payload?.error?.code ?? "");
    } catch {
      console.warn("Search Home returned a malformed error response");
    }
    if (errorCode) {
      throw new SearchHomeRequestError(errorCode);
    }
    throw new Error(`Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export async function routeInput(
  input: string,
  model = "",
  attachments: SearchHomeAttachment[] = [],
): Promise<RouteDecision> {
  return requestJson<RouteDecision>(searchHomeContractRoute("api/route"), {
    method: "POST",
    body: JSON.stringify({ input, model, attachments }),
  });
}

export async function answerInput(
  input: string,
  model = "",
  attachments: SearchHomeAttachment[] = [],
): Promise<SearchAnswerResponse> {
  return requestJson<SearchAnswerResponse>(searchHomeContractRoute("api/answer"), {
    method: "POST",
    body: JSON.stringify({ input, model, use_search: true, attachments }),
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
