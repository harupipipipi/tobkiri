import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  GripVertical,
  Loader2,
  Move,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";

import { cn } from "../../lib/cn";
import { ErrorNotice } from "../ErrorNotice";
import {
  HISTORY_CHAT_KANBAN_DROP_EVENT,
  parseHistoryChatDragPayload,
} from "../../lib/historyComposer";
import type {
  KanbanBoardResponse,
  KanbanBoardScope,
  KanbanCard,
  KanbanColumn,
  KanbanMovePayload,
} from "../../lib/api";
import {
  KanbanApiError,
  kanbanResources,
  type KanbanDataSource,
} from "../../features/kanban/resources/kanbanResources";

const KANBAN_CARD_MIME = "application/rumi-kanban-card";

export function kanbanPriorityLabel(priority: string | undefined): string {
  const normalized = String(priority ?? "normal").trim().toLowerCase();
  if (normalized === "urgent") return "Urgent";
  if (normalized === "high") return "High";
  if (normalized === "low") return "Low";
  return normalized === "normal" || !normalized ? "Normal" : priority ?? "Normal";
}

function priorityClass(priority: string | undefined): string {
  const normalized = String(priority ?? "normal").toLowerCase();
  if (normalized === "urgent") return "border-red-400/30 bg-red-500/10 text-red-200";
  if (normalized === "high") return "border-amber-400/30 bg-amber-500/10 text-amber-200";
  if (normalized === "low") return "border-sky-400/25 bg-sky-500/10 text-sky-200";
  return "border-zinc-700 bg-zinc-900 text-zinc-400";
}

function sortedColumns(columns: KanbanColumn[]): KanbanColumn[] {
  return [...columns].sort((left, right) => Number(left.position) - Number(right.position));
}

function sortedCards(cards: KanbanCard[]): KanbanCard[] {
  return [...cards].sort((left, right) => Number(left.position) - Number(right.position));
}

export function kanbanColumnSummary(
  column: KanbanColumn,
  columnIndex: number,
  columnCount: number,
  cardCount: number,
): string {
  const limit = Number(column.wip_limit ?? 0);
  const wip = limit > 0
    ? ` WIP limit ${limit}${cardCount >= limit ? " reached" : ""}.`
    : "";
  return `${column.title}, column ${columnIndex + 1} of ${columnCount}, ${cardCount} ${cardCount === 1 ? "card" : "cards"}.${wip}`;
}

export function kanbanCardSummary(
  card: KanbanCard,
  columnTitle: string,
  cardIndex: number,
  cardCount: number,
): string {
  const checklist = Array.isArray(card.checklist) ? card.checklist : [];
  const completed = checklist.filter((item) => item.done).length;
  const blockedBy = Array.isArray(card.blocked_by) ? card.blocked_by.length : 0;
  const parts = [
    `${card.title}. ${columnTitle}, card ${cardIndex + 1} of ${cardCount}.`,
    `Priority ${kanbanPriorityLabel(card.priority)}.`,
  ];
  if (blockedBy > 0) parts.push(`Blocked by ${blockedBy} ${blockedBy === 1 ? "item" : "items"}.`);
  if (card.agent_status) parts.push(`Run status ${card.agent_status}.`);
  if (checklist.length > 0) parts.push(`Checklist ${completed} of ${checklist.length} complete.`);
  if (card.due_at) parts.push(`Due ${card.due_at}.`);
  parts.push(card.source_type === "conversation" ? "Sync source conversation." : "Sync source local card.");
  return parts.join(" ");
}

export function kanbanMoveWithinColumnPayload(
  cards: KanbanCard[],
  cardId: string,
  direction: "before" | "after",
): KanbanMovePayload | null {
  const index = cards.findIndex((card) => card.card_id === cardId);
  const targetIndex = direction === "before" ? index - 1 : index + 1;
  const target = cards[targetIndex];
  const card = cards[index];
  if (!card || !target) return null;
  return direction === "before"
    ? { column_id: card.column_id, before_card_id: target.card_id, position: targetIndex }
    : { column_id: card.column_id, after_card_id: target.card_id, position: targetIndex };
}

type UndoMove = {
  cardId: string;
  title: string;
  payload: KanbanMovePayload;
};

