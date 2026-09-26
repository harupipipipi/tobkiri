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

export class KanbanApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "KanbanApiError";
    this.status = status;
  }
}

type RequestCandidate = {
  path: DefaultspackContractRoute;
  method?: string;
  body?: Record<string, unknown>;
};

type ApiEnvelope<T> = {
  status?: string;
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

function normalizeBoardResponse(value: unknown): KanbanBoardResponse {
  const payload = unwrapPayload<Partial<KanbanBoardResponse>>(value);
  if (!payload || typeof payload !== "object" || !payload.board || typeof payload.board !== "object") {
    throw new Error("Kanban API returned an invalid board response.");
  }
  return {
    board: payload.board,
    columns: Array.isArray(payload.columns) ? payload.columns : [],
    cards: Array.isArray(payload.cards) ? payload.cards : [],
    events: Array.isArray(payload.events) ? payload.events : [],
    imported: payload.imported,
  };
}

async function requestCandidates<T>(candidates: RequestCandidate[]): Promise<T> {
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

    if (response.ok) {
      const envelope = payload as ApiEnvelope<T> | null;
      if (envelope?.status === "error") {
        throw new KanbanApiError(
          response.status,
          explainDefaultspackApiError(response.status, envelope.error, response.statusText),
        );
      }
      return unwrapPayload<T>(payload);
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
    const query = boardQuery(scope);
    const payload = await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/boards?${query}`) },
      { path: defaultspackContractRoute(`api/kanban/board?${query}`) },
      { path: defaultspackContractRoute(`api/kanban?${query}`) },
    ]);
    return normalizeBoardResponse(payload);
  },

  async ensureBoard(scope, title) {
    const body = { scope_type: scope.type, scope_id: scope.id, title };
    const payload = await requestCandidates<unknown>([
      { path: defaultspackContractRoute("api/kanban/boards"), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban/board"), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban"), method: "POST", body: { action: "ensure", ...body } },
    ]);
    return normalizeBoardResponse(payload);
  },

  async createCard(boardId, columnId, input) {
    const body = { board_id: boardId, column_id: columnId, ...input };
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/boards/${encode(boardId)}/cards`), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban/cards"), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban"), method: "POST", body: { action: "create_card", ...body } },
    ]);
  },

  async moveCard(boardId, cardId, payload) {
    const body = { board_id: boardId, card_id: cardId, ...payload };
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/cards/${encode(cardId)}/move`), method: "POST", body },
      { path: defaultspackContractRoute(`api/kanban/boards/${encode(boardId)}/cards/${encode(cardId)}/move`), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban"), method: "POST", body: { action: "move_card", ...body } },
    ]);
  },

  async deleteCard(boardId, cardId) {
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/cards/${encode(cardId)}?board_id=${encode(boardId)}`), method: "DELETE" },
      { path: defaultspackContractRoute(`api/kanban/boards/${encode(boardId)}/cards/${encode(cardId)}`), method: "DELETE" },
      { path: defaultspackContractRoute("api/kanban"), method: "POST", body: { action: "delete_card", board_id: boardId, card_id: cardId } },
    ]);
  },

  async importConversation(boardId, payload) {
    const body = { board_id: boardId, ...payload };
    await requestCandidates<unknown>([
      { path: defaultspackContractRoute(`api/kanban/boards/${encode(boardId)}/import-conversation`), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban/import-conversation"), method: "POST", body },
      { path: defaultspackContractRoute("api/kanban"), method: "POST", body: { action: "import_conversation", ...body } },
    ]);
  },
};
