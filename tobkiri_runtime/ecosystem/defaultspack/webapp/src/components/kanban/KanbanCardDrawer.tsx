import { ExternalLink, MessageSquareText, PlayCircle, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import type { KanbanCard, KanbanColumn, ModelProfile } from "../../lib/api";
import { cn } from "../../lib/cn";
import { KanbanRunBadge } from "./KanbanRunBadge";
import { ModalFoundation } from "../ModalFoundation";

export type KanbanAgentAction = "start" | "refresh" | "ready" | "apply" | "dismiss";

type DraftState = {
  title: string;
  description: string;
  priority: string;
  assignee: string;
  dueAt: string;
  labels: string;
  checklist: string;
  columnId: string;
};

function checklistToText(card: KanbanCard | null): string {
  return (card?.checklist ?? [])
    .map((item) => `${item.done ? "[x]" : "[ ]"} ${item.title}`)
    .join("\n");
}

function parseChecklist(value: string) {
  return value
    .split(/\r?\n/)
    .map((line, index) => {
      const trimmed = line.trim();
      if (!trimmed) return null;
      const done = /^\[[xX]\]\s*/.test(trimmed);
      const title = trimmed.replace(/^\[[ xX]\]\s*/, "").trim();
      if (!title) return null;
      return { id: `check-${index}-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 24)}`, title, done };
    })
    .filter((item): item is { id: string; title: string; done: boolean } => Boolean(item));
}

function parseLabels(value: string): string[] {
  return [...new Set(value.split(",").map((label) => label.trim()).filter(Boolean))].slice(0, 12);
}

function draftFromCard(card: KanbanCard | null, fallbackColumnId: string): DraftState {
  return {
    title: card?.title ?? "",
    description: card?.description ?? "",
    priority: card?.priority ?? "normal",
    assignee: card?.assignee ?? "",
    dueAt: card?.due_at ?? "",
    labels: (card?.labels ?? []).join(", "),
    checklist: checklistToText(card),
    columnId: card?.column_id ?? fallbackColumnId,
  };
}

export function KanbanCardDrawer({
  card,
  columns,
  defaultColumnId,
  modelId,
  modelProfiles,
  busy,
  onClose,
  onSave,
  onDelete,
  onAgentAction,
  onOpenChat,
}: {
  card: KanbanCard | null;
  columns: KanbanColumn[];
  defaultColumnId: string;
  modelId: string;
  modelProfiles: ModelProfile[];
  busy: boolean;
  onClose: () => void;
  onSave: (updates: Partial<KanbanCard>) => void;
  onDelete?: (cardId: string) => void;
  onAgentAction?: (card: KanbanCard, action: KanbanAgentAction) => void;
  onOpenChat?: (conversationId: string) => void;
}) {
  const [draft, setDraft] = useState<DraftState>(() => draftFromCard(card, defaultColumnId));
  const isCreate = !card;
  const selectedProfile = useMemo(
    () => modelProfiles.find((profile) => profile.profile_id === modelId || profile.qualified_model_id === modelId) ?? null,
    [modelId, modelProfiles],
  );

  useEffect(() => {
    setDraft(draftFromCard(card, defaultColumnId));
  }, [card, defaultColumnId]);

  const save = () => {
    const title = draft.title.trim();
    if (!title) return;
    onSave({
      title,
      description: draft.description.trim() || null,
      priority: draft.priority,
      assignee: draft.assignee.trim() || null,
      due_at: draft.dueAt.trim() || null,
      labels: parseLabels(draft.labels),
      checklist: parseChecklist(draft.checklist),
      column_id: draft.columnId,
    });
  };

  return createPortal(
    <ModalFoundation
      variant="drawer"
      title={isCreate ? "New card" : `Card detail: ${card?.title ?? ""}`}
      description="Edit this Kanban card. Escape closes the topmost drawer."
      onClose={onClose}
      dismissible={!busy}
      backdropClassName="fixed inset-0 rumi-layer-modal flex justify-end bg-black/35 motion-reduce:transition-none"
      panelClassName="h-full w-[min(440px,calc(100vw-56px))] overflow-hidden border-l border-zinc-800 bg-[#0b0b0e] shadow-2xl outline-none"
    >
      <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-zinc-800 px-3">
        <div className="min-w-0">
          <h3 className="truncate text-[13px] font-semibold text-zinc-100">{isCreate ? "New card" : "Card detail"}</h3>
          <p className="truncate text-[10px] text-zinc-600">{selectedProfile?.display_name ?? modelId}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-100"
          title="Close"
          aria-label="Close"
        >
          <X size={15} />
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        {card && (
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/45 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <KanbanRunBadge status={card.agent_status} branch={card.branch} />
              {card.pr_url && (
                <a
                  href={card.pr_url}
                  target="_blank"
                  rel="noreferrer"
                  className="flex h-7 items-center gap-1 rounded-md border border-zinc-800 px-2 text-[11px] text-sky-300 transition hover:bg-sky-500/10"
                >
                  <ExternalLink size={12} />
                  PR
                </a>
              )}
            </div>
            <div className="mt-2 grid grid-cols-2 gap-1.5">
              <button
                type="button"
                onClick={() => onAgentAction?.(card, "start")}
                disabled={busy}
                className="flex h-8 items-center justify-center gap-1 rounded-md border border-sky-500/30 text-[11px] text-sky-200 transition hover:bg-sky-500/10 disabled:opacity-40"
              >
                <PlayCircle size={12} />
                Run agent
              </button>
              <button
                type="button"
                onClick={() => onAgentAction?.(card, "refresh")}
                disabled={busy}
                className="flex h-8 items-center justify-center gap-1 rounded-md border border-zinc-800 text-[11px] text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
              >
                <RefreshCw size={12} />
                Refresh
              </button>
              <button
                type="button"
                onClick={() => onAgentAction?.(card, "ready")}
                disabled={busy}
                className="flex h-8 items-center justify-center gap-1 rounded-md border border-amber-500/30 text-[11px] text-amber-200 transition hover:bg-amber-500/10 disabled:opacity-40"
              >
                <ShieldCheck size={12} />
                Ready
              </button>
              <button
                type="button"
                onClick={() => onAgentAction?.(card, "apply")}
                disabled={busy}
                className="flex h-8 items-center justify-center gap-1 rounded-md border border-emerald-500/30 text-[11px] text-emerald-200 transition hover:bg-emerald-500/10 disabled:opacity-40"
              >
                <ShieldCheck size={12} />
                Apply
              </button>
              <button
                type="button"
                onClick={() => onAgentAction?.(card, "dismiss")}
                disabled={busy}
                className="col-span-2 flex h-8 items-center justify-center gap-1 rounded-md border border-zinc-800 text-[11px] text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        <label className="block space-y-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Title</span>
          <input
            value={draft.title}
            onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))}
            className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[13px] text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            placeholder="Task title"
          />
        </label>

        <label className="block space-y-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Description</span>
          <textarea
            value={draft.description}
            onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
            className="min-h-24 w-full resize-y rounded-md border border-zinc-800 bg-zinc-950 px-2 py-2 text-[12px] leading-5 text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            placeholder="Notes, acceptance criteria, links"
          />
        </label>

        <div className="grid grid-cols-2 gap-2">
          <label className="block space-y-1">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Column</span>
            <select
              value={draft.columnId}
              onChange={(event) => setDraft((current) => ({ ...current, columnId: event.target.value }))}
              className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none focus:border-zinc-600"
            >
              {columns.map((column) => (
                <option key={column.column_id} value={column.column_id}>{column.title}</option>
              ))}
            </select>
          </label>
          <label className="block space-y-1">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Priority</span>
            <select
              value={draft.priority}
              onChange={(event) => setDraft((current) => ({ ...current, priority: event.target.value }))}
              className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none focus:border-zinc-600"
            >
              <option value="low">Low</option>
              <option value="normal">Normal</option>
              <option value="high">High</option>
              <option value="urgent">Urgent</option>
            </select>
          </label>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <label className="block space-y-1">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Assignee</span>
            <input
              value={draft.assignee}
              onChange={(event) => setDraft((current) => ({ ...current, assignee: event.target.value }))}
              className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
              placeholder="agent or person"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Due</span>
            <input
              value={draft.dueAt}
              onChange={(event) => setDraft((current) => ({ ...current, dueAt: event.target.value }))}
              className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
              placeholder="YYYY-MM-DD"
            />
          </label>
        </div>

        <label className="block space-y-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Labels</span>
          <input
            value={draft.labels}
            onChange={(event) => setDraft((current) => ({ ...current, labels: event.target.value }))}
            className="h-9 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            placeholder="frontend, qa, blocked"
          />
        </label>

        <label className="block space-y-1">
          <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Checklist</span>
          <textarea
            value={draft.checklist}
            onChange={(event) => setDraft((current) => ({ ...current, checklist: event.target.value }))}
            className="min-h-20 w-full resize-y rounded-md border border-zinc-800 bg-zinc-950 px-2 py-2 font-mono text-[11px] leading-5 text-zinc-300 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            placeholder="[ ] item"
          />
        </label>
      </div>

      <footer className="flex shrink-0 items-center justify-between gap-2 border-t border-zinc-800 p-3">
        <div className="flex items-center gap-1.5">
          {card?.conversation_id && onOpenChat && (
            <button
              type="button"
              onClick={() => onOpenChat(card.conversation_id || "")}
              className="flex h-8 items-center gap-1 rounded-md border border-zinc-800 px-2 text-[11px] text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
            >
              <MessageSquareText size={12} />
              Chat
            </button>
          )}
          {card && onDelete && (
            <button
              type="button"
              onClick={() => onDelete(card.card_id)}
              disabled={busy}
              className="flex h-8 w-8 items-center justify-center rounded-md border border-red-500/25 text-red-300 transition hover:bg-red-500/10 disabled:opacity-40"
              title="Delete"
              aria-label="Delete"
            >
              <Trash2 size={13} />
            </button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onClose}
            className="h-8 rounded-md border border-zinc-800 px-3 text-[12px] text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={busy || !draft.title.trim()}
            className={cn(
              "h-8 rounded-md px-3 text-[12px] font-semibold transition",
              draft.title.trim() ? "bg-zinc-100 text-zinc-950 hover:bg-white" : "bg-zinc-800 text-zinc-500",
            )}
          >
            Save
          </button>
        </div>
      </footer>
      </div>
    </ModalFoundation>,
    document.body,
  );
}