type KeyboardMoveSession = UndoMove & {
  moved: boolean;
};

export type KanbanWorkspacePanelProps = {
  scope: KanbanBoardScope;
  scopeLabel?: string;
  activeConversationId?: string | null;
  workspaceId?: string | null;
  companyId?: string | null;
  initialData?: KanbanBoardResponse | null;
  dataSource?: KanbanDataSource;
};

export function KanbanWorkspacePanel({
  scope,
  scopeLabel,
  activeConversationId = null,
  workspaceId = null,
  companyId = null,
  initialData = null,
  dataSource = kanbanResources,
}: KanbanWorkspacePanelProps) {
  const [boardData, setBoardData] = useState<KanbanBoardResponse | null>(initialData);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">(initialData ? "ready" : "loading");
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [draftByColumn, setDraftByColumn] = useState<Record<string, string>>({});
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [expandedCardId, setExpandedCardId] = useState<string | null>(null);
  const [moveMenuCardId, setMoveMenuCardId] = useState<string | null>(null);
  const [undoMove, setUndoMove] = useState<UndoMove | null>(null);
  const [keyboardMove, setKeyboardMove] = useState<KeyboardMoveSession | null>(null);
  const initialDataConsumedRef = useRef(Boolean(initialData));
  const cardFocusTargetsRef = useRef(new Map<string, HTMLButtonElement>());
  const pendingFocusCardIdRef = useRef<string | null>(null);
  const dragDropHandledRef = useRef(false);
  const draggedCardTitleRef = useRef("");
  const scopeType = scope.type;
  const scopeId = scope.id;
  const stableScope = useMemo<KanbanBoardScope>(() => ({ type: scopeType, id: scopeId }), [scopeId, scopeType]);
  const scopeKey = `${scopeType}:${scopeId}`;

  const loadBoard = useCallback(async () => {
    setLoadState("loading");
    setError(null);
    setStatusMessage(null);
    try {
      let next: KanbanBoardResponse;
      try {
        next = await dataSource.loadBoard(stableScope);
      } catch (reason) {
        if (!(reason instanceof KanbanApiError) || reason.status !== 404) throw reason;
        next = await dataSource.ensureBoard(stableScope, scopeLabel?.trim() || "Kanban");
      }
      setBoardData(next);
      setLoadState("ready");
    } catch (reason) {
      setLoadState("error");
      setError(reason instanceof Error ? reason.message : "Kanban board could not be loaded.");
    }
  }, [dataSource, scopeLabel, stableScope]);

  useEffect(() => {
    if (initialData && initialDataConsumedRef.current) {
      initialDataConsumedRef.current = false;
      setBoardData(initialData);
      setLoadState("ready");
      return;
    }
    void loadBoard();
  }, [initialData, loadBoard, scopeKey]);

  const columns = useMemo(() => sortedColumns(boardData?.columns ?? []), [boardData?.columns]);
  const cardsByColumn = useMemo(() => {
    const map = new Map<string, KanbanCard[]>();
    for (const column of columns) map.set(column.column_id, []);
    for (const card of sortedCards(boardData?.cards ?? [])) {
      map.set(card.column_id, [...(map.get(card.column_id) ?? []), card]);
    }
    return map;
  }, [boardData?.cards, columns]);

  useEffect(() => {
    const cardId = pendingFocusCardIdRef.current;
    if (!cardId) return;
    pendingFocusCardIdRef.current = null;
    window.requestAnimationFrame(() => cardFocusTargetsRef.current.get(cardId)?.focus());
  }, [boardData]);

  const runMutation = useCallback(async (
    key: string,
    mutation: () => Promise<void>,
    successMessage: string,
    focusCardId: string | null = null,
  ): Promise<boolean> => {
    setBusyAction(key);
    setError(null);
    setStatusMessage(null);
    try {
      await mutation();
      const next = await dataSource.loadBoard(stableScope);
      pendingFocusCardIdRef.current = focusCardId;
      setBoardData(next);
      setLoadState("ready");
      setStatusMessage(successMessage);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Kanban update failed.");
      if (focusCardId) {
        window.requestAnimationFrame(() => cardFocusTargetsRef.current.get(focusCardId)?.focus());
      }
      return false;
    } finally {
      setBusyAction(null);
    }
  }, [dataSource, stableScope]);

  const handleCreateCard = (event: FormEvent<HTMLFormElement>, column: KanbanColumn) => {
    event.preventDefault();
    if (!boardData) return;
    const title = (draftByColumn[column.column_id] ?? "").trim();
    if (!title || busyAction) return;
    void runMutation(
      `create:${column.column_id}`,
      () => dataSource.createCard(boardData.board.board_id, column.column_id, {
        title,
        priority: "normal",
        conversation_id: activeConversationId,
        workspace_id: workspaceId,
        company_id: companyId,
      }),
      `Added “${title}”.`,
    ).then((didSucceed) => {
      if (didSucceed) setDraftByColumn((current) => ({ ...current, [column.column_id]: "" }));
    });
  };

  const moveCard = useCallback(async (
    card: KanbanCard,
    payload: KanbanMovePayload,
    destination: string,
    recordUndo = true,
  ): Promise<boolean> => {
    if (!boardData || busyAction) return false;
    const targetColumn = columns.find((column) => column.column_id === payload.column_id);
    if (!targetColumn) return false;
    const targetCards = cardsByColumn.get(targetColumn.column_id) ?? [];
    const targetLimit = Number(targetColumn.wip_limit ?? 0);
    if (
      targetColumn.column_id !== card.column_id
      && targetLimit > 0
      && targetCards.length >= targetLimit
    ) {
      setStatusMessage(null);
      setError(`Cannot move “${card.title}” to ${targetColumn.title}: WIP limit ${targetLimit} is reached.`);
      window.requestAnimationFrame(() => cardFocusTargetsRef.current.get(card.card_id)?.focus());
      return false;
    }
    const sourceCards = cardsByColumn.get(card.column_id) ?? [];
    const sourceIndex = sourceCards.findIndex((candidate) => candidate.card_id === card.card_id);
    const sourceNextCard = sourceCards[sourceIndex + 1];
    const didSucceed = await runMutation(
      `move:${card.card_id}`,
      () => dataSource.moveCard(boardData.board.board_id, card.card_id, payload),
      `Moved “${card.title}” ${destination}.`,
      card.card_id,
    );
    if (didSucceed && recordUndo) {
      setUndoMove({
        cardId: card.card_id,
        title: card.title,
        payload: {
          column_id: card.column_id,
          before_card_id: sourceNextCard?.card_id,
          position: Math.max(0, sourceIndex),
        },
      });
    }
    return didSucceed;
  }, [boardData, busyAction, cardsByColumn, columns, dataSource, runMutation]);

  const undoLastMove = useCallback(async () => {
    if (!undoMove || !boardData || busyAction) return;
    const card = boardData.cards.find((candidate) => candidate.card_id === undoMove.cardId);
    const targetColumn = columns.find((column) => column.column_id === undoMove.payload.column_id);
    if (!card || !targetColumn) {
      setUndoMove(null);
      return;
    }
    const didSucceed = await moveCard(
      card,
      undoMove.payload,
      `back to ${targetColumn.title}`,
      false,
    );
    if (didSucceed) setUndoMove(null);
  }, [boardData, busyAction, columns, moveCard, undoMove]);

  const beginKeyboardMove = useCallback((card: KanbanCard) => {
    const sourceCards = cardsByColumn.get(card.column_id) ?? [];
    const sourceIndex = sourceCards.findIndex((candidate) => candidate.card_id === card.card_id);
    const sourceNextCard = sourceCards[sourceIndex + 1];
    setError(null);
    setUndoMove(null);
    setKeyboardMove({
      cardId: card.card_id,
      title: card.title,
      payload: {
        column_id: card.column_id,
        before_card_id: sourceNextCard?.card_id,
        position: Math.max(0, sourceIndex),
      },
      moved: false,
    });
    setStatusMessage(
      `Keyboard move started for “${card.title}”. Use Arrow Up or Down to reorder, `
      + "Arrow Left or Right to change column, Enter to drop, or Escape to cancel.",
    );
    window.requestAnimationFrame(() => cardFocusTargetsRef.current.get(card.card_id)?.focus());
  }, [cardsByColumn]);

  const finishKeyboardMove = useCallback(() => {
    if (!keyboardMove) return;
    setKeyboardMove(null);
    setError(null);
    setStatusMessage(
      `Dropped “${keyboardMove.title}”.${keyboardMove.moved
        ? " Undo move is available."
        : " Its position did not change."}`,
    );
    setUndoMove(keyboardMove.moved ? keyboardMove : null);
  }, [keyboardMove]);

  const cancelKeyboardMove = useCallback(async (card: KanbanCard) => {
    if (!keyboardMove) return;
    if (!keyboardMove.moved) {
      setKeyboardMove(null);
      setStatusMessage(`Keyboard move cancelled. “${card.title}” stayed in place.`);
      return;
    }
    const origin = columns.find((column) => column.column_id === keyboardMove.payload.column_id);
    const didSucceed = await moveCard(
      card,
      keyboardMove.payload,
      `back to ${origin?.title ?? "its original position"}`,
      false,
    );
    if (didSucceed) {
      setKeyboardMove(null);
      setUndoMove(null);
      setStatusMessage(`Keyboard move cancelled. “${card.title}” returned to its original position.`);
    }
  }, [columns, keyboardMove, moveCard]);

  const handleKeyboardMoveKeyDown = useCallback((
    event: ReactKeyboardEvent<HTMLButtonElement>,
    card: KanbanCard,
  ) => {
    if (keyboardMove?.cardId !== card.card_id || busyAction) return;
    if (event.key === "Enter") {
      event.preventDefault();
      finishKeyboardMove();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      void cancelKeyboardMove(card);
      return;
    }
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();

    const sourceCards = cardsByColumn.get(card.column_id) ?? [];
    const columnIndex = columns.findIndex((column) => column.column_id === card.column_id);
    let payload: KanbanMovePayload | null = null;
    let destination = "";
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      const direction = event.key === "ArrowUp" ? "before" : "after";
      payload = kanbanMoveWithinColumnPayload(sourceCards, card.card_id, direction);
      destination = `${direction === "before" ? "before the previous" : "after the next"} card in ${columns[columnIndex]?.title ?? "the column"}`;
    } else {
      const targetIndex = columnIndex + (event.key === "ArrowLeft" ? -1 : 1);
      const targetColumn = columns[targetIndex];
      if (targetColumn) {
        const targetCount = (cardsByColumn.get(targetColumn.column_id) ?? []).length;
        payload = { column_id: targetColumn.column_id, position: targetCount };
        destination = `to ${targetColumn.title}`;
      }
    }
    if (!payload) {
      setStatusMessage(`“${card.title}” cannot move farther in that direction.`);
      return;
    }
    void moveCard(card, payload, destination, false).then((didSucceed) => {
      if (didSucceed) {
        setKeyboardMove((current) => current?.cardId === card.card_id
          ? { ...current, moved: true }
          : current);
      }
    });
  }, [busyAction, cancelKeyboardMove, cardsByColumn, columns, finishKeyboardMove, keyboardMove, moveCard]);

  const handleColumnDrop = (event: DragEvent<HTMLElement>, column: KanbanColumn) => {
    const cardId = event.dataTransfer.getData(KANBAN_CARD_MIME);
    if (!cardId || !boardData) return;
    event.preventDefault();
    const card = boardData.cards.find((candidate) => candidate.card_id === cardId);
    if (card) {
      dragDropHandledRef.current = true;
      const targetPosition = (cardsByColumn.get(column.column_id) ?? []).length;
      void moveCard(
        card,
        { column_id: column.column_id, position: targetPosition },
        `to ${column.title}`,
      );
    }
  };

  const deleteCard = (card: KanbanCard) => {
    if (!boardData || busyAction) return;
    if (typeof window !== "undefined" && !window.confirm(`Delete “${card.title}”?`)) return;
    const cards = cardsByColumn.get(card.column_id) ?? [];
    const index = cards.findIndex((candidate) => candidate.card_id === card.card_id);
    const adjacentCard = cards[index + 1] ?? cards[index - 1];
    void runMutation(
      `delete:${card.card_id}`,
      () => dataSource.deleteCard(boardData.board.board_id, card.card_id),
      `Deleted “${card.title}”.`,
      adjacentCard?.card_id ?? null,
    );
  };

  useEffect(() => {
    if (!boardData) return;
    const handleHistoryDrop = (event: Event) => {
      const detail = (event as CustomEvent<{ columnId?: string; rawPayload?: string }>).detail;
      const columnId = String(detail?.columnId ?? "").trim();
      const payload = parseHistoryChatDragPayload(String(detail?.rawPayload ?? ""));
      if (!columnId || !payload || !columns.some((column) => column.column_id === columnId)) return;
      void runMutation(
        `import:${payload.conversationId}`,
        () => dataSource.importConversation(boardData.board.board_id, {
          conversation_id: payload.conversationId,
          column_id: columnId,
          title: payload.title,
          workspace_id: workspaceId,
          company_id: companyId,
          use_ai: false,
        }),
        `Imported “${payload.title}”.`,
      );
    };
    window.addEventListener(HISTORY_CHAT_KANBAN_DROP_EVENT, handleHistoryDrop);
    return () => window.removeEventListener(HISTORY_CHAT_KANBAN_DROP_EVENT, handleHistoryDrop);
  }, [boardData, columns, companyId, dataSource, runMutation, workspaceId]);

  if (loadState === "loading" && !boardData) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center" role="status" aria-live="polite">
        <div className="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/70 px-4 py-3 text-sm text-zinc-400">
          <Loader2 size={16} className="animate-spin" aria-hidden="true" />
          Loading Kanban…
        </div>
      </div>
    );
  }

  if (loadState === "error" && !boardData) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6">
        <ErrorNotice
          className="max-w-xl p-5 text-sm"
          copyLabel="Kanban 読み込みエラーをコピー"
          message={error || "Kanban board could not be loaded."}
          messageClassName="mt-1 whitespace-pre-wrap leading-6"
          title="Kanban is unavailable"
        >
          <button type="button" onClick={() => void loadBoard()} className="mt-4 inline-flex min-h-10 items-center gap-2 rounded-lg border border-red-300/25 px-3 text-sm font-semibold text-red-100 hover:bg-red-500/10">
            <RefreshCw size={15} aria-hidden="true" /> Retry
          </button>
        </ErrorNotice>
      </div>
    );
  }

  if (!boardData) return null;

  return (
    <section
      className="rumi-kanban flex min-h-0 flex-1 flex-col overflow-hidden bg-[#09090b]"
      aria-label={`${scopeLabel ?? boardData.board.title} Kanban board`}
      aria-describedby="kanban-interaction-instructions"
    >
      <header className="flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-3 border-b border-zinc-800/70 px-4 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-zinc-100">{boardData.board.title || scopeLabel || "Kanban"}</h2>
          <p className="mt-0.5 text-xs leading-5 text-zinc-500">{scopeLabel || `${scope.type}: ${scope.id}`} · Drag conversations from History onto a column</p>
          <p id="kanban-interaction-instructions" className="mt-0.5 text-xs leading-5 text-zinc-500">
            Open a card title for its full summary. Pointer drag is optional. Use Keyboard move for arrow-key drag, or use the before, after, and Move controls without dragging.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void loadBoard()}
          disabled={Boolean(busyAction) || loadState === "loading"}
          className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950 px-3 text-xs font-medium text-zinc-300 hover:bg-zinc-900 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw size={14} className={loadState === "loading" ? "animate-spin" : undefined} aria-hidden="true" />
          Refresh
        </button>
      </header>

      {error ? (
        <ErrorNotice
          className="mx-4 mt-3 px-3 py-2.5 text-xs"
          copyLabel="Kanban 操作エラーをコピー"
          message={error}
          messageClassName="whitespace-pre-wrap"
          trailing={<button type="button" onClick={() => { setError(null); setStatusMessage(null); }} className="shrink-0 rounded px-2 py-1 text-current/70 hover:bg-white/5 hover:text-current">Dismiss</button>}
        />
      ) : statusMessage ? (
        <div className="mx-4 mt-3 flex items-start justify-between gap-3 rounded-xl border border-emerald-400/20 bg-emerald-500/[0.08] px-3 py-2.5 text-xs text-emerald-100" role="status" aria-live="polite">
          <span className="min-w-0 whitespace-pre-wrap break-words">{statusMessage}</span>
          <button type="button" onClick={() => setStatusMessage(null)} className="shrink-0 rounded px-2 py-1 text-current/70 hover:bg-white/5 hover:text-current">Dismiss</button>
        </div>
      ) : null}

      {columns.length === 0 ? (
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <div className="max-w-md rounded-2xl border border-zinc-800 bg-zinc-950/60 p-5 text-center">
            <h3 className="text-sm font-semibold text-zinc-200">This board has no columns</h3>
            <p className="mt-2 text-xs leading-5 text-zinc-500">The host created the board but did not return a column configuration. Refresh after configuring the board in the host.</p>
          </div>
        </div>
      ) : (
        <div className="rumi-kanban-columns flex min-h-0 flex-1 gap-3 overflow-x-auto overflow-y-hidden p-3" role="list" aria-label="Kanban columns">
          {columns.map((column, columnIndex) => {
            const cards = cardsByColumn.get(column.column_id) ?? [];
            const isAtWipLimit = Boolean(column.wip_limit && cards.length >= column.wip_limit);
            return (
              <article
                key={column.column_id}
                data-kanban-column-id={column.column_id}
                className="flex h-full min-h-0 w-[min(310px,82vw)] min-w-[260px] flex-col rounded-xl border border-zinc-800/80 bg-[#101014]"
                role="listitem"
                aria-label={kanbanColumnSummary(column, columnIndex, columns.length, cards.length)}
                aria-posinset={columnIndex + 1}
                aria-setsize={columns.length}
                onDragOver={(event) => {
                  if (event.dataTransfer.types.includes(KANBAN_CARD_MIME)) {
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                  }
                }}
                onDrop={(event) => handleColumnDrop(event, column)}
              >
                <header className="flex min-h-11 shrink-0 items-center justify-between gap-2 border-b border-zinc-800/70 px-3">
                  <div className="min-w-0">
                    <h3 className="truncate text-xs font-semibold text-zinc-200">{column.title}</h3>
                  </div>
                  <span className={cn("rounded-full border px-2 py-0.5 text-xs tabular-nums", isAtWipLimit ? "border-amber-400/30 bg-amber-500/10 text-amber-200" : "border-zinc-800 bg-zinc-950 text-zinc-500")}>{cards.length} {cards.length === 1 ? "card" : "cards"}{column.wip_limit ? ` · limit ${column.wip_limit}${isAtWipLimit ? " reached" : ""}` : ""}</span>
                </header>

                <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2.5" role="list" aria-label={`${column.title} cards`}>
                  {cards.length === 0 && <p className="rounded-lg border border-dashed border-zinc-800 px-3 py-6 text-center text-xs leading-5 text-zinc-600">Drop a card or conversation here</p>}
                  {cards.map((card, cardIndex) => {
                    const busy = busyAction?.endsWith(card.card_id) === true;
                    const expanded = expandedCardId === card.card_id;
                    const moveBefore = kanbanMoveWithinColumnPayload(cards, card.card_id, "before");
                    const moveAfter = kanbanMoveWithinColumnPayload(cards, card.card_id, "after");
                    const isKeyboardMoving = keyboardMove?.cardId === card.card_id;
                    const cardTitleId = `kanban-card-title-${card.card_id}`;
                    const cardSummaryId = `kanban-card-summary-${card.card_id}`;
                    return (
                      <article
                        key={card.card_id}
                        draggable={!busyAction}
                        onDragStart={(event) => {
                          dragDropHandledRef.current = false;
                          draggedCardTitleRef.current = card.title;
                          event.dataTransfer.effectAllowed = "move";
                          event.dataTransfer.setData(KANBAN_CARD_MIME, card.card_id);
                          setError(null);
                          setStatusMessage(`Picked up “${card.title}”. Drop on a named column, or press Escape to cancel the pointer drag.`);
                        }}
                        onDragEnd={() => {
                          if (!dragDropHandledRef.current) {
                            setStatusMessage(`Move cancelled. “${draggedCardTitleRef.current || card.title}” stayed in ${column.title}.`);
                          }
                          dragDropHandledRef.current = false;
                          draggedCardTitleRef.current = "";
                        }}
                        className="group/card rounded-lg border border-zinc-800 bg-zinc-950/75 p-3 shadow-sm transition-colors hover:border-zinc-700 hover:bg-zinc-950"
                        role="listitem"
                        aria-labelledby={cardTitleId}
                        aria-describedby={`${cardSummaryId} kanban-interaction-instructions`}
                        aria-posinset={cardIndex + 1}
                        aria-setsize={cards.length}
                        aria-current={isKeyboardMoving ? "true" : undefined}
                      >
                        <div className="flex items-start gap-2">
                          <span className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center text-zinc-600" title="Pointer drag handle" aria-hidden="true">
                            <GripVertical size={16} className="cursor-grab group-hover/card:text-zinc-400" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <h4 className="text-sm font-medium leading-5 text-zinc-200">
                              <button
                                ref={(node) => {
                                  if (node) cardFocusTargetsRef.current.set(card.card_id, node);
                                  else cardFocusTargetsRef.current.delete(card.card_id);
                                }}
                                id={cardTitleId}
                                type="button"
                                aria-expanded={expanded}
                                aria-controls={`kanban-card-details-${card.card_id}`}
                                onKeyDown={(event) => handleKeyboardMoveKeyDown(event, card)}
                                onClick={() => setExpandedCardId((current) => current === card.card_id ? null : card.card_id)}
                                className="min-h-11 w-full rounded-md px-1 text-left font-semibold outline-none hover:bg-zinc-900 focus-visible:ring-2 focus-visible:ring-sky-400"
                              >
                                {card.title}
                              </button>
                            </h4>
                            <p id={cardSummaryId} className="sr-only">{kanbanCardSummary(card, column.title, cardIndex, cards.length)}</p>
                            <div id={`kanban-card-details-${card.card_id}`}>
                              {card.description && <p className={cn("mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-zinc-500", !expanded && "line-clamp-3")}>{card.description}</p>}
                              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                                <span className={cn("rounded-md border px-2 py-1 font-medium", priorityClass(card.priority))}>Priority: {kanbanPriorityLabel(card.priority)}</span>
                                {card.assignee && <span className="max-w-full truncate rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-zinc-400">Assignee: {card.assignee}</span>}
                                {card.due_at && <time className="rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1 text-zinc-400" dateTime={card.due_at}>Due: {card.due_at}</time>}
                                {Boolean(card.blocked_by?.length) && <span className="rounded-md border border-red-400/30 bg-red-500/10 px-2 py-1 text-red-200">Blocked by {card.blocked_by?.length}</span>}
                                {card.agent_status && <span className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-300">Run: {card.agent_status}</span>}
                                {Boolean(card.checklist?.length) && <span className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-300">Checklist: {card.checklist?.filter((item) => item.done).length}/{card.checklist?.length}</span>}
                                <span className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-300">Sync: {card.source_type === "conversation" ? "Conversation" : "Local card"}</span>
                              </div>
                            </div>
                          </div>
                          {busy && <Loader2 size={14} className="shrink-0 animate-spin text-zinc-500" aria-label="Updating" />}
                        </div>
                        <div className="mt-2 flex flex-wrap items-center justify-end gap-1 border-t border-zinc-800/60 pt-2" role="group" aria-label={`Actions for ${card.title}`}>
                          <button
                            type="button"
                            disabled={Boolean(busyAction) || Boolean(keyboardMove && !isKeyboardMoving)}
                            onClick={() => isKeyboardMoving ? finishKeyboardMove() : beginKeyboardMove(card)}
                            className="inline-flex min-h-11 items-center gap-1.5 rounded-lg px-3 text-xs font-semibold text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-30"
                            aria-label={isKeyboardMoving ? `Drop ${card.title} after keyboard move` : `Start keyboard move for ${card.title}`}
                          ><Move size={15} aria-hidden="true" /> {isKeyboardMoving ? "Drop" : "Keyboard move"}</button>
                          <button
                            type="button"
                            disabled={!moveBefore || Boolean(busyAction)}
                            onClick={() => moveBefore && void moveCard(card, moveBefore, `before the previous card in ${column.title}`)}
                            className="flex h-11 w-11 items-center justify-center rounded-lg text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-30"
                            aria-label={`Move ${card.title} before the previous card in ${column.title}`}
                          ><ArrowUp size={16} aria-hidden="true" /></button>
                          <button
                            type="button"
                            disabled={!moveAfter || Boolean(busyAction)}
                            onClick={() => moveAfter && void moveCard(card, moveAfter, `after the next card in ${column.title}`)}
                            className="flex h-11 w-11 items-center justify-center rounded-lg text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-30"
                            aria-label={`Move ${card.title} after the next card in ${column.title}`}
                          ><ArrowDown size={16} aria-hidden="true" /></button>
                          <details
                            open={moveMenuCardId === card.card_id}
                            onToggle={(event) => setMoveMenuCardId(event.currentTarget.open ? card.card_id : null)}
                            className="min-w-0"
                          >
                            <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 rounded-lg px-3 text-xs font-semibold text-zinc-300 hover:bg-zinc-800 focus-visible:ring-2 focus-visible:ring-sky-400" aria-label={`Move ${card.title} to another column`}>
                              <Move size={15} aria-hidden="true" /> Move
                            </summary>
                            <div className="mt-2 grid min-w-[220px] gap-1 rounded-lg border border-zinc-700 bg-zinc-950 p-2" role="group" aria-label={`Choose a destination for ${card.title}`}>
                              {columns.filter((target) => target.column_id !== card.column_id).map((target) => {
                                const targetCount = (cardsByColumn.get(target.column_id) ?? []).length;
                                const targetLimit = Number(target.wip_limit ?? 0);
                                const targetAtLimit = targetLimit > 0 && targetCount >= targetLimit;
                                return (
                                  <button
                                    key={target.column_id}
                                    type="button"
                                    disabled={Boolean(busyAction) || targetAtLimit}
                                    onClick={() => {
                                      void moveCard(
                                        card,
                                        { column_id: target.column_id, position: targetCount },
                                        `to ${target.title}`,
                                      ).then((didSucceed) => {
                                        if (didSucceed) setMoveMenuCardId(null);
                                      });
                                    }}
                                    className="min-h-11 rounded-md px-3 text-left text-xs text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:text-zinc-600"
                                  >
                                    {target.title} · {targetCount} {targetCount === 1 ? "card" : "cards"}{targetAtLimit ? ` · WIP limit ${targetLimit} reached` : ""}
                                  </button>
                                );
                              })}
                              <button type="button" onClick={() => setMoveMenuCardId(null)} className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-md px-3 text-xs text-zinc-400 hover:bg-zinc-800">
                                <X size={14} aria-hidden="true" /> Cancel move
                              </button>
                            </div>
                          </details>
                          <button type="button" disabled={Boolean(busyAction)} onClick={() => deleteCard(card)} className="flex h-11 w-11 items-center justify-center rounded-lg text-zinc-500 hover:bg-red-500/10 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-30" aria-label={`Delete ${card.title}`}><Trash2 size={16} aria-hidden="true" /></button>
                        </div>
                      </article>
                    );
                  })}
                </div>

                <form className="shrink-0 border-t border-zinc-800/70 p-2.5" onSubmit={(event) => handleCreateCard(event, column)}>
                  <label className="sr-only" htmlFor={`kanban-new-${column.column_id}`}>New card in {column.title}</label>
                  <div className="flex items-center gap-2">
                    <input
                      id={`kanban-new-${column.column_id}`}
                      type="text"
                      value={draftByColumn[column.column_id] ?? ""}
                      onChange={(event) => setDraftByColumn((current) => ({ ...current, [column.column_id]: event.target.value }))}
                      placeholder="Add a card"
                      maxLength={240}
                      className="min-h-10 min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 text-xs text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
                    />
                    <button type="submit" disabled={!draftByColumn[column.column_id]?.trim() || Boolean(busyAction) || isAtWipLimit} className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-35" aria-label={`Add card to ${column.title}`}><Plus size={16} aria-hidden="true" /></button>
                  </div>
                  {isAtWipLimit && <p className="mt-1.5 text-xs text-amber-300/80">WIP limit {column.wip_limit} reached. Move or finish a card before adding another.</p>}
                </form>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
