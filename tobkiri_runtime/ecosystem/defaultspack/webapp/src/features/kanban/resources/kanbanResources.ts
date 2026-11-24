import {
  defaultspackApiFetch,
  defaultspackContractRoute,
  explainDefaultspackApiError,
  type KanbanBoardResponse,
  type KanbanBoardScope,
  type KanbanImportConversationPayload,
  type KanbanMovePayload,
  type DefaultspackContractRoute,
} from "../../../lib/api";
import { parseProfileScreenPath } from "../../../lib/profileRoute";

export class KanbanApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "KanbanApiError";
    this.status = status;
  }
}

/** Only a successful scoped list can establish that a board is missing. */
export class KanbanBoardMissingError extends KanbanApiError {
  constructor() {
    super(404, "Kanban board was not found.");
    this.name = "KanbanBoardMissingError";
  }
}

const revisions = new Map<string, number>();
const scopeRevisions = new Map<string, number>();

function currentProfile(): string {
  return typeof window === "undefined" ? ""
    : parseProfileScreenPath(window.location.pathname)?.profileId ?? "";
}

function revisionKey(boardId: string, profile = currentProfile()): string {
  return `${profile}:${boardId}`;
}

function capturedRevision(boardId: string): number {
  const revision = revisions.get(revisionKey(boardId));
  if (revision === undefined) throw new Error("Reload the Kanban board before changing it.");
  return revision;
}

type RequestCandidate = {
  path: DefaultspackContractRoute;
  method?: string;
  body?: Record<string, unknown>;
};

type ApiEnvelope<T> = {
  status?: string;
  success?: boolean;
  data?: T;
  error?: { code?: string; message?: string };
};

function encode(value: string): string {
  return encodeURIComponent(value);
}

function boardQuery(scope: KanbanBoardScope): string {
  const query = new URLSearchParams({ scope_type: scope.type, scope_id: scope.id });
  return query.toString();
}

function unwrapPayload<T>(payload: unknown): T {
  if (payload && typeof payload === "object" && "data" in payload) {
    return (payload as ApiEnvelope<T>).data as T;
  }
  return payload as T;
}

function normalizeBoardResponse(value: unknown, profile: string, scope?: KanbanBoardScope, expectedId?: string): KanbanBoardResponse {
  const payload = unwrapPayload<Partial<KanbanBoardResponse>>(value);
  if (!payload || typeof payload !== "object" || !payload.board || typeof payload.board !== "object") {
    throw new Error("Kanban API returned an invalid board response.");
  }
  if ((scope && !boardMatchesScope(payload.board, scope))
    || (expectedId && boardId(payload.board) !== expectedId)) {
    throw new Error("Kanban API returned a different board.");
  }
  const revision = (payload as Partial<KanbanBoardResponse> & { revision?: unknown }).revision;
  if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 0) {
    throw new Error("Kanban API returned an invalid board revision.");
  }
  revisions.set(revisionKey(boardId(payload.board), profile), revision);
  return {
    board: payload.board,
    columns: Array.isArray(payload.columns) ? payload.columns : [],
    cards: Array.isArray(payload.cards) ? payload.cards : [],
    events: Array.isArray(payload.events) ? payload.events : [],
    imported: payload.imported,
  };
}

function boardId(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const board = value as Record<string, unknown>;
  return String(board.board_id ?? board.id ?? "").trim();
}

function boardMatchesScope(value: unknown, scope: KanbanBoardScope): boolean {
  if (!value || typeof value !== "object") return false;
  const board = value as Record<string, unknown>;
  const nestedScope = board.scope;
  const nested = nestedScope && typeof nestedScope === "object"
    ? nestedScope as Record<string, unknown>
    : {};
  return String(board.scope_type ?? nested.type ?? "").trim() === scope.type
    && String(board.scope_id ?? nested.id ?? "").trim() === scope.id;
}

function snapshotForScope(value: unknown, scope: KanbanBoardScope, profile: string): KanbanBoardResponse | null {
  const payload = unwrapPayload<Record<string, unknown>>(value);
  if (!payload || typeof payload !== "object") return null;
  if (payload.board && typeof payload.board === "object") {
    const board = payload.board as Record<string, unknown>;
    if (boardMatchesScope(board, scope)) return normalizeBoardResponse(payload, profile, scope);
  }
  const boards = Array.isArray(payload.boards) ? payload.boards : [];
  for (const candidate of boards) {
    if (!candidate || typeof candidate !== "object") continue;
    const snapshot = candidate as Record<string, unknown>;
    const board = snapshot.board;
    if (board && typeof board === "object" && boardMatchesScope(board, scope)) {
      return normalizeBoardResponse(snapshot, profile, scope);
    }
  }
  return null;
}

function boardSummaryForScope(value: unknown, scope: KanbanBoardScope): Record<string, unknown> | null {
  const payload = unwrapPayload<Record<string, unknown>>(value);
  if (!payload || typeof payload !== "object") return null;
  const boards = Array.isArray(payload.boards) ? payload.boards : [];
  for (const candidate of boards) {
    if (!candidate || typeof candidate !== "object") continue;
    const board = candidate as Record<string, unknown>;
    if (boardMatchesScope(board, scope)) return board;
    if (board.board && typeof board.board === "object"
      && boardMatchesScope(board.board, scope)) {
      return board.board as Record<string, unknown>;
    }
  }
  return null;
}

