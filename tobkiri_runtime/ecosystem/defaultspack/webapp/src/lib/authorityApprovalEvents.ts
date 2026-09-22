import { api, type AuthorityRequest } from "./api";

const AUTHORITY_APPROVAL_CHANNEL = "rumi-authority-approval.v2";
const AUTHORITY_APPROVAL_MESSAGE_TYPE = "rumi-authority-approval-hint";
const LEGACY_AUTHORITY_APPROVAL_STORAGE_KEY = "rumi.authority.approval.settlement";
const AUTHORITY_APPROVAL_HINT_MAX_AGE_MS = 30_000;

export type AuthorityApprovalSettlement = {
  requestId: string;
  status: "approved" | "denied";
  conversationId?: string | null;
};

export type AuthorityApprovalHint = {
  requestId: string;
  conversationId?: string | null;
  emittedAt: number;
  nonce: string;
};

type SubscribeAuthorityApprovalSettlementOptions = {
  replayStored?: boolean;
  replayStoredRequestId?: string;
  expected?: AuthorityApprovalVerificationBinding;
};

export type AuthorityApprovalVerificationBinding = {
  requestId: string;
  principalId?: string | null;
  permissionId?: string | null;
  conversationId?: string | null;
  resource?: Record<string, unknown> | null;
};

type AuthorityRequestFetcher = (requestId: string) => Promise<AuthorityRequest>;

function cleanString(value: unknown): string | null {
  const text = String(value ?? "").trim();
  return text || null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function createNonce(): string {
  try {
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  }
}

function hintFromMessage(value: unknown, now = Date.now()): AuthorityApprovalHint | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (record.type !== AUTHORITY_APPROVAL_MESSAGE_TYPE) return null;
  const hint = record.hint;
  if (!hint || typeof hint !== "object" || Array.isArray(hint)) return null;
  const hintRecord = hint as Record<string, unknown>;
  if (Object.prototype.hasOwnProperty.call(hintRecord, "status")) return null;
  const requestId = cleanString(hintRecord.requestId);
  const nonce = cleanString(hintRecord.nonce);
  const emittedAt = Number(hintRecord.emittedAt);
  const conversationId = cleanString(hintRecord.conversationId);
  if (!requestId || !nonce || !Number.isFinite(emittedAt)) return null;
  if (emittedAt > now + 5_000 || now - emittedAt > AUTHORITY_APPROVAL_HINT_MAX_AGE_MS) return null;
  return {
    requestId,
    conversationId,
    emittedAt,
    nonce,
  };
}

export function authorityApprovalHintMessage(
  event: Pick<AuthorityApprovalSettlement, "requestId" | "conversationId">,
  now = Date.now(),
): { type: string; hint: AuthorityApprovalHint } {
  return {
    type: AUTHORITY_APPROVAL_MESSAGE_TYPE,
    hint: {
      requestId: event.requestId,
      conversationId: event.conversationId ?? null,
      emittedAt: now,
      nonce: createNonce(),
    },
  };
}

export async function verifyAuthorityApprovalHint(
  hint: AuthorityApprovalHint,
  fetchRequest: AuthorityRequestFetcher = (requestId) => api.getAuthorityRequest(requestId),
  now = Date.now(),
  expected?: AuthorityApprovalVerificationBinding,
): Promise<AuthorityApprovalSettlement | null> {
  if (hint.emittedAt > now + 5_000 || now - hint.emittedAt > AUTHORITY_APPROVAL_HINT_MAX_AGE_MS) return null;
  if (expected && expected.requestId !== hint.requestId) return null;
  let request: AuthorityRequest;
  try {
    request = await fetchRequest(hint.requestId);
  } catch {
    return null;
  }
  if (String(request.request_id || "") !== hint.requestId) return null;
  if (request.status !== "approved" && request.status !== "denied") return null;
  if (request.expires_at != null) {
    const expiresAt = Date.parse(String(request.expires_at));
    if (!Number.isFinite(expiresAt) || expiresAt <= now) return null;
  }
  const authoritativeConversationId = cleanString(request.conversation_id);
  if (hint.conversationId && authoritativeConversationId !== hint.conversationId) return null;
  if (expected) {
    if (
      expected.principalId !== undefined
      && cleanString(request.principal_id) !== cleanString(expected.principalId)
    ) return null;
    if (
      expected.permissionId !== undefined
      && cleanString(request.permission_id) !== cleanString(expected.permissionId)
    ) return null;
    if (
      expected.conversationId !== undefined
      && authoritativeConversationId !== cleanString(expected.conversationId)
    ) return null;
    if (
      expected.resource !== undefined
      && stableJson(request.resource ?? {}) !== stableJson(expected.resource ?? {})
    ) return null;
  }
  return {
    requestId: request.request_id,
    status: request.status,
    conversationId: authoritativeConversationId,
  };
}

export function broadcastAuthorityApprovalSettlement(event: AuthorityApprovalSettlement): void {
  const message = authorityApprovalHintMessage(event);
  try {
    const channel = new BroadcastChannel(AUTHORITY_APPROVAL_CHANNEL);
    channel.postMessage(message);
    channel.close();
  } catch {
    // Notification delivery is optional; the authoritative request remains queryable.
  }
  try {
    window.opener?.postMessage(message, window.location.origin);
  } catch {
    // Some dedicated windows do not expose opener.
  }
  clearStoredAuthorityApprovalSettlement();
}

export function clearStoredAuthorityApprovalSettlement(_requestId?: string): void {
  try {
    window.localStorage.removeItem(LEGACY_AUTHORITY_APPROVAL_STORAGE_KEY);
  } catch {
    // Storage may be unavailable in restricted browser contexts.
  }
}

export function readStoredAuthorityApprovalSettlement(
  _options?: { requestId?: string; maxAgeMs?: number },
): AuthorityApprovalSettlement | null {
  clearStoredAuthorityApprovalSettlement();
  return null;
}

export function subscribeAuthorityApprovalSettlements(
  handler: (event: AuthorityApprovalSettlement) => void,
  options?: SubscribeAuthorityApprovalSettlementOptions,
): () => void {
  let active = true;
  let channel: BroadcastChannel | null = null;
  const seenNonces = new Set<string>();
  const inFlightRequestIds = new Set<string>();
  const settledRequestIds = new Set<string>();

  const acceptHint = (value: unknown) => {
    const hint = hintFromMessage(value);
    if (!hint || seenNonces.has(hint.nonce) || settledRequestIds.has(hint.requestId)) return;
    seenNonces.add(hint.nonce);
    if (inFlightRequestIds.has(hint.requestId)) return;
    inFlightRequestIds.add(hint.requestId);
    void verifyAuthorityApprovalHint(hint, undefined, Date.now(), options?.expected)
      .then((settlement) => {
        if (!active || !settlement) return;
        settledRequestIds.add(settlement.requestId);
        handler(settlement);
      })
      .finally(() => {
        inFlightRequestIds.delete(hint.requestId);
      });
  };

  try {
    channel = new BroadcastChannel(AUTHORITY_APPROVAL_CHANNEL);
    channel.onmessage = (event) => acceptHint(event.data);
  } catch {
    channel = null;
  }
  const onWindowMessage = (event: MessageEvent) => {
    if (event.origin !== window.location.origin) return;
    acceptHint(event.data);
  };
  window.addEventListener("message", onWindowMessage);
  clearStoredAuthorityApprovalSettlement();

  return () => {
    active = false;
    channel?.close();
    window.removeEventListener("message", onWindowMessage);
  };
}
