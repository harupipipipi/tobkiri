import type { ReactNode } from "react";
import { FolderOpen, KanbanSquare, Monitor, PanelLeftClose, PanelLeftOpen, Search, Tag } from "lucide-react";
import { cn } from "../../lib/cn";
import { WarmActionIcon } from "../WarmActionIcon";

type HistoryNavigationProps = {
  compact: boolean;
  selectionMode: boolean;
  onToggle?: () => void;
  onSearchOpen?: () => void;
  onCreateChat: () => void;
  onCreateProject: () => void;
  projectOpen: boolean;
  projectForm: ReactNode;
  onCalendarOpen?: () => void;
  onKanbanOpen?: () => void;
  onDesktopsOpen?: () => void;
  calendarActive?: boolean;
  kanbanActive?: boolean;
  desktopsActive?: boolean;
  searchFilter: ReactNode;
  tagFilter: ReactNode;
  hasTags: boolean;
};

export function HistoryNavigation(props: HistoryNavigationProps) {
  const toggleLabel = props.compact ? "サイドバーを開く" : "サイドバーを閉じる";
  const ToggleIcon = props.compact ? PanelLeftOpen : PanelLeftClose;
  const actions = [
    { label: "New Chat", onClick: props.onCreateChat, icon: <WarmActionIcon kind="newChat" size="sm" iconClassName="h-4 w-4" /> },
    { label: "New Project", onClick: props.onCreateProject, icon: <WarmActionIcon kind="group" size="sm" iconClassName="h-4 w-4" />, expanded: props.projectOpen },
    { label: "Calendar", onClick: props.onCalendarOpen, icon: <WarmActionIcon kind="calendar" size="sm" iconClassName="h-4 w-4" />, active: props.calendarActive },
    { label: "Kanban", onClick: props.onKanbanOpen, icon: <KanbanSquare size={16} className="rumi-nav-icon" />, active: props.kanbanActive },
    { label: "Desktops", onClick: props.onDesktopsOpen, icon: <Monitor size={16} className="rumi-nav-icon" />, active: props.desktopsActive },
  ];

  return (
    <>
      <header className="flex w-full shrink-0 flex-col border-b border-zinc-800/60 bg-[var(--rumi-surface-base)] px-2.5 py-2">
        <div className="flex h-10 shrink-0 items-center gap-1 overflow-hidden">
          {props.onToggle && (
            <button type="button" onClick={props.onToggle} title={toggleLabel} aria-label={toggleLabel} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100">
              <ToggleIcon size={18} aria-hidden="true" />
            </button>
          )}
          {!props.compact && (
            <>
              <span className="min-w-0 truncate text-xs font-semibold text-zinc-300">Tobkiri</span>
              {props.onSearchOpen && (
                <button type="button" onClick={props.onSearchOpen} title="チャットを検索" aria-label="チャットを検索" className="ml-auto flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100">
                  <Search size={16} aria-hidden="true" />
                </button>
              )}
            </>
          )}
        </div>
        {!props.selectionMode && (
          <nav aria-label="メインナビゲーション" className="mt-2 flex flex-col gap-1">
            {actions.map((action) => (
              <div key={action.label} className="relative">
                <button
                  type="button"
                  onClick={action.onClick}
                  title={action.label}
                  aria-label={action.label}
                  aria-expanded={action.expanded}
                  aria-current={action.active ? "page" : undefined}
                  className={cn(
                    "flex h-9 w-full items-center gap-1 overflow-hidden rounded-md text-left text-xs font-medium transition-colors",
                    action.active ? "bg-zinc-800 text-zinc-100" : "text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100",
                  )}
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center" aria-hidden="true">{action.icon}</span>
                  {!props.compact && <span className="truncate">{action.label}</span>}
                </button>
                {action.label === "New Project" && props.projectForm}
              </div>
            ))}
          </nav>
        )}
        {props.searchFilter}
        {props.hasTags && (
          <div className="mt-1 flex h-8 shrink-0 items-center">
            {props.compact ? (
              <button type="button" onClick={props.onToggle} title="タグで絞り込む" aria-label="タグで絞り込む" className="flex h-8 w-9 shrink-0 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100">
                <Tag size={14} aria-hidden="true" />
              </button>
            ) : props.tagFilter}
          </div>
        )}
      </header>
      {!props.selectionMode && (
        <div className="flex h-10 w-full shrink-0 items-center gap-1 overflow-hidden border-b border-zinc-800/70 px-2.5 text-zinc-500">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center"><FolderOpen size={16} aria-hidden="true" /></span>
          {!props.compact && <span className="truncate text-[10px] font-semibold uppercase tracking-[0.12em]">Projects</span>}
        </div>
      )}
    </>
  );
}