async function requestCandidates<T>(candidates: RequestCandidate[], profile = currentProfile()): Promise<T> {
  let lastError: KanbanApiError | null = null;
  for (const candidate of candidates) {
    const method = candidate.method ?? "GET";
    const response = await defaultspackApiFetch(candidate.path, {
      method,
      cache: method === "GET" ? "no-store" : undefined,
      body: candidate.body ? JSON.stringify(candidate.body) : undefined,
    });

    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }

    if (currentProfile() !== profile) {
      throw new Error("Kanban Profile changed while the request was loading.");
    }

    if (response.ok) {
      const envelope = payload as ApiEnvelope<T> | null;
      if (envelope?.status === "error" || envelope?.success === false) {
        throw new KanbanApiError(
          response.status,
          explainDefaultspackApiError(response.status, envelope.error, response.statusText),
        );
      }
      const value = unwrapPayload<T>(payload);
      if (value && typeof value === "object" && "state" in value && value.state === "error") {
        throw new KanbanApiError(response.status, "Kanban operation failed. Reload before retrying.");
      }
      return value;
    }

    const envelope = payload as ApiEnvelope<T> | null;
    lastError = new KanbanApiError(
      response.status,
      explainDefaultspackApiError(response.status, envelope?.error, response.statusText),
    );
    if (response.status !== 404 && response.status !== 405) throw lastError;
  }
  throw lastError ?? new KanbanApiError(0, "Kanban API is unavailable.");
}

export type CreateKanbanCardInput = {
  title: string;
  description?: string;
  priority?: string;
  conversation_id?: string | null;
  workspace_id?: string | null;
  company_id?: string | null;
};

export type KanbanDataSource = {
  loadBoard(scope: KanbanBoardScope): Promise<KanbanBoardResponse>;
  ensureBoard(scope: KanbanBoardScope, title: string): Promise<KanbanBoardResponse>;
  createCard(boardId: string, columnId: string, input: CreateKanbanCardInput): Promise<void>;
  moveCard(boardId: string, cardId: string, payload: KanbanMovePayload): Promise<void>;
  deleteCard(boardId: string, cardId: string): Promise<void>;
  importConversation(boardId: string, payload: KanbanImportConversationPayload): Promise<void>;
};

export const kanbanResources: KanbanDataSource = {
  async loadBoard(scope) {
    const profile = currentProfile();
    const payload = await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/boards?${boardQuery(scope)}`) },
    ], profile);
    if (!payload || typeof payload !== "object" || !Array.isArray((payload as { boards?: unknown }).boards)) {
      throw new Error("Kanban API returned an invalid board list.");
    }
    const revision = (payload as { revision?: unknown }).revision;
    if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 0) {
      throw new Error("Kanban API returned an invalid list revision.");
    }
    scopeRevisions.set(`${profile}:${boardQuery(scope)}`, revision);
    const snapshot = snapshotForScope(payload, scope, profile);
    if (snapshot) return snapshot;

    const summary = boardSummaryForScope(payload, scope);
    const id = boardId(summary);
    if (!id) throw new KanbanBoardMissingError();
    const board = await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/board?board_id=${encode(id)}`) },
    ], profile);
    return normalizeBoardResponse(board, profile, scope, id);
  },

  async ensureBoard(scope, title) {
    const profile = currentProfile();
    const expectedRevision = scopeRevisions.get(`${profile}:${boardQuery(scope)}`);
    if (expectedRevision === undefined) throw new Error("Load the Kanban board list before creating a board.");
    const payload = await requestCandidates<unknown>([
      { path: defaultspackContractRoute("api/kanban/boards/bootstrap"), method: "POST",
        body: { scope_type: scope.type, scope_id: scope.id, title, expected_revision: expectedRevision } },
    ], profile);
    return normalizeBoardResponse(payload, profile, scope);
  },

  async createCard(boardId, columnId, input) {
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute("api/kanban/cards"), method: "POST",
        body: { board_id: boardId, column_id: columnId, ...input,
          expected_revision: capturedRevision(boardId) } },
    ]);
  },

  async moveCard(boardId, cardId, payload) {
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute("api/kanban/cards/move"), method: "POST",
        body: { board_id: boardId, card_id: cardId, ...payload,
          expected_revision: capturedRevision(boardId) } },
    ]);
  },

  async deleteCard(boardId, cardId) {
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute("api/kanban/cards"), method: "DELETE",
        body: { board_id: boardId, card_id: cardId, expected_revision: capturedRevision(boardId) } },
    ]);
  },

  async importConversation(boardId, payload) {
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute("api/kanban/import-conversation"), method: "POST",
        body: { board_id: boardId, ...payload, expected_revision: capturedRevision(boardId) } },
    ]);
  },
};
