import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  GripVertical,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
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
  const initialDataConsumedRef = useRef(Boolean(initialData));
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

  const runMutation = useCallback(async (key: string, mutation: () => Promise<void>, successMessage: string): Promise<boolean> => {
    setBusyAction(key);
    setError(null);
    setStatusMessage(null);
    try {
      await mutation();
      const next = await dataSource.loadBoard(stableScope);
      setBoardData(next);
      setLoadState("ready");
      setStatusMessage(successMessage);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Kanban update failed.");
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

  const moveCard = useCallback((card: KanbanCard, targetColumnId: string) => {
    if (!boardData || busyAction || card.column_id === targetColumnId) return;
    void runMutation(
      `move:${card.card_id}`,
      () => dataSource.moveCard(boardData.board.board_id, card.card_id, { column_id: targetColumnId }),
      `Moved “${card.title}”.`,
    );
  }, [boardData, busyAction, dataSource, runMutation]);

  const handleColumnDrop = (event: DragEvent<HTMLElement>, column: KanbanColumn) => {
    const cardId = event.dataTransfer.getData(KANBAN_CARD_MIME);
    if (!cardId || !boardData) return;
    event.preventDefault();
    const card = boardData.cards.find((candidate) => candidate.card_id === cardId);
    if (card) moveCard(card, column.column_id);
  };

  const deleteCard = (card: KanbanCard) => {
    if (!boardData || busyAction) return;
    if (typeof window !== "undefined" && !window.confirm(`Delete “${card.title}”?`)) return;
    void runMutation(
      `delete:${card.card_id}`,
      () => dataSource.deleteCard(boardData.board.board_id, card.card_id),
      `Deleted “${card.title}”.`,
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
    <section className="rumi-kanban flex min-h-0 flex-1 flex-col overflow-hidden bg-[#09090b]" aria-label={`${scopeLabel ?? boardData.board.title} Kanban board`}>
      <header className="flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-3 border-b border-zinc-800/70 px-4 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-zinc-100">{boardData.board.title || scopeLabel || "Kanban"}</h2>
          <p className="mt-0.5 truncate text-[11px] text-zinc-500">{scopeLabel || `${scope.type}: ${scope.id}`} · Drag conversations from History onto a column</p>
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
                aria-label={`${column.title}, ${cards.length} cards`}
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
                  <span className={cn("rounded-full border px-2 py-0.5 text-[10px] tabular-nums", isAtWipLimit ? "border-amber-400/30 bg-amber-500/10 text-amber-200" : "border-zinc-800 bg-zinc-950 text-zinc-500")}>{cards.length}{column.wip_limit ? ` / ${column.wip_limit}` : ""}</span>
                </header>

                <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2.5" role="list" aria-label={`${column.title} cards`}>
                  {cards.length === 0 && <p className="rounded-lg border border-dashed border-zinc-800 px-3 py-6 text-center text-[11px] leading-5 text-zinc-600">Drop a card or conversation here</p>}
                  {cards.map((card) => {
                    const previousColumn = columns[columnIndex - 1];
                    const nextColumn = columns[columnIndex + 1];
                    const busy = busyAction?.endsWith(card.card_id) === true;
                    return (
                      <article
                        key={card.card_id}
                        draggable={!busyAction}
                        onDragStart={(event) => {
                          event.dataTransfer.effectAllowed = "move";
                          event.dataTransfer.setData(KANBAN_CARD_MIME, card.card_id);
                        }}
                        className="group/card rounded-lg border border-zinc-800 bg-zinc-950/75 p-3 shadow-sm transition-colors hover:border-zinc-700 hover:bg-zinc-950"
                        role="listitem"
                      >
                        <div className="flex items-start gap-2">
                          <GripVertical size={14} className="mt-0.5 shrink-0 cursor-grab text-zinc-700 group-hover/card:text-zinc-500" aria-hidden="true" />
                          <div className="min-w-0 flex-1">
                            <h4 className="break-words text-[12px] font-medium leading-5 text-zinc-200">{card.title}</h4>
                            {card.description && <p className="mt-1 line-clamp-3 whitespace-pre-wrap break-words text-[11px] leading-5 text-zinc-500">{card.description}</p>}
                            <div className="mt-2 flex flex-wrap items-center gap-1.5">
                              <span className={cn("rounded-md border px-1.5 py-0.5 text-[9px] font-medium", priorityClass(card.priority))}>{kanbanPriorityLabel(card.priority)}</span>
                              {card.assignee && <span className="max-w-full truncate rounded-md border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[9px] text-zinc-400">{card.assignee}</span>}
                              {card.due_at && <time className="rounded-md border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[9px] text-zinc-400" dateTime={card.due_at}>{card.due_at}</time>}
                            </div>
                          </div>
                          {busy && <Loader2 size={14} className="shrink-0 animate-spin text-zinc-500" aria-label="Updating" />}
                        </div>
                        <div className="mt-2 flex items-center justify-end gap-1 border-t border-zinc-800/60 pt-2 opacity-70 transition-opacity group-hover/card:opacity-100 group-focus-within/card:opacity-100">
                          <button type="button" disabled={!previousColumn || Boolean(busyAction)} onClick={() => previousColumn && moveCard(card, previousColumn.column_id)} className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-30" aria-label={`Move ${card.title} left`}><ArrowLeft size={14} /></button>
                          <button type="button" disabled={!nextColumn || Boolean(busyAction)} onClick={() => nextColumn && moveCard(card, nextColumn.column_id)} className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-30" aria-label={`Move ${card.title} right`}><ArrowRight size={14} /></button>
                          <button type="button" disabled={Boolean(busyAction)} onClick={() => deleteCard(card)} className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-600 hover:bg-red-500/10 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-30" aria-label={`Delete ${card.title}`}><Trash2 size={14} /></button>
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
                    <button type="submit" disabled={!draftByColumn[column.column_id]?.trim() || Boolean(busyAction) || isAtWipLimit} className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-35" aria-label={`Add card to ${column.title}`}><Plus size={16} /></button>
                  </div>
                  {isAtWipLimit && <p className="mt-1.5 text-[10px] text-amber-300/80">WIP limit reached</p>}
                </form>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
