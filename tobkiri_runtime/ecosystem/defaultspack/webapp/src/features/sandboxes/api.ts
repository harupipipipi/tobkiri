import {
  defaultspackApiFetch,
  defaultspackContractRoute,
  explainDefaultspackApiError,
  type DefaultspackContractRoute,
} from "../../lib/api";
import { normalizeDesktopProvisioningStatus, normalizeDesktopStatus, normalizeSandboxState } from "./types";
import type {
  CreateDesktopRequest,
  DesktopAccessPolicy,
  DesktopAccessRequest,
  DesktopControlLeaseGrant,
  DesktopControlLeaseRenewal,
  DesktopFrameQuality,
  DesktopFrameResult,
  DesktopInputRequest,
  DesktopInstance,
  DesktopRules,
  RuntimeDoctorResult,
  RuntimeOperation,
  RuntimeProvidersResponse,
  SandboxInstance,
  SandboxTemplate,
} from "./types";

type ApiEnvelope<T> =
  | { status: "ok"; data: T }
  | { status: "error"; error: { code?: string; message?: string } };

type DesktopListPayload = { desktops: unknown[] };

function encodeId(value: string): string {
  return encodeURIComponent(value);
}

function requestId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function request<T>(path: DefaultspackContractRoute, init?: RequestInit): Promise<T> {
  const response = await defaultspackApiFetch(path, init);
  let payload: ApiEnvelope<T>;
  try {
    payload = await response.json() as ApiEnvelope<T>;
  } catch {
    throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
  }
  if (!response.ok || payload.status === "error") {
    throw new Error(explainDefaultspackApiError(
      response.status,
      payload.status === "error" ? payload.error : undefined,
      response.statusText,
    ));
  }
  return payload.data;
}

