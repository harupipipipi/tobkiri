import { api, type AuthorityRequest } from "./api";

const AUTHORITY_APPROVAL_CHANNEL = "rumi-authority-approval.v2";
const AUTHORITY_APPROVAL_MESSAGE_TYPE = "rumi-authority-approval-hint";
const LEGACY_AUTHORITY_APPROVAL_STORAGE_KEY = "rumi.authority.approval.settlement";
const AUTHORITY_APPROVAL_HINT_MAX_AGE_MS = 30_000;
const AUTHORITY_APPROVAL_RETURN_MAX_AGE_MS = 5 * 60_000;
export const AUTHORITY_APPROVAL_RETURN_PARAM = "authority_return";
export const AUTHORITY_APPROVAL_RETURN_STORAGE_KEY = "rumi.authority.approval.return.v1";

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

type AuthorityRequestFetcher = (requestId: string) => Promise<AuthorityRequest>;
type AuthorityApprovalReturnStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

type AuthorityApprovalReturnRecord = {
  requestId: string;
  nonce: string;
  createdAt: number;
};

function cleanString(value: unknown): string | null {
  const text = String(value ?? "").trim();
  return text || null;
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

function browserSessionStorage(): AuthorityApprovalReturnStorage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

function samePagePath(currentHref: string): URL {
  const origin = typeof window === "undefined" ? "http://tobkiri.local" : window.location.origin;
  return new URL(currentHref, origin);
}

export function createAuthorityApprovalReturnPath(
  requestId: string,
  currentHref: string,
  storage: AuthorityApprovalReturnStorage | null = browserSessionStorage(),
  now = Date.now(),
): string {
  const url = samePagePath(currentHref);
  url.searchParams.delete("authority_approved");
  url.searchParams.delete(AUTHORITY_APPROVAL_RETURN_PARAM);
  const normalizedRequestId = cleanString(requestId);
  if (!normalizedRequestId || !storage) return `${url.pathname}${url.search}${url.hash}`;
  const record: AuthorityApprovalReturnRecord = {
    requestId: normalizedRequestId,
    nonce: createNonce(),
    createdAt: now,
  };
  try {
    storage.setItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY, JSON.stringify(record));
    url.searchParams.set(AUTHORITY_APPROVAL_RETURN_PARAM, record.nonce);
  } catch {
    try {
      storage.removeItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY);
    } catch {
      // A locked session store leaves the return URL as a non-settling page reload.
    }
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

export function consumeAuthorityApprovalReturnHint(
  search: string,
  storage: AuthorityApprovalReturnStorage | null = browserSessionStorage(),
  now = Date.now(),
): string | null {
  const nonce = cleanString(new URLSearchParams(search).get(AUTHORITY_APPROVAL_RETURN_PARAM));
  if (!nonce || !storage) return null;
  let serialized: string | null = null;
  try {
    serialized = storage.getItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY);
    storage.removeItem(AUTHORITY_APPROVAL_RETURN_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!serialized) return null;
  try {
    const value = JSON.parse(serialized) as Partial<AuthorityApprovalReturnRecord>;
    const requestId = cleanString(value.requestId);
    const storedNonce = cleanString(value.nonce);
    const createdAt = Number(value.createdAt);
    if (!requestId || !storedNonce || storedNonce !== nonce || !Number.isFinite(createdAt)) return null;
    if (createdAt > now + 5_000 || now - createdAt > AUTHORITY_APPROVAL_RETURN_MAX_AGE_MS) return null;
    return requestId;
  } catch {
    return null;
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

export async function verifyAuthorityApprovalRequest(
  requestId: string,
  expectedConversationId: string | null = null,
  fetchRequest: AuthorityRequestFetcher = (requestId) => api.getAuthorityRequest(requestId),
  now = Date.now(),
): Promise<AuthorityApprovalSettlement | null> {
  let request: AuthorityRequest;
  try {
    request = await fetchRequest(requestId);
  } catch {
    return null;
  }
  if (String(request.request_id || "") !== requestId) return null;
  if (request.status !== "approved" && request.status !== "denied") return null;
  const expiresAt = Date.parse(String(request.expires_at ?? ""));
  if (Number.isFinite(expiresAt) && expiresAt <= now) return null;
  const authoritativeConversationId = cleanString(request.conversation_id);
  if (expectedConversationId && authoritativeConversationId !== expectedConversationId) return null;
  return {
    requestId: request.request_id,
    status: request.status,
    conversationId: authoritativeConversationId,
  };
}

export async function verifyAuthorityApprovalHint(
  hint: AuthorityApprovalHint,
  fetchRequest: AuthorityRequestFetcher = (requestId) => api.getAuthorityRequest(requestId),
  now = Date.now(),
): Promise<AuthorityApprovalSettlement | null> {
  if (hint.emittedAt > now + 5_000 || now - hint.emittedAt > AUTHORITY_APPROVAL_HINT_MAX_AGE_MS) return null;
  return verifyAuthorityApprovalRequest(hint.requestId, hint.conversationId ?? null, fetchRequest, now);
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
    void verifyAuthorityApprovalHint(hint)
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