function numberHeader(response: Response, names: string[], fallback: number): number {
  for (const name of names) {
    const raw = response.headers.get(name);
    if (!raw) continue;
    const parsed = Number(raw);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function stringHeader(response: Response, names: string[], fallback: string | null = null): string | null {
  for (const name of names) {
    const value = response.headers.get(name);
    if (value && value.trim()) return value.trim();
  }
  return fallback;
}

function normalizeSandboxInstance(instance: SandboxInstance): SandboxInstance {
  const state = normalizeSandboxState(instance.state ?? instance.status);
  return {
    ...instance,
    state,
    status: state,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function trimString(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const text = String(value).trim();
  return text ? text : null;
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    const text = trimString(value);
    if (text) return text;
  }
  return null;
}

function desktopSeatId(record: Record<string, unknown>): string | null {
  return firstString(
    record.seat_id,
    record.seatId,
    record.sandbox_id,
    record.sandboxId,
    record.desktop_id,
    record.desktopId,
    record.id,
  );
}

function desktopStatusValue(record: Record<string, unknown>): unknown {
  const status = record.status
    ?? record.state
    ?? record.desktop_status
    ?? record.desktopStatus
    ?? record.lifecycle_state
    ?? record.lifecycleState;
  if (trimString(status)) return status;
  if (record.running === true || record.ready === true) return "running";
  return status;
}

function normalizeDesktopInstanceOrNull(instance: unknown): DesktopInstance | null {
  if (!isRecord(instance)) return null;
  const seatId = desktopSeatId(instance);
  if (!seatId) return null;
  const provisioning = isRecord(instance.provisioning)
    ? {
        ...instance.provisioning,
        status: normalizeDesktopProvisioningStatus(instance.provisioning.status ?? instance.provisioning.state),
      }
    : instance.provisioning;
  return {
    ...(instance as unknown as DesktopInstance),
    seat_id: seatId,
    sandbox_id: firstString(instance.sandbox_id, instance.sandboxId) ?? seatId,
    name: firstString(instance.name, instance.display_name, instance.displayName, instance.label) ?? `Desktop ${seatId}`,
    status: normalizeDesktopStatus(desktopStatusValue(instance)),
    assigned_agent: firstString(instance.assigned_agent, instance.assigned_agent_id, instance.assignedAgentId),
    provisioning: provisioning as DesktopInstance["provisioning"],
  };
}

function normalizeDesktopInstance(instance: unknown): DesktopInstance {
  const normalized = normalizeDesktopInstanceOrNull(instance);
  if (!normalized) {
    throw new Error("Desktop response did not include a usable desktop record.");
  }
  return normalized;
}

function normalizeDesktopListPayload(payload: DesktopListPayload): { desktops: DesktopInstance[] } {
  const desktops = payload.desktops
    .map(normalizeDesktopInstanceOrNull)
    .filter((desktop): desktop is DesktopInstance => desktop !== null);
  if (payload.desktops.length > 0 && desktops.length === 0) {
    throw new Error("Desktop list response contained desktop records, but none included a usable desktop id.");
  }
  return { desktops };
}

function unwrapDesktopListPayload(payload: unknown): DesktopListPayload {
  const envelope = payload as Partial<ApiEnvelope<DesktopListPayload>>;
  const data = envelope.status === "ok" && "data" in envelope
    ? envelope.data
    : payload;
  if (!data || typeof data !== "object" || !Array.isArray((data as DesktopListPayload).desktops)) {
    throw new Error("Desktop list response did not include a desktops array.");
  }
  return data as DesktopListPayload;
}

async function requestDesktopList(): Promise<{ desktops: DesktopInstance[] }> {
  const response = await defaultspackApiFetch(defaultspackContractRoute("api/desktops"), { cache: "no-store" });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
  }
  const envelope = payload as Partial<ApiEnvelope<DesktopListPayload>>;
  if (!response.ok || envelope.status === "error") {
    throw new Error(explainDefaultspackApiError(
      response.status,
      envelope.status === "error" ? envelope.error : undefined,
      response.statusText,
    ));
  }
  return normalizeDesktopListPayload(unwrapDesktopListPayload(payload));
}

function normalizeLeaseExpiresAt(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) {
    const milliseconds = value > 10_000_000_000 ? value : value * 1000;
    return new Date(milliseconds).toISOString();
  }
  return typeof value === "string" ? value : "";
}

function normalizeDesktopLeaseGrant(lease: DesktopControlLeaseGrant & { expires_at?: unknown }): DesktopControlLeaseGrant {
  return {
    ...lease,
    expires_at: normalizeLeaseExpiresAt(lease.expires_at),
  };
}

function normalizeDesktopLeaseRenewal(lease: DesktopControlLeaseRenewal & { expires_at?: unknown }): DesktopControlLeaseRenewal {
  return {
    ...lease,
    expires_at: normalizeLeaseExpiresAt(lease.expires_at),
  };
}

export async function fetchDesktopFrame(
  seatId: string,
  options: {
    afterSeq?: number | null;
    quality?: DesktopFrameQuality;
    accessKey?: string | null;
    signal?: AbortSignal;
  } = {},
): Promise<DesktopFrameResult> {
  const query = new URLSearchParams();
  if (typeof options.afterSeq === "number") query.set("after", String(options.afterSeq));
  if (options.quality) query.set("quality", options.quality);

  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await defaultspackApiFetch(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/frame${suffix}`), {
    method: "GET",
    headers: {
      Accept: "image/webp,image/jpeg,image/png",
      ...(options.accessKey ? { "X-Rumi-Desktop-Session-Credential": options.accessKey } : {}),
    },
    cache: "no-store",
    signal: options.signal,
  });

  if (response.status === 204) {
    return { status: "not_modified", seat_id: seatId, after_seq: options.afterSeq ?? null };
  }
  if (!response.ok) {
    throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
  }

  const blob = await response.blob();
  const fallbackSeq = typeof options.afterSeq === "number" ? options.afterSeq + 1 : 0;
  return {
    status: "frame",
    seat_id: seatId,
    frame_seq: numberHeader(response, ["X-Rumi-Frame-Seq", "X-Frame-Seq"], fallbackSeq),
    width: numberHeader(response, ["X-Rumi-Frame-Width", "X-Frame-Width"], 0),
    height: numberHeader(response, ["X-Rumi-Frame-Height", "X-Frame-Height"], 0),
    mime_type: response.headers.get("Content-Type")?.split(";")[0]?.trim() || blob.type || "image/jpeg",
    blob,
    captured_at: stringHeader(response, ["X-Rumi-Captured-At", "X-Captured-At"]),
  };
}

export const sandboxesApi = {
  listRuntimeProviders() {
    return request<RuntimeProvidersResponse>(defaultspackContractRoute("api/runtime/providers"), { cache: "no-store" });
  },

  runRuntimeDoctor() {
    return request<RuntimeDoctorResult>(defaultspackContractRoute("api/runtime/doctor"), {
      method: "POST",
      body: JSON.stringify({ request_id: requestId("doctor") }),
    });
  },

  ensureRuntime(providerId?: string | null) {
    return request<RuntimeOperation>(defaultspackContractRoute("api/runtime/ensure"), {
      method: "POST",
      body: JSON.stringify({
        request_id: requestId("ensure"),
        provider_id: providerId || undefined,
      }),
    });
  },

  getRuntimeOperation(operationId: string) {
    return request<RuntimeOperation>(defaultspackContractRoute(`api/runtime/operations/${encodeId(operationId)}`), { cache: "no-store" });
  },

  cancelRuntimeOperation(operationId: string) {
    return request<RuntimeOperation>(defaultspackContractRoute(`api/runtime/operations/${encodeId(operationId)}/cancel`), {
      method: "POST",
      body: JSON.stringify({ request_id: requestId("runtime-cancel") }),
    });
  },

  listSandboxTemplates() {
    return request<{ templates: SandboxTemplate[] }>(defaultspackContractRoute("api/sandbox/templates"), { cache: "no-store" });
  },

  listSandboxes() {
    return request<{ sandboxes: SandboxInstance[] }>(defaultspackContractRoute("api/sandboxes"), { cache: "no-store" })
      .then((payload) => ({ sandboxes: payload.sandboxes.map(normalizeSandboxInstance) }));
  },

  listDesktops() {
    return requestDesktopList();
  },

  createDesktop(payload: CreateDesktopRequest) {
    return request<DesktopInstance>(defaultspackContractRoute("api/desktops"), {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        request_id: payload.request_id ?? requestId("desktop-create"),
      }),
    }).then(normalizeDesktopInstance);
  },

  startDesktop(seatId: string, accessKey?: string | null) {
    return request<DesktopInstance>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/start`), {
      method: "POST",
      body: JSON.stringify({
        desktop_session_credential: accessKey || undefined,
        request_id: requestId("desktop-start"),
      }),
    }).then(normalizeDesktopInstance);
  },

  stopDesktop(seatId: string, accessKey?: string | null) {
    return request<DesktopInstance>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/stop`), {
      method: "POST",
      body: JSON.stringify({
        desktop_session_credential: accessKey || undefined,
        request_id: requestId("desktop-stop"),
        confirm_destructive: true,
      }),
    }).then(normalizeDesktopInstance);
  },

  restartDesktop(seatId: string, accessKey?: string | null) {
    return request<DesktopInstance>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/restart`), {
      method: "POST",
      body: JSON.stringify({
        desktop_session_credential: accessKey || undefined,
        request_id: requestId("desktop-restart"),
      }),
    }).then(normalizeDesktopInstance);
  },

  deleteDesktop(seatId: string, accessKey?: string | null) {
    const query = new URLSearchParams({
      confirm_destructive: "true",
      request_id: requestId("desktop-delete"),
    });
    return request<{ deleted: boolean; seat_id: string }>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}?${query.toString()}`), {
      method: "DELETE",
      headers: {
        ...(accessKey ? { "X-Rumi-Desktop-Session-Credential": accessKey } : {}),
      },
    });
  },

  updateDesktopRules(
    seatId: string,
    payload: {
      role?: string | null;
      rules?: DesktopRules | string[] | null;
      access?: DesktopAccessPolicy & { access_key?: string };
      access_key?: string;
    },
  ) {
    return request<DesktopInstance>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/rules`), {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        request_id: requestId("desktop-rules"),
      }),
    }).then(normalizeDesktopInstance);
  },

  requestDesktopAccess(seatId: string, reason?: string) {
    return request<DesktopAccessRequest>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/access-requests`), {
      method: "POST",
      body: JSON.stringify({
        reason,
        request_id: requestId("desktop-access"),
      }),
    });
  },

  issueDesktopExchange(seatId: string, operations: string[]) {
    return request<{ exchange_code: string }>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/access-exchanges`), {
      method: "POST",
      body: JSON.stringify({ operations, request_id: requestId("desktop-exchange-issue") }),
    });
  },

  redeemDesktopExchange(exchangeCode: string) {
    return request<{ session_credential: string; credential_id: string; expires_at: number }>(
      defaultspackContractRoute("api/desktop-access/exchange"),
      {
        method: "POST",
        body: JSON.stringify({
          exchange_code: exchangeCode,
          request_id: requestId("desktop-exchange-redeem"),
        }),
      },
    );
  },

  listDesktopGrants(seatId: string) {
    return request<{ grants: Array<Record<string, unknown>> }>(
      defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/access-grants`),
      { cache: "no-store" },
    );
  },

  revokeDesktopGrant(seatId: string, grantId: string) {
    return request<{ revoked: boolean }>(
      defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/access-grants/${encodeId(grantId)}`),
      { method: "DELETE" },
    );
  },

  grantDesktopAccess(seatId: string, accessRequestId: string, approved = true) {
    return request<DesktopAccessRequest>(
      defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/access-requests/${encodeId(accessRequestId)}/grant`),
      {
        method: "POST",
        body: JSON.stringify({
          approved,
          request_id: requestId("desktop-access-grant"),
        }),
      },
    );
  },

  fetchDesktopFrame,

  acquireDesktopControl(seatId: string, accessKey?: string | null) {
    return request<DesktopControlLeaseGrant>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/control/acquire`), {
      method: "POST",
      body: JSON.stringify({
        desktop_session_credential: accessKey || undefined,
        request_id: requestId("desktop-control-acquire"),
      }),
    }).then(normalizeDesktopLeaseGrant);
  },

  renewDesktopControl(seatId: string, leaseToken: string, accessKey?: string | null) {
    return request<DesktopControlLeaseRenewal>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/control/renew`), {
      method: "POST",
      body: JSON.stringify({
        desktop_session_credential: accessKey || undefined,
        lease_token: leaseToken,
        request_id: requestId("desktop-control-renew"),
      }),
    }).then(normalizeDesktopLeaseRenewal);
  },

  releaseDesktopControl(seatId: string, leaseToken: string, accessKey?: string | null) {
    return request<{ released: boolean; seat_id: string }>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/control/release`), {
      method: "POST",
      body: JSON.stringify({
        desktop_session_credential: accessKey || undefined,
        lease_token: leaseToken,
        request_id: requestId("desktop-control-release"),
      }),
    });
  },

  sendDesktopInput(seatId: string, payload: DesktopInputRequest) {
    return request<{ accepted: boolean; seat_id: string }>(defaultspackContractRoute(`api/desktops/${encodeId(seatId)}/input`), {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        client_action_id: payload.client_action_id ?? requestId("desktop-action"),
        request_id: payload.request_id ?? requestId("desktop-input"),
      }),
    });
  },
};
